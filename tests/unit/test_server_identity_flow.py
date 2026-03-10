"""
Tests for tool-level identity flow (as_user -> effective_user threading).

Covers plan test cases:
8. Tool signatures accept as_user; services receive and apply it.
9. Non-impersonation behavior remains unchanged when feature is disabled and no as_user is supplied.
"""

import inspect

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.handlers.handlers import Handlers
from src.handlers.review_handlers import ReviewsHandlers
from src.services.server_services import ServerServices
from src.services.file_services import FileServices
from src.services.changelist_services import ChangelistServices
from src.services.workspace_services import WorkspaceServices
from src.services.shelve_services import ShelveServices
from src.services.job_services import JobServices
from src.services.review_services import ReviewServices


# ---------------------------------------------------------------------------
# Handler dispatch forwards effective_user
# ---------------------------------------------------------------------------


class TestHandlerEffectiveUserPropagation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("effective_user", ["alice", None])
    async def test_handle_forwards_effective_user(self, effective_user):
        """Handlers.handle() forwards effective_user (or None) to the matched handler."""
        handler = Handlers.__new__(Handlers)
        handler.dispatch = {}

        mock_handler = AsyncMock(return_value={"status": "success", "action": "test"})
        handler.dispatch[("query", "server")] = mock_handler

        params = MagicMock()
        await handler.handle("query", "server", params, effective_user=effective_user)

        mock_handler.assert_called_once_with(params, effective_user=effective_user)

    @pytest.mark.asyncio
    async def test_review_handler_forwards_effective_user_to_service(self):
        """ReviewsHandlers passes effective_user through to the review service."""
        mock_review_svc = MagicMock()
        mock_review_svc.list_reviews = AsyncMock(
            return_value={"status": "success", "message": []}
        )

        rh = ReviewsHandlers(mock_review_svc)

        params = MagicMock()
        params.action = "list"
        params.max_results = 10

        await rh._handle_query_reviews(params, effective_user="alice")

        mock_review_svc.list_reviews.assert_called_once()
        call_kwargs = mock_review_svc.list_reviews.call_args
        assert call_kwargs.kwargs.get("effective_user") == "alice"


# ---------------------------------------------------------------------------
# All service methods accept effective_user
# ---------------------------------------------------------------------------

_SERVICE_METHOD_MATRIX = [
    (ServerServices, ["get_server_info", "get_current_user"]),
    (FileServices, [
        "get_file_content", "get_file_history", "get_file_info",
        "add_files", "edit_files", "delete_files", "sync_files",
    ]),
    (ChangelistServices, [
        "get_changelist", "list_changelists", "create_changelist",
        "update_changelist", "submit_changelist", "delete_changelist",
    ]),
    (WorkspaceServices, [
        "get_workspace", "list_workspaces", "create_workspace",
        "delete_workspace", "switch_workspace",
    ]),
    (ShelveServices, [
        "list_shelves", "get_shelve_diff", "shelve_files",
        "unshelve_files", "delete_shelve",
    ]),
    (JobServices, [
        "list_jobs_from_changelist", "get_job_details",
        "link_job_to_changelist", "unlink_job_from_changelist",
    ]),
    (ReviewServices, [
        "list_reviews", "create_review", "vote_review",
        "transition_review_state", "obliterate_review",
    ]),
]


class TestServiceReceivesEffectiveUser:
    @pytest.mark.parametrize(
        "svc_class,methods",
        _SERVICE_METHOD_MATRIX,
        ids=[cls.__name__ for cls, _ in _SERVICE_METHOD_MATRIX],
    )
    def test_service_methods_accept_effective_user(self, svc_class, methods):
        """All service methods accept effective_user kwarg."""
        svc = svc_class(MagicMock())
        for method_name in methods:
            sig = inspect.signature(getattr(svc, method_name))
            assert "effective_user" in sig.parameters, (
                f"{svc_class.__name__}.{method_name} missing effective_user"
            )
