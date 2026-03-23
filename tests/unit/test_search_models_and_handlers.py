from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from src.handlers.handlers import Handlers
from src.models import models as m
from src.server import P4MCPServer


def _make_handlers(search_services):
    return Handlers(
        server_services=MagicMock(),
        workspace_services=MagicMock(),
        file_services=MagicMock(),
        changelist_services=MagicMock(),
        shelve_services=MagicMock(),
        job_services=MagicMock(),
        review_services=MagicMock(),
        search_services=search_services,
    )


class TestSearchParams:
    def test_requires_search_text_for_search_content(self):
        with pytest.raises(ValidationError):
            m.SearchParams(action="search_content", depot_path="//depot/...")

    def test_allows_search_files_without_search_text(self):
        params = m.SearchParams(action="search_files", depot_path="//depot/...")
        assert params.search_text is None

    def test_context_lines_defaults_to_1(self):
        params = m.SearchParams(
            action="search_content", depot_path="//depot/...", search_text="TODO"
        )
        assert params.context_lines == 1

    def test_context_lines_accepts_valid_range(self):
        for val in [0, 1, 3, 5]:
            params = m.SearchParams(
                action="search_content",
                depot_path="//depot/...",
                search_text="TODO",
                context_lines=val,
            )
            assert params.context_lines == val

    def test_context_lines_rejects_above_max(self):
        with pytest.raises(ValidationError):
            m.SearchParams(
                action="search_content",
                depot_path="//depot/...",
                search_text="TODO",
                context_lines=6,
            )

    def test_context_lines_rejects_negative(self):
        with pytest.raises(ValidationError):
            m.SearchParams(
                action="search_content",
                depot_path="//depot/...",
                search_text="TODO",
                context_lines=-1,
            )


class TestSearchHandler:
    @pytest.mark.asyncio
    async def test_dispatches_search_files(self):
        search_services = MagicMock()
        search_services.search_files = AsyncMock(
            return_value={
                "status": "success",
                "message": [{"depotFile": "//depot/a.txt"}],
            }
        )
        handlers = _make_handlers(search_services)

        params = SimpleNamespace(
            action="search_files", depot_path="//depot/...", max_results=25
        )
        result = await handlers.handle(
            "query", "search", params, effective_user="alice"
        )

        search_services.search_files.assert_awaited_once_with(
            depot_path="//depot/...", max_results=25, effective_user="alice"
        )
        assert result == {
            "status": "success",
            "action": "search_files",
            "message": [{"depotFile": "//depot/a.txt"}],
        }

    @pytest.mark.asyncio
    async def test_propagates_truncated_flag(self):
        search_services = MagicMock()
        search_services.list_directories = AsyncMock(
            return_value={
                "status": "success",
                "message": ["//depot/a", "//depot/b"],
                "truncated": True,
            }
        )
        handlers = _make_handlers(search_services)

        params = SimpleNamespace(
            action="search_dirs", depot_path="//depot/*", max_results=1
        )
        result = await handlers.handle("query", "search", params, effective_user=None)

        assert result["status"] == "success"
        assert result["action"] == "search_dirs"
        assert result["truncated"] is True

    @pytest.mark.asyncio
    async def test_dispatches_search_content_with_context_lines(self):
        search_services = MagicMock()
        search_services.search_content = AsyncMock(
            return_value={"status": "success", "message": [], "truncated": False}
        )
        handlers = _make_handlers(search_services)

        params = SimpleNamespace(
            action="search_content",
            depot_path="//depot/...",
            search_text="TODO",
            max_results=50,
            case_insensitive=False,
            show_line_numbers=True,
            filenames_only=False,
            context_lines=3,
        )
        result = await handlers.handle(
            "query", "search", params, effective_user="alice"
        )

        search_services.search_content.assert_awaited_once_with(
            search_text="TODO",
            depot_path="//depot/...",
            max_results=50,
            case_insensitive=False,
            show_line_numbers=True,
            filenames_only=False,
            context_lines=3,
            effective_user="alice",
        )
        assert result["status"] == "success"
        assert result["action"] == "search_content"


class TestSearchToolValidation:
    def test_clean_error_for_missing_search_text(self):
        error = P4MCPServer._validate_search_params(
            action="search_content",
            depot_path="//depot/...",
            search_text=None,
            case_insensitive=False,
            show_line_numbers=True,
            filenames_only=False,
            max_results=100,
        )
        assert error == {
            "status": "error",
            "action": "search_content",
            "message": "search_text is required when action is 'search_content'",
        }

    def test_clean_error_for_search_dirs_using_ellipsis(self):
        error = P4MCPServer._validate_search_params(
            action="search_dirs",
            depot_path="//depot/...",
            search_text=None,
            case_insensitive=False,
            show_line_numbers=True,
            filenames_only=False,
            max_results=100,
        )
        assert error == {
            "status": "error",
            "action": "search_dirs",
            "message": (
                "search_dirs requires '*' wildcards (for example '//depot/*'); "
                "do not use '...'."
            ),
        }
