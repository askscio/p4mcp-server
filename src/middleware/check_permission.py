from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.exceptions import ToolError
import logging
from ..core.config import Config
from ..core.connection import P4ConnectionManager
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
        cache_key = effective_user or "__default__"
        current_time = time.time()

        # Per-user sub-cache stored as (timestamp, dict)
        entry = self._property_cache.get(cache_key)
        if entry is not None:
            ts, _ = entry
            if current_time - ts < self._cache_timeout:
                return  # Cache is still valid

        try:
            async with self.connection_manager.get_connection(
                effective_user=effective_user
            ) as p4:
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
        if context.fastmcp_context:
            try:
                if not self.connection_manager:
                    raise ToolError("P4 connection manager not initialized")

                tool = await context.fastmcp_context.fastmcp.get_tool(
                    context.message.name
                )
                tool_name = context.message.name

                # Parse tool information from tags
                tool_info = self._parse_tool_info_from_tags(tool_name, tool.tags)

                # ---- Impersonation policy enforcement ----
                arguments = (
                    context.message.arguments if context.message.arguments else {}
                )
                try:
                    self._check_impersonation_policy(tool_name, tool_info, arguments)
                except ImpersonationPolicyError as ipe:
                    logger.warning(
                        "Impersonation policy denied: tool=%s reason_code=%s msg=%s",
                        tool_name,
                        ipe.reason_code,
                        str(ipe),
                    )
                    raise ToolError(f"Policy denied: {ipe.reason_code} - {str(ipe)}")

                # Check global permissions
                await self._check_global_permissions(tool_info)

                # Check toolset permissions
                # await self._check_toolset_permissions(tool_info)

                # Check tool-specific permissions
                # await self._check_tool_permissions(tool_name, tool_info)

                # Check if tool is enabled
                if not tool.enabled:
                    raise ToolError("Tool is currently disabled")

                logger.info(
                    f"Permission check passed for {tool_name} "
                    f"({tool_info['operation_type']} on {tool_info['toolset']})"
                )

            except ToolError:
                raise
            except Exception as e:
                logger.error(f"Permission check failed: {e}")
                raise ToolError(f"Permission denied: {str(e)}")

        return await call_next(context)
