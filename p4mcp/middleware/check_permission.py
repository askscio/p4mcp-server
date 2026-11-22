from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.exceptions import ToolError
import logging
from ..core.config import Config
from ..core.connection import (
    P4ConnectionManager,
    get_effective_user,
    reset_effective_user,
    set_effective_user,
)
from .impersonation_policy import ImpersonationReasonCode, ImpersonationPolicyError
import time

logger = logging.getLogger(__name__)


class CheckPermissionMiddleware(Middleware):
    """Middleware to check tool permissions based on P4 properties"""

    def __init__(
        self, connection_manager: P4ConnectionManager, config: Config | None = None
    ):
        super().__init__()
        self.connection_manager = connection_manager
        self.config = config
        self._property_cache = {}
        self._cache_timeout = 60  # 1 minute

    def _parse_tool_info_from_tags(self, tool_name: str, tags: list) -> dict:
        """Extract tool information from tags instead of parsing tool name"""
        tool_info = {
            "operation_type": "read",  # Default to read
            "toolset": "unknown",
            "is_write_operation": False,
            "is_delete_operation": False,
        }

        # Extract operation type and toolset from tags
        for tag in tags:
            if tag in ["read", "write", "delete"]:
                tool_info["operation_type"] = tag
                tool_info["is_write_operation"] = tag in ["write", "delete"]
                tool_info["is_delete_operation"] = tag == "delete"
            elif tag in [
                "server",
                "files",
                "workspaces",
                "changelists",
                "shelves",
                "jobs",
                "reviews",
                "streams",
                "p4dam",
            ]:
                tool_info["toolset"] = tag

        # Fallback to parsing tool name if tags don't provide enough info
        if tool_info["toolset"] == "unknown" and "_" in tool_name:
            tool_info["toolset"] = tool_name.split("_")[1]

        return tool_info

    async def _refresh_properties_cache(self, effective_user: str | None = None):
        """Fetch all properties and cache them.

        When *effective_user* is provided the cache is keyed per-user so that
        property lookups for different impersonated users remain isolated.
        """
        effective_user = effective_user or get_effective_user()
        cache_key = effective_user or "__default__"
        current_time = time.time()

        # Per-user sub-cache stored as (timestamp, dict)
        entry = self._property_cache.get(cache_key)
        if entry is not None:
            ts, _ = entry
            if current_time - ts < self._cache_timeout:
                return  # Cache is still valid

        try:
            connection = self.connection_manager.get_connection(
                effective_user=effective_user
            )
            async with connection as p4:
                if not p4:
                    self._property_cache[cache_key] = (current_time, {})
                    return
                result = p4.run("property", "-l")
                props = {prop["name"]: prop.get("value", "").strip() for prop in result}
                self._property_cache[cache_key] = (current_time, props)
        except Exception as e:
            logger.warning(f"Failed to refresh property cache: {e}")
            self._property_cache[cache_key] = (current_time, {})

    async def _get_property_value(
        self, property_name, effective_user: str | None = None
    ):
        """Get property value from cached properties"""
        await self._refresh_properties_cache(effective_user)
        cache_key = effective_user or "__default__"
        entry = self._property_cache.get(cache_key)
        if entry is None:
            return None
        _, props = entry
        return props.get(property_name)

    async def _check_global_permissions(self, tool_info: dict):
        """Check global MCP permissions based on operation type from tags"""
        mcp_enabled = await self._get_property_value("mcp.enabled")
        if mcp_enabled is not None and mcp_enabled.lower() == "false":
            raise ToolError("P4 MCP server is disabled by the administrator")

        if tool_info["is_write_operation"]:
            global_write = await self._get_property_value("mcp.toolsets.write")
            if global_write is not None and global_write.lower() == "false":
                raise ToolError("Write operations are disabled by the administrator")

        return True

    async def _check_toolset_permissions(self, tool_info: dict):
        """Check toolset-specific permissions using tag-based toolset"""
        toolset = tool_info["toolset"]

        allowed_toolsets = await self._get_property_value("mcp.toolsets.allowed")
        if allowed_toolsets:
            allowed_list = [ts.strip() for ts in allowed_toolsets.split(",")]
            if toolset not in allowed_list:
                raise ToolError(f"Toolset '{toolset}' is disabled by the administrator")

        toolset_enabled = await self._get_property_value(
            f"mcp.toolset.{toolset}.enabled"
        )
        if toolset_enabled is not None and toolset_enabled.lower() == "false":
            raise ToolError(f"Toolset '{toolset}' is disabled by the administrator")

        if tool_info["is_write_operation"]:
            toolset_write = await self._get_property_value(
                f"mcp.toolset.{toolset}.write"
            )
            if toolset_write is not None and toolset_write.lower() == "false":
                raise ToolError(
                    f"Write operations disabled for toolset '{toolset}' by the administrator"
                )

        return True

    async def _check_tool_permissions(self, tool_name, tool_info: dict):
        """Check specific tool permissions using tag-based toolset"""
        toolset = tool_info["toolset"]

        allowed_tools = await self._get_property_value(f"mcp.toolset.{toolset}.tools")
        if allowed_tools:
            allowed_list = [tool.strip() for tool in allowed_tools.split(",")]
            if tool_name not in allowed_list:
                raise ToolError(f"Tool '{tool_name}' is disabled by the administrator")

        return True

    def _check_impersonation_policy(
        self, tool_name: str, tool_info: dict, arguments: dict
    ) -> str | None:
        """Enforce impersonation policy and return the effective user (or ``None``).

        Raises ``ImpersonationPolicyError`` on policy violation.
        """
        if self.config is None:
            # No config means legacy mode – skip impersonation checks entirely.
            return None

        if tool_info.get("toolset") == "p4dam":
            # P4 DAM tools use the DAM API identity, not the P4 connection.
            return None

        raw_as_user: str | None = arguments.get("as_user")

        # Normalize: reject empty/whitespace-only, then strip.
        as_user: str | None = None
        if raw_as_user is not None:
            stripped = raw_as_user.strip()
            if stripped == "":
                raise ImpersonationPolicyError(
                    f"as_user cannot be empty or whitespace-only (tool '{tool_name}')",
                    ImpersonationReasonCode.AS_USER_INVALID,
                )
            as_user = stripped

        impersonation_enabled = self.config.impersonation_enabled

        if impersonation_enabled:
            # All P4-backed tools require as_user when impersonation is on.
            if as_user is None:
                raise ImpersonationPolicyError(
                    f"as_user is required for tool '{tool_name}' when impersonation is enabled",
                    ImpersonationReasonCode.AS_USER_REQUIRED,
                )
            return as_user
        else:
            # Impersonation disabled – providing as_user is a hard error.
            if as_user is not None:
                raise ImpersonationPolicyError(
                    f"as_user is not allowed when impersonation is disabled (tool '{tool_name}')",
                    ImpersonationReasonCode.IMPERSONATION_DISABLED,
                )
            return None

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        """Check permissions before executing a tool"""
        if not context.fastmcp_context:
            return await call_next(context)

        tool_name = context.message.name
        if tool_name in {"search_tools", "regex_search_tools", "semantic_search_tools"}:
            return await call_next(context)

        token = None
        try:
            if not self.connection_manager:
                raise ToolError("P4 connection manager not initialized")

            fastmcp = context.fastmcp_context.fastmcp
            tool = await fastmcp.get_tool(tool_name)
            tool_info = self._parse_tool_info_from_tags(tool_name, tool.tags or [])
            arguments = context.message.arguments or {}

            # FastMCP's search transform invokes the selected native tool via
            # a proxy. Validate and scope the nested call using its tool schema.
            if tool_name in {"call_tool", "regex_call_tool", "semantic_call_tool"}:
                target_name = arguments.get("name")
                target_arguments = arguments.get("arguments") or {}
                if target_name:
                    target_tool = await fastmcp.get_tool(target_name)
                    tool_info = self._parse_tool_info_from_tags(
                        target_name, target_tool.tags or []
                    )
                    tool_name = target_name
                    arguments = target_arguments

            try:
                effective_user = self._check_impersonation_policy(
                    tool_name, tool_info, arguments
                )
            except ImpersonationPolicyError as ipe:
                logger.warning(
                    "Impersonation policy denied: tool=%s reason_code=%s msg=%s",
                    tool_name,
                    ipe.reason_code,
                    str(ipe),
                )
                raise ToolError(f"Policy denied: {ipe.reason_code} - {str(ipe)}")

            # Pass the effective Perforce username downstream and scope every
            # connection created during this request to that user.
            if effective_user is not None:
                arguments["as_user"] = effective_user
            token = set_effective_user(effective_user)

            await self._check_global_permissions(tool_info)
            await self._check_toolset_permissions(tool_info)
            await self._check_tool_permissions(tool_name, tool_info)

            logger.info(
                "Permission check passed for %s (%s on %s)",
                tool_name,
                tool_info["operation_type"],
                tool_info["toolset"],
            )
            return await call_next(context)
        except ToolError:
            raise
        except Exception as e:
            logger.error("Permission check failed: %s", e)
            raise ToolError(f"Permission denied: {str(e)}")
        finally:
            if token is not None:
                reset_effective_user(token)
