from unittest.mock import MagicMock

import pytest
from P4 import P4Exception

from src.core.config import DEFAULT_SEARCH_MAX_RESULTS_CAP
from src.services.search_services import SearchServices


class _Conn:
    def __init__(self, p4):
        self.p4 = p4

    async def __aenter__(self):
        return self.p4

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _service_with_p4(p4, max_results_cap=DEFAULT_SEARCH_MAX_RESULTS_CAP):
    manager = MagicMock()
    manager.get_connection.return_value = _Conn(p4)
    return SearchServices(manager, max_results_cap=max_results_cap), manager


class TestSearchServices:
    @pytest.mark.asyncio
    async def test_search_files_filters_to_expected_fields(self):
        p4 = MagicMock()
        p4.run.return_value = [
            {"depotFile": "//depot/a.py", "rev": "3", "type": "text", "change": "17", "extra": "x"},
            {"depotFile": "//depot/b.py", "rev": "5", "type": "binary", "change": "20"},
            "unexpected",
        ]
        service, manager = _service_with_p4(p4)

        result = await service.search_files("//depot/.../*.py", max_results=2, effective_user="alice")

        manager.get_connection.assert_called_once_with(effective_user="alice")
        p4.run.assert_called_once_with("files", "-m2", "//depot/.../*.py")
        assert result == {
            "status": "success",
            "message": [
                {"depotFile": "//depot/a.py", "rev": "3", "type": "text", "change": "17"},
                {"depotFile": "//depot/b.py", "rev": "5", "type": "binary", "change": "20"},
            ],
        }

    @pytest.mark.asyncio
    async def test_search_content_parses_and_truncates(self):
        p4 = MagicMock()
        p4.tagged = True
        p4.run.return_value = [
            "//depot/a.py#3:12: TODO: fix this",
            "//depot/b.py#7:33: TODO: another",
            "unparseable line",
        ]
        service, _ = _service_with_p4(p4)

        result = await service.search_content(
            search_text="TODO",
            depot_path="//depot/...",
            max_results=1,
            case_insensitive=True,
            show_line_numbers=True,
            filenames_only=False,
            effective_user="bob",
        )

        p4.run.assert_called_once_with("grep", "-n", "-i", "-e", "TODO", "//depot/...")
        assert p4.tagged is True
        assert result["status"] == "success"
        assert result["truncated"] is True
        assert result["message"] == [
            {
                "depotFile": "//depot/a.py",
                "revision": "3",
                "lineNumber": "12",
                "content": "TODO: fix this",
            }
        ]

    @pytest.mark.asyncio
    async def test_search_content_no_matches_returns_empty_success(self):
        p4 = MagicMock()
        p4.tagged = True
        p4.run.side_effect = P4Exception("no file(s) to match")
        service, _ = _service_with_p4(p4)

        result = await service.search_content(search_text="MISSING", depot_path="//depot/...")

        assert result == {"status": "success", "message": [], "truncated": False}

    @pytest.mark.asyncio
    async def test_list_directories_truncates_and_wraps_entries(self):
        p4 = MagicMock()
        p4.run.return_value = ["//depot/one", "//depot/two", "//depot/three"]
        service, _ = _service_with_p4(p4)

        result = await service.list_directories("//depot/*", max_results=2)

        assert result == {
            "status": "success",
            "message": [{"dir": "//depot/one"}, {"dir": "//depot/two"}],
            "truncated": True,
        }

    @pytest.mark.asyncio
    async def test_search_files_applies_server_side_cap(self):
        p4 = MagicMock()
        p4.run.return_value = []
        service, _ = _service_with_p4(p4, max_results_cap=5)

        await service.search_files("//depot/...", max_results=99)

        p4.run.assert_called_once_with("files", "-m5", "//depot/...")

    @pytest.mark.asyncio
    async def test_search_content_applies_server_side_cap(self):
        p4 = MagicMock()
        p4.tagged = True
        p4.run.return_value = [
            "//depot/a.py#1:1: TODO",
            "//depot/b.py#1:1: TODO",
            "//depot/c.py#1:1: TODO",
        ]
        service, _ = _service_with_p4(p4, max_results_cap=2)

        result = await service.search_content("TODO", max_results=99)

        assert len(result["message"]) == 2
        assert result["truncated"] is True

    @pytest.mark.asyncio
    async def test_list_directories_applies_server_side_cap(self):
        p4 = MagicMock()
        p4.run.return_value = ["//depot/one", "//depot/two", "//depot/three"]
        service, _ = _service_with_p4(p4, max_results_cap=2)

        result = await service.list_directories("//depot/*", max_results=99)

        assert result["message"] == [{"dir": "//depot/one"}, {"dir": "//depot/two"}]
        assert result["truncated"] is True

    @pytest.mark.asyncio
    async def test_search_files_returns_hint_for_missing_recursive_wildcard(self):
        p4 = MagicMock()
        p4.run.side_effect = P4Exception("no such file(s).")
        service, _ = _service_with_p4(p4)

        result = await service.search_files("//depot/bluetooth", max_results=20)

        assert result["status"] == "error"
        assert "Try '//depot/bluetooth/...'" in result["message"]
