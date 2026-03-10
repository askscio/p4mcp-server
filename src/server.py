import json
import logging
import uuid
from typing import Annotated, Optional, List, Literal
from pydantic import Field
from fastmcp import FastMCP, Context
from .core.config import Config
from .logging.global_logging import setup_logging
from .logging.session_logging import log_tool_call
from .core.connection import P4ConnectionManager

from .models import models as m
from .models import review_models as review_m
from .handlers.handlers import Handlers

from .services.file_services import FileServices
from .services.server_services import ServerServices
from .services.shelve_services import ShelveServices
from .services.workspace_services import WorkspaceServices
from .services.changelist_services import ChangelistServices
from .services.job_services import JobServices
from .services.review_services import ReviewServices

from .middleware.check_permission import CheckPermissionMiddleware

logger = logging.getLogger(__name__)


class ImpersonationConfigError(Exception):
    """Raised when impersonation configuration is invalid at startup."""

    pass


class P4MCPServer:
    """Perforce MCP Server with improved structure"""

    def __init__(
        self, session_id: str = None, readonly: bool = True, toolsets: list = []
    ):
        self.readonly = readonly
        self.toolsets = toolsets
        self.session_id = session_id

        setup_logging()
        self.p4config = Config.load()

        # ---- Startup fail-fast: impersonation preconditions ----
        self._validate_impersonation_config(self.p4config)

        self.p4_manager = P4ConnectionManager(self.p4config)

        if self.readonly:
            logger.info(
                "Running in read-only mode. No write operations will be allowed."
            )
        else:
            logger.info("Running in read-write mode. Write operations are enabled.")

        logger.info(
            f"Enabled toolsets: {', '.join(self.toolsets) if self.toolsets else 'None'}"
        )

        if self.p4config.impersonation_enabled:
            logger.info("Impersonation is ENABLED")

        self.mcp = FastMCP(
            "P4 MCP Server",
            middleware=[
                CheckPermissionMiddleware(self.p4_manager, config=self.p4config)
            ],
        )
        self._initialize_dependencies()

    @staticmethod
    def _validate_impersonation_config(config: Config) -> None:
        """Fail fast if impersonation config is inconsistent.

        Rules:
        - impersonation_enabled requires p4user to be set (superuser identity).
        """
        if not config.impersonation_enabled:
            return

        if not config.p4user:
            msg = (
                "Impersonation startup check failed: "
                "MCP_IMPERSONATION_ENABLED=true requires P4USER to be set (superuser account)"
            )
            logger.error(msg)
            raise ImpersonationConfigError(msg)

    def _initialize_dependencies(self) -> None:
        """Initialize all dependencies with proper error handling"""
        try:
            self._initialize_handlers()
            self._register_tools()
        except Exception as e:
            logger.error(f"Failed to initialize dependencies: {e}")
            raise

    def _initialize_handlers(self) -> None:
        """Initialize handlers with all services"""
        self.handlers = Handlers(
            server_services=ServerServices(self.p4_manager),
            workspace_services=WorkspaceServices(self.p4_manager),
            file_services=FileServices(self.p4_manager),
            changelist_services=ChangelistServices(self.p4_manager),
            shelve_services=ShelveServices(self.p4_manager),
            job_services=JobServices(self.p4_manager),
            review_services=ReviewServices(self.p4_manager),
        )

    def process_tool_logs(
        self,
        tool_name: str,
        result: dict,
        ctx: Context,
        as_user: str | None = None,
        reason_code: str | None = None,
    ) -> dict:
        """Process incoming data and emit a single audit summary event per tool invocation.

        Includes identity fields (actor_user, effective_user) and outcome context
        required by the impersonation audit contract.
        """
        request_id = str(uuid.uuid4())

        # Resolve actor from the connection manager's base identity.
        actor_user = self.p4_manager.actor_user if self.p4_manager else "unknown_actor"
        if actor_user == "unknown_actor":
            logger.warning(
                "audit: actor_user could not be resolved; using 'unknown_actor'"
            )

        effective_user = as_user or actor_user

        response = {
            "request_id": request_id,
            "session_id": self.session_id,
            "mcp_client": (
                ctx.session.client_params.clientInfo.name
                if ctx and ctx.session and ctx.session.client_params
                else "Unknown"
            ),
            "toolset": tool_name.split("_")[1] if "_" in tool_name else "unknown",
            "tool_name": tool_name,
            "tool_action": result.get("action", "unknown"),
            "status": result.get("status", "unknown"),
            "outcome": result.get("status", "unknown"),
            "actor_user": actor_user,
            "effective_user": effective_user,
            "p4_version": getattr(self.p4config, "p4version", "Unknown"),
        }

        if reason_code:
            response["reason_code"] = reason_code

        logger.info("tool_call: %s", json.dumps(response))

        if self.session_id:
            log_tool_call(response, session_id=self.session_id)

    @staticmethod
    def requires_approval(func, params):
        """Handle dangerous operations that require user approval"""
        logger.warning(
            f"Approval required for dangerous operation: {params.action} on {func.split('_')[1]}"
        )
        # Return a special response that forces MCP client to ask for approval
        return {
            "_meta": {
                "requires_approval": True,
                "approval_type": "dangerous_operation",
            },
            "type": "approval_required",
            "operation": f"delete_{func.split('_')[1]}",
            "message": f"⚠️ DANGER: This will permanently delete the {func.split('_')[1]}. \
                            This action cannot be undone. Next operation should not be executed until the user approves.",
            "details": {
                "source_tool": func,
                "action": params.action,
                "params": params.model_dump(),
            },
            "instruction": "User must explicitly approve this operation",
            "on_approval": "execute_delete",
        }

    def _register_tools(self):
        """Register read-only tools (always available)"""

        # Build as_user field description based on impersonation config.
        # When enabled, agents MUST know to always supply as_user.
        if self.p4config.impersonation_enabled:
            _as_user_desc = (
                "REQUIRED. The Perforce username to execute this action as. "
                "Every tool call MUST include this parameter with a valid Perforce username."
            )
            _impersonation_note = (
                "\n\nIMPORTANT: This server uses impersonation. "
                "You MUST pass the 'as_user' parameter with the Perforce username "
                "of the person you are acting on behalf of in EVERY tool call."
            )
        else:
            _as_user_desc = (
                "Impersonation target user. Only available when impersonation is enabled on the server; "
                "do NOT pass this parameter unless instructed to do so."
            )
            _impersonation_note = ""

        @self.mcp.tool(tags=["read", "server"], description="Get server info and current user information (READ permission)" + _impersonation_note)
        async def query_server(
            action: Annotated[
                Literal["server_info", "current_user"],
                Field(description="Get server info or current user information"),
            ],
            ctx: Context,
            as_user: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description=_as_user_desc,
                    examples=["alice", "bob"],
                ),
            ] = None,
        ) -> dict:
            """Get server info and current user information (READ permission)"""
            params = m.QueryServerParams(action=action)
            result = await self.handlers.handle(
                "query", "server", params, effective_user=as_user
            )
            self.process_tool_logs("query_server", result, ctx, as_user=as_user)
            return result

        @self.mcp.tool(
            tags=["read", "workspaces"], enabled="workspaces" in self.toolsets,
            description="Get workspace details, list workspaces, check type and status (READ permission)" + _impersonation_note,
        )
        async def query_workspaces(
            action: Annotated[
                Literal["list", "get", "type", "status"],
                Field(description="Workspace query action"),
            ],
            ctx: Context,
            as_user: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description=_as_user_desc,
                    examples=["alice", "bob"],
                ),
            ] = None,
            workspace_name: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description="Workspace name - required for get, type, status actions",
                    examples=["my_workspace"],
                ),
            ] = None,
            user: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description="Filter by user - optional for list action",
                    examples=["alice", "bob"],
                ),
            ] = None,
            max_results: Annotated[
                int,
                Field(
                    default=100,
                    ge=1,
                    le=1000,
                    description="Maximum number of results to return",
                ),
            ] = 100,
        ) -> dict:
            """Get workspace details, list workspaces, check type and status (READ permission)"""
            params = m.QueryWorkspacesParams(
                action=action,
                workspace_name=workspace_name,
                user=user,
                max_results=max_results,
            )
            result = await self.handlers.handle(
                "query", "workspaces", params, effective_user=as_user
            )
            self.process_tool_logs("query_workspaces", result, ctx, as_user=as_user)
            return result

        @self.mcp.tool(tags=["read", "files"], enabled="files" in self.toolsets, description="Get file content, history, info, diff, annotations (READ permission)" + _impersonation_note)
        async def query_files(
            action: Annotated[
                Literal[
                    "content", "history", "info", "metadata", "diff", "annotations"
                ],
                Field(
                    description="File query action, metadata includes extra information like optional attributes and file size"
                ),
            ],
            file_path: Annotated[
                str,
                Field(
                    description="Primary file path - required for all actions",
                    examples=["//depot/projectX/file.txt", "/local/path/file.txt"],
                ),
            ],
            ctx: Context,
            as_user: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description=_as_user_desc,
                    examples=["alice", "bob"],
                ),
            ] = None,
            file2: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description="Second file path - required for diff action",
                    examples=["//depot/projectX/file2.txt"],
                ),
            ] = None,
            diff2: Annotated[
                bool,
                Field(
                    default=True,
                    description="Use p4 diff2 for depot-to-depot diff, false for mixed diff",
                ),
            ] = True,
            max_results: Annotated[
                int,
                Field(
                    default=100,
                    ge=1,
                    le=1000,
                    description="Maximum number of results to return",
                ),
            ] = 100,
        ) -> dict:
            """Get file content, history, info, diff, annotations (READ permission)"""
            params = m.QueryFilesParams(
                action=action,
                file_path=file_path,
                file2=file2,
                diff2=diff2,
                max_results=max_results,
            )
            result = await self.handlers.handle(
                "query", "files", params, effective_user=as_user
            )
            self.process_tool_logs("query_files", result, ctx, as_user=as_user)
            return result

        @self.mcp.tool(
            tags=["read", "changelists"], enabled="changelists" in self.toolsets,
            description="Get changelist details and list changelists (READ permission)" + _impersonation_note,
        )
        async def query_changelists(
            action: Annotated[
                Literal["get", "list"], Field(description="Changelist query action")
            ],
            ctx: Context,
            as_user: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description=_as_user_desc,
                    examples=["alice", "bob"],
                ),
            ] = None,
            changelist_id: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description="Changelist ID - required for get action",
                    examples=["12345", "default"],
                ),
            ] = None,
            workspace_name: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description="Filter by workspace - for list action",
                    examples=["my_workspace"],
                ),
            ] = None,
            user: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description="Filter by user - for list action",
                    examples=["alice"],
                ),
            ] = None,
            status: Annotated[
                Optional[Literal["pending", "submitted"]],
                Field(default=None, description="Filter by status - for list action"),
            ] = None,
            depot_path: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description="Filter by depot path - for list action",
                    examples=["//depot/my_workspace/..."],
                ),
            ] = None,
            max_results: Annotated[
                int,
                Field(
                    default=100,
                    ge=1,
                    le=1000,
                    description="Maximum number of results to return",
                ),
            ] = 100,
        ) -> dict:
            """Get changelist details and list changelists (READ permission)"""
            params = m.QueryChangelistsParams(
                action=action,
                changelist_id=changelist_id,
                workspace_name=workspace_name,
                user=user,
                status=status,
                depot_path=depot_path,
                max_results=max_results,
            )
            result = await self.handlers.handle(
                "query", "changelists", params, effective_user=as_user
            )
            self.process_tool_logs("query_changelists", result, ctx, as_user=as_user)
            return result

        @self.mcp.tool(tags=["read", "shelves"], enabled="shelves" in self.toolsets, description="List shelves, get shelve diff and files (READ permission)" + _impersonation_note)
        async def query_shelves(
            action: Annotated[
                Literal["list", "diff", "files"],
                Field(description="Shelve query action"),
            ],
            ctx: Context,
            as_user: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description=_as_user_desc,
                    examples=["alice", "bob"],
                ),
            ] = None,
            changelist_id: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description="Changelist ID - required for diff and files actions",
                    examples=["12345"],
                ),
            ] = None,
            user: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description="Filter by user - for list action",
                    examples=["alice"],
                ),
            ] = None,
            max_results: Annotated[
                int,
                Field(
                    default=100,
                    ge=1,
                    le=1000,
                    description="Maximum number of results to return",
                ),
            ] = 100,
        ) -> dict:
            """List shelves, get shelve diff and files (READ permission)"""
            params = m.QueryShelvesParams(
                action=action,
                changelist_id=changelist_id,
                user=user,
                max_results=max_results,
            )
            result = await self.handlers.handle(
                "query", "shelves", params, effective_user=as_user
            )
            self.process_tool_logs("query_shelves", result, ctx, as_user=as_user)
            return result

        @self.mcp.tool(tags=["read", "jobs"], enabled="jobs" in self.toolsets, description="Get jobs from changelist and get job details (READ permission)" + _impersonation_note)
        async def query_jobs(
            action: Annotated[
                Literal["list_jobs", "get_job"], Field(description="Job query action")
            ],
            ctx: Context,
            as_user: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description=_as_user_desc,
                    examples=["alice", "bob"],
                ),
            ] = None,
            changelist_id: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description="Changelist ID - required for list_jobs action",
                    examples=["12345"],
                ),
            ] = None,
            job_id: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description="Job ID - required for get_job action",
                    examples=["job67890"],
                ),
            ] = None,
            max_results: Annotated[
                int,
                Field(
                    default=100,
                    ge=1,
                    le=1000,
                    description="Maximum number of results to return",
                ),
            ] = 100,
        ) -> dict:
            """Get jobs from changelist and get job details (READ permission)"""
            params = m.QueryJobsParams(
                action=action,
                changelist_id=changelist_id,
                job_id=job_id,
                max_results=max_results,
            )
            result = await self.handlers.handle(
                "query", "jobs", params, effective_user=as_user
            )
            self.process_tool_logs("query_jobs", result, ctx, as_user=as_user)
            return result

        @self.mcp.tool(tags=["read", "reviews"], enabled="reviews" in self.toolsets, description="Get review details and list reviews (READ permission). Open review - state is 'approved but pending=true' or 'needsReview' or 'needsRevision'. Closed review - state is 'approved but pending=false' or 'rejected' or 'archived'." + _impersonation_note)
        async def query_reviews(
            action: Annotated[
                Literal[
                    "list",
                    "dashboard",
                    "get",
                    "transitions",
                    "files_readby",
                    "files",
                    "comments",
                    "activity",
                ],
                Field(
                    description="Review query action: list all reviews, dashboard for current user, get specific review, transitions, files_readby, files, comments, activity"
                ),
            ],
            ctx: Context,
            as_user: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description=_as_user_desc,
                    examples=["alice", "bob"],
                ),
            ] = None,
            review_id: Annotated[
                Optional[int],
                Field(
                    default=None,
                    description="Review ID - required for get, transitions, files_readby, files, comments, activity actions",
                    examples=[12345, 67890],
                ),
            ] = None,
            review_fields: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description="Comma-separated list of fields to return for list/get actions",
                    examples=[
                        "id,description,author,state",
                        "id,author,state,participants,commits",
                    ],
                ),
            ] = None,
            comments_fields: Annotated[
                Optional[str],
                Field(
                    default="id,body,user,time",
                    description="Comma-separated list of fields to return for comments action",
                    examples=["id,body,user,time", "id,user,time"],
                ),
            ] = "id,body,user,time",
            up_voters: Annotated[
                Optional[List[str]],
                Field(
                    default=None,
                    description="List of up voters for transitions action",
                    examples=[["alice", "bob"]],
                ),
            ] = None,
            from_version: Annotated[
                Optional[int],
                Field(
                    default=None,
                    description="Starting version for files action",
                    examples=[1, 2],
                ),
            ] = None,
            to_version: Annotated[
                Optional[int],
                Field(
                    default=None,
                    description="Ending version for files action",
                    examples=[2, 3],
                ),
            ] = None,
            max_results: Annotated[
                int,
                Field(default=10, description="Maximum number of results to return"),
            ] = 10,
        ) -> dict:
            """Get review details and list reviews (READ permission).
            Open review - state is 'approved but pending=true' or 'needsReview' or 'needsRevision'.
            Closed review - state is 'approved but pending=false' or 'rejected' or 'archived'.
            """
            params = review_m.QueryReviewsParams(
                action=action,
                review_id=review_id,
                review_fields=review_fields,
                comments_fields=comments_fields,
                up_voters=up_voters,
                from_version=from_version,
                to_version=to_version,
                max_results=max_results,
            )
            result = await self.handlers.handle(
                "query", "reviews", params, effective_user=as_user
            )
            self.process_tool_logs("query_reviews", result, ctx, as_user=as_user)
            return result

        @self.mcp.tool(
            tags=["write", "workspaces"],
            enabled=not self.readonly and "workspaces" in self.toolsets,
            description="Create/delete workspace, Update workspace specs, and switch active workspace (WRITE permission)" + _impersonation_note,
        )
        async def modify_workspaces(
            action: Annotated[
                Literal["create", "delete", "update", "switch"],
                Field(description="Workspace modification action"),
            ],
            name: Annotated[
                str, Field(description="Workspace name", examples=["my_workspace"])
            ],
            ctx: Context,
            as_user: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description=_as_user_desc,
                    examples=["alice", "bob"],
                ),
            ] = None,
            specs: Annotated[
                Optional[dict],
                Field(
                    default=None,
                    description="Workspace specification with keys: Name, Root, Description, Options, LineEnd, View",
                    examples=[
                        {
                            "Name": "my_workspace",
                            "Root": "/path/to/root",
                            "View": ["//depot/... //my_workspace/..."],
                        }
                    ],
                ),
            ] = None,
        ) -> dict:
            """Create/delete workspace, Update workspace specs, and switch active workspace (WRITE permission)"""
            workspace_specs = m.WorkspaceSpec(**specs) if specs else None
            params = m.ModifyWorkspacesParams(
                action=action, name=name, specs=workspace_specs
            )
            if params.action == "delete":
                result = {
                    "status": "warning",
                    "action": "delete",
                    "message": "Requires approval to delete workspace.",
                }
                self.process_tool_logs(
                    "modify_workspaces", result, ctx, as_user=as_user
                )
                return self.requires_approval("modify_workspaces", params)
            result = await self.handlers.handle(
                "modify", "workspaces", params, effective_user=as_user
            )
            self.process_tool_logs("modify_workspaces", result, ctx, as_user=as_user)
            return result

        @self.mcp.tool(
            tags=["write", "files"],
            enabled=not self.readonly and "files" in self.toolsets,
            description="Add, edit, move, delete, revert, reconcile, resolve, and sync files (WRITE permission)" + _impersonation_note,
        )
        async def modify_files(
            action: Annotated[
                Literal[
                    "add",
                    "edit",
                    "delete",
                    "move",
                    "revert",
                    "reconcile",
                    "resolve",
                    "sync",
                ],
                Field(description="File modification action"),
            ],
            ctx: Context,
            as_user: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description=_as_user_desc,
                    examples=["alice", "bob"],
                ),
            ] = None,
            file_paths: Annotated[
                Optional[List[str]],
                Field(
                    default=None,
                    description="Full depot or client or local paths",
                    examples=[
                        [
                            "//depot/projectX/file1.txt",
                            "//workspace_name/projectX/file2.txt",
                        ]
                    ],
                ),
            ] = None,
            changelist: Annotated[
                str,
                Field(
                    default="default",
                    description="Changelist ID or 'default'",
                    examples=["default", "12345"],
                ),
            ] = "default",
            source_paths: Annotated[
                Optional[List[str]],
                Field(
                    default=None,
                    description="Source paths - required for move action",
                    examples=[["//depot/projectX/file1.txt"]],
                ),
            ] = None,
            target_paths: Annotated[
                Optional[List[str]],
                Field(
                    default=None,
                    description="Target paths - required for move action",
                    examples=[["//depot/projectX/file1_renamed.txt"]],
                ),
            ] = None,
            mode: Annotated[
                Literal["auto", "safe", "force", "preview", "theirs", "yours"],
                Field(
                    default="auto",
                    description="Resolve mode: auto(-am), safe(-as), force(-af), preview(-n), theirs(-at), yours(-ay)",
                ),
            ] = "auto",
            force: Annotated[
                bool,
                Field(default=False, description="Force operation - use with caution"),
            ] = False,
        ) -> dict:
            """Add, edit, move, delete, revert, reconcile, resolve, and sync files (WRITE permission)"""
            params = m.ModifyFilesParams(
                action=action,
                file_paths=file_paths,
                changelist=changelist,
                source_paths=source_paths,
                target_paths=target_paths,
                mode=mode,
                force=force,
            )
            if params.action == "delete":
                result = {
                    "status": "warning",
                    "action": "delete",
                    "message": "Requires approval to delete files.",
                }
                self.process_tool_logs("modify_files", result, ctx, as_user=as_user)
                return self.requires_approval("modify_files", params)
            result = await self.handlers.handle(
                "modify", "files", params, effective_user=as_user
            )
            self.process_tool_logs("modify_files", result, ctx, as_user=as_user)
            return result

        @self.mcp.tool(
            tags=["write", "changelists"],
            enabled=not self.readonly and "changelists" in self.toolsets,
            description="Create/delete changelists, update changelists and organize files/jobs (WRITE permission)" + _impersonation_note,
        )
        async def modify_changelists(
            action: Annotated[
                Literal["create", "update", "submit", "delete", "move_files"],
                Field(description="Changelist modification action"),
            ],
            ctx: Context,
            as_user: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description=_as_user_desc,
                    examples=["alice", "bob"],
                ),
            ] = None,
            changelist_id: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description="Changelist ID - required for most actions except create",
                    examples=["12345"],
                ),
            ] = None,
            description: Annotated[
                str,
                Field(
                    default="",
                    description="Changelist description - required for create, optional for update",
                    examples=["Fix bug in authentication module", "Add new feature X"],
                ),
            ] = "",
            file_paths: Annotated[
                Optional[List[str]],
                Field(
                    default=None,
                    description="File paths - required for move_files action",
                    examples=[["//depot/projectX/file1.txt"]],
                ),
            ] = None,
        ) -> dict:
            """Create/delete changelists, update changelists and organize files/jobs (WRITE permission)"""
            params = m.ModifyChangelistsParams(
                action=action,
                changelist_id=changelist_id,
                description=description,
                file_paths=file_paths,
            )
            if params.action == "delete":
                result = {
                    "status": "warning",
                    "action": "delete",
                    "message": "Requires approval to delete changelist.",
                }
                self.process_tool_logs(
                    "modify_changelists", result, ctx, as_user=as_user
                )
                return self.requires_approval("modify_changelists", params)
            result = await self.handlers.handle(
                "modify", "changelists", params, effective_user=as_user
            )
            self.process_tool_logs("modify_changelists", result, ctx, as_user=as_user)
            return result

        @self.mcp.tool(
            tags=["write", "shelves"],
            enabled=not self.readonly and "shelves" in self.toolsets,
            description="Create/delete, update shelves and unshelve files (WRITE permission)" + _impersonation_note,
        )
        async def modify_shelves(
            action: Annotated[
                Literal[
                    "shelve", "unshelve", "update", "delete", "unshelve_to_changelist"
                ],
                Field(description="Shelve modification action"),
            ],
            changelist_id: Annotated[
                str, Field(description="Changelist ID", examples=["12345"])
            ],
            ctx: Context,
            as_user: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description=_as_user_desc,
                    examples=["alice", "bob"],
                ),
            ] = None,
            file_paths: Annotated[
                Optional[List[str]],
                Field(
                    default=None,
                    description="File paths for shelve/unshelve/update/delete",
                    examples=[["//depot/projectX/file1.txt"]],
                ),
            ] = None,
            target_changelist: Annotated[
                str,
                Field(
                    default="default",
                    description="Target changelist for unshelve operations",
                    examples=["default", "54321"],
                ),
            ] = "default",
            force: Annotated[
                bool,
                Field(default=False, description="Force operation - use with caution"),
            ] = False,
        ) -> dict:
            """Create/delete, update shelves and unshelve files (WRITE permission)"""
            params = m.ModifyShelvesParams(
                action=action,
                changelist_id=changelist_id,
                file_paths=file_paths,
                target_changelist=target_changelist,
                force=force,
            )
            if params.action == "delete":
                result = {
                    "status": "warning",
                    "action": "delete",
                    "message": "Requires approval to delete shelve.",
                }
                self.process_tool_logs("modify_shelves", result, ctx, as_user=as_user)
                return self.requires_approval("modify_shelves", params)
            result = await self.handlers.handle(
                "modify", "shelves", params, effective_user=as_user
            )
            self.process_tool_logs("modify_shelves", result, ctx, as_user=as_user)
            return result

        @self.mcp.tool(
            tags=["write", "jobs"],
            enabled=not self.readonly and "jobs" in self.toolsets,
            description="Link or unlink jobs (WRITE permission)" + _impersonation_note,
        )
        async def modify_jobs(
            action: Annotated[
                Literal["link_job", "unlink_job"],
                Field(description="Job modification action"),
            ],
            changelist_id: Annotated[
                str,
                Field(
                    description="Changelist ID - required for link_job/unlink_job",
                    examples=["12345"],
                ),
            ],
            job_id: Annotated[
                str,
                Field(
                    description="Job ID - required for link_job/unlink_job",
                    examples=["job67890"],
                ),
            ],
            ctx: Context,
            as_user: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description=_as_user_desc,
                    examples=["alice", "bob"],
                ),
            ] = None,
        ) -> dict:
            """Link or unlink jobs (WRITE permission)"""
            params = m.ModifyJobsParams(
                action=action, changelist_id=changelist_id, job_id=job_id
            )
            result = await self.handlers.handle(
                "modify", "jobs", params, effective_user=as_user
            )
            self.process_tool_logs("modify_jobs", result, ctx, as_user=as_user)
            return result

        @self.mcp.tool(
            tags=["write", "reviews"],
            enabled=not self.readonly and "reviews" in self.toolsets,
            description="Create/update/delete reviews (WRITE permission)" + _impersonation_note,
        )
        async def modify_reviews(
            action: Annotated[
                Literal[
                    "create",
                    "refresh_projects",
                    "vote",
                    "transition",
                    "append_participants",
                    "add_comment",
                    "reply_comment",
                    "append_change",
                    "replace_with_change",
                    "join",
                    "archive_inactive",
                    "mark_comment_read",
                    "mark_comment_unread",
                    "mark_all_comments_read",
                    "mark_all_comments_unread",
                    "update_author",
                    "update_description",
                    "replace_participants",
                    "delete_participants",
                    "leave",
                    "obliterate",
                ],
                Field(description="Review modification action"),
            ],
            ctx: Context,
            as_user: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description=_as_user_desc,
                    examples=["alice", "bob"],
                ),
            ] = None,
            review_id: Annotated[
                Optional[int],
                Field(
                    default=None,
                    description="Review ID (required for most actions except create, archive_inactive)",
                    examples=[12345],
                ),
            ] = None,
            change_id: Annotated[
                Optional[int],
                Field(
                    default=None,
                    description="Changelist ID (required for create, append_change, replace_with_change)",
                    examples=[67890],
                ),
            ] = None,
            description: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description="Review description (optional on create)",
                    examples=["Implement feature X"],
                ),
            ] = None,
            reviewers: Annotated[
                Optional[List[str]],
                Field(
                    default=None,
                    description="List of reviewers",
                    examples=[["alice", "bob"]],
                ),
            ] = None,
            required_reviewers: Annotated[
                Optional[List[str]],
                Field(
                    default=None,
                    description="List of required reviewers",
                    examples=[["carol"]],
                ),
            ] = None,
            reviewer_groups: Annotated[
                Optional[List[dict]],
                Field(
                    default=None,
                    description="Reviewer groups",
                    examples=[[{"name": "Developers", "required": "true"}]],
                ),
            ] = None,
            context: Annotated[
                Optional[dict],
                Field(
                    default=None,
                    description="Comment context: file, leftLine, rightLine, content, version, attribute, comment",
                    examples=[
                        {
                            "file": "//depot/path/file.txt",
                            "rightLine": 42,
                            "leftLine": 40,
                        }
                    ],
                ),
            ] = None,
            vote_value: Annotated[
                Optional[Literal["up", "down", "clear"]],
                Field(default=None, description="Vote value"),
            ] = None,
            version: Annotated[
                Optional[int],
                Field(
                    default=None,
                    description="Review version (optional for vote)",
                    examples=[2],
                ),
            ] = None,
            transition: Annotated[
                Optional[
                    Literal[
                        "needsRevision",
                        "needsReview",
                        "approved",
                        "committed",
                        "approved:commit",
                        "rejected",
                        "archived",
                    ]
                ],
                Field(default=None, description="Transition target state"),
            ] = None,
            jobs: Annotated[
                Optional[List[str]],
                Field(
                    default=None,
                    description="Associated job IDs for transition",
                    examples=[["job000123", "job000456"]],
                ),
            ] = None,
            fix_status: Annotated[
                Optional[Literal["open", "closed"]],
                Field(default=None, description="Job fix status when transitioning"),
            ] = None,
            cleanup: Annotated[
                Optional[bool],
                Field(
                    default=None,
                    description="Perform cleanup for approved:commit/committed transitions",
                ),
            ] = None,
            users: Annotated[
                Optional[dict],
                Field(
                    default=None,
                    description="Usernames for append/replace/delete participants",
                    examples=[
                        {"alice": {"required": "yes"}, "bob": {"required": "no"}}
                    ],
                ),
            ] = None,
            groups: Annotated[
                Optional[dict],
                Field(
                    default=None,
                    description="Group names for append/replace/delete participants",
                    examples=[{"dev-team": {"required": "all"}}],
                ),
            ] = None,
            body: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description="Comment body (required for add_comment, reply_comment)",
                    examples=["Looks good."],
                ),
            ] = None,
            task_state: Annotated[
                Optional[Literal["open", "comment"]],
                Field(default=None, description="Task state"),
            ] = None,
            notify: Annotated[
                Optional[Literal["immediate", "delayed"]],
                Field(default=None, description="Notification mode"),
            ] = None,
            comment_id: Annotated[
                Optional[int],
                Field(
                    default=None,
                    description="Parent comment ID (reply_comment, mark_comment_read/unread)",
                    examples=[987],
                ),
            ] = None,
            not_updated_since: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description="ISO date (YYYY-MM-DD) threshold for archive_inactive",
                    examples=["2024-01-15"],
                ),
            ] = None,
            max_reviews: Annotated[
                int,
                Field(
                    default=0,
                    description="Maximum number of inactive reviews to archive (0 = no limit)",
                ),
            ] = 0,
            new_author: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description="New author username (update_author)",
                    examples=["dave"],
                ),
            ] = None,
            new_description: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description="New review description (update_description)",
                    examples=["Refined implementation details."],
                ),
            ] = None,
        ) -> dict:
            """Create/update/delete reviews (WRITE permission)"""
            comment_context = review_m.CommentContext(**context) if context else None
            params = review_m.ModifyReviewsParams(
                action=action,
                review_id=review_id,
                change_id=change_id,
                description=description,
                reviewers=reviewers,
                required_reviewers=required_reviewers,
                reviewer_groups=reviewer_groups,
                context=comment_context,
                vote_value=vote_value,
                version=version,
                transition=transition,
                jobs=jobs,
                fix_status=fix_status,
                cleanup=cleanup,
                users=users,
                groups=groups,
                body=body,
                task_state=task_state,
                notify=notify,
                comment_id=comment_id,
                not_updated_since=not_updated_since,
                max_reviews=max_reviews,
                new_author=new_author,
                new_description=new_description,
            )
            if params.action == "delete":
                result = {
                    "status": "warning",
                    "action": "delete",
                    "message": "Requires approval to delete review.",
                }
                self.process_tool_logs("modify_reviews", result, ctx, as_user=as_user)
                return self.requires_approval("modify_reviews", params)
            result = await self.handlers.handle(
                "modify", "reviews", params, effective_user=as_user
            )
            self.process_tool_logs("modify_reviews", result, ctx, as_user=as_user)
            return result

        @self.mcp.tool(
            tags=["write", "delete"],
            enabled=not self.readonly and len(set(self.toolsets) - {"jobs"}) > 0,
            description="Execute any approved delete operation from any tool (WRITE permission)" + _impersonation_note,
        )
        async def execute_delete(
            source_tool: Annotated[
                Literal[
                    "modify_changelists",
                    "modify_workspaces",
                    "modify_files",
                    "modify_shelves",
                    "modify_reviews",
                ],
                Field(
                    description="The source tool that initiated the delete operation"
                ),
            ],
            action: Annotated[
                Literal["delete"],
                Field(description="The action to execute - must be 'delete'"),
            ],
            ctx: Context,
            as_user: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description=_as_user_desc,
                    examples=["alice", "bob"],
                ),
            ] = None,
            changelist_id: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description="Changelist ID - required for changelist and shelve operations",
                    examples=["12345", "default"],
                ),
            ] = None,
            workspace_name: Annotated[
                Optional[str],
                Field(
                    default=None,
                    description="Workspace name - required for workspace operations",
                    examples=["my_workspace"],
                ),
            ] = None,
            file_paths: Annotated[
                Optional[List[str]],
                Field(
                    default=None,
                    description="File paths - required for file operations",
                    examples=[["//depot/projectX/file1.txt"]],
                ),
            ] = None,
            review_id: Annotated[
                Optional[int],
                Field(
                    default=None,
                    description="Review ID - required for review operations",
                    examples=[12345],
                ),
            ] = None,
            operation_id: Annotated[
                Optional[str],
                Field(default=None, description="Unique ID for the delete operation"),
            ] = None,
            user_confirmed: Annotated[
                bool,
                Field(
                    default=False,
                    description="Whether the user has confirmed the delete operation",
                ),
            ] = False,
        ) -> dict:
            """Execute any approved delete operation from any tool (WRITE permission)"""
            params = m.ExecuteDeleteParams(
                source_tool=source_tool,
                action=action,
                changelist_id=changelist_id,
                workspace_name=workspace_name,
                file_paths=file_paths,
                review_id=review_id,
                operation_id=operation_id,
                user_confirmed=user_confirmed,
            )
            if params.source_tool.split("_")[1] not in self.toolsets:
                result = {
                    "status": "error",
                    "action": "delete",
                    "message": f"Toolset not allowed: {params.source_tool.split('_')[1]}",
                }
                self.process_tool_logs("execute_delete", result, ctx, as_user=as_user)
                return result

            if params.source_tool == "modify_workspaces":
                workspace_params = m.ModifyWorkspacesParams(
                    action="delete", name=params.workspace_name
                )
                result = await self.handlers.handle(
                    "modify", "workspaces", workspace_params, effective_user=as_user
                )
            elif params.source_tool == "modify_changelists":
                changelist_params = m.ModifyChangelistsParams(
                    action="delete", changelist_id=params.changelist_id
                )
                result = await self.handlers.handle(
                    "modify", "changelists", changelist_params, effective_user=as_user
                )
            elif params.source_tool == "modify_files":
                file_params = m.ModifyFilesParams(
                    action="delete", file_paths=params.file_paths
                )
                result = await self.handlers.handle(
                    "modify", "files", file_params, effective_user=as_user
                )
            elif params.source_tool == "modify_shelves":
                shelve_params = m.ModifyShelvesParams(
                    action="delete",
                    changelist_id=params.changelist_id,
                    file_paths=params.file_paths,
                )
                result = await self.handlers.handle(
                    "modify", "shelves", shelve_params, effective_user=as_user
                )
            elif params.source_tool == "modify_reviews":
                review_params = review_m.ModifyReviewsParams(
                    action="delete", review_id=params.review_id
                )
                result = await self.handlers.handle(
                    "modify", "reviews", review_params, effective_user=as_user
                )
            else:
                result = {
                    "status": "error",
                    "action": "delete",
                    "message": f"Unknown source tool: {params.source_tool}",
                }

            self.process_tool_logs("execute_delete", result, ctx, as_user=as_user)
            return result

    def run(self, transport="stdio", **kwargs):
        """Run the MCP server"""
        self.mcp.run(transport=transport, **kwargs)
