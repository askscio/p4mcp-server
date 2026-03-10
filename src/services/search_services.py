"""
P4 search services layer

Read service for tools:
- search_files : Search files by depot path pattern
- search_content : Search file contents with grep
- list_directories : List depot directories by pattern
"""

import logging
import re

from P4 import P4Exception

from ..core.connection import P4ConnectionManager
from ..core.config import DEFAULT_SEARCH_MAX_RESULTS_CAP

logger = logging.getLogger(__name__)

# Handles:
#   //depot/path/file.ext#3:12: matching content
#   //depot/path/file.ext#3
_GREP_LINE_RE = re.compile(r"^(.+?)#(\d+)(?::(\d+):)?\s*(.*)$")


class SearchServices:
    """Service for depot search operations."""

    def __init__(
        self,
        connection_manager: P4ConnectionManager,
        max_results_cap: int = DEFAULT_SEARCH_MAX_RESULTS_CAP,
    ):
        self.connection_manager = connection_manager
        self.max_results_cap = max_results_cap

    def _effective_max_results(self, requested_max_results: int) -> int:
        return min(requested_max_results, self.max_results_cap)

    async def search_files(
        self,
        depot_path: str,
        max_results: int = 100,
        effective_user: str | None = None,
    ) -> dict:
        """Search files by depot path pattern using p4 files."""
        async with self.connection_manager.get_connection(
            effective_user=effective_user
        ) as p4:
            try:
                effective_max_results = self._effective_max_results(max_results)
                results = p4.run("files", f"-m{effective_max_results}", depot_path)
                files = [
                    {
                        "depotFile": item.get("depotFile"),
                        "rev": item.get("rev"),
                        "type": item.get("type"),
                        "change": item.get("change"),
                    }
                    for item in results
                    if isinstance(item, dict)
                ]
                return {"status": "success", "message": files}
            except P4Exception as e:
                logger.error(f"P4Error: Failed to search files: {e}")
                return {"status": "error", "message": str(e)}

    async def search_content(
        self,
        search_text: str,
        depot_path: str = "//...",
        max_results: int = 100,
        case_insensitive: bool = False,
        show_line_numbers: bool = True,
        filenames_only: bool = False,
        effective_user: str | None = None,
    ) -> dict:
        """Search file content using p4 grep."""
        async with self.connection_manager.get_connection(
            effective_user=effective_user
        ) as p4:
            try:
                effective_max_results = self._effective_max_results(max_results)
                args = []
                if show_line_numbers:
                    args.append("-n")
                if case_insensitive:
                    args.append("-i")
                if filenames_only:
                    args.append("-l")
                args.extend(["-e", search_text, depot_path])

                original_tagged = p4.tagged
                p4.tagged = False
                try:
                    raw_results = p4.run("grep", *args)
                finally:
                    p4.tagged = original_tagged

                parsed = []
                for line in raw_results:
                    if not isinstance(line, str):
                        continue
                    match = _GREP_LINE_RE.match(line)
                    if not match:
                        continue

                    entry = {"depotFile": match.group(1), "revision": match.group(2)}
                    if match.group(3) is not None:
                        entry["lineNumber"] = match.group(3)
                    if match.group(4):
                        entry["content"] = match.group(4)
                    parsed.append(entry)

                truncated = len(parsed) > effective_max_results
                return {
                    "status": "success",
                    "message": parsed[:effective_max_results],
                    "truncated": truncated,
                }
            except P4Exception as e:
                if "no file(s) to match" in str(e):
                    return {"status": "success", "message": [], "truncated": False}
                logger.error(f"P4Error: Failed to search content: {e}")
                return {"status": "error", "message": str(e)}

    async def list_directories(
        self,
        depot_path: str,
        max_results: int = 100,
        effective_user: str | None = None,
    ) -> dict:
        """List directories matching a depot path pattern using p4 dirs."""
        async with self.connection_manager.get_connection(
            effective_user=effective_user
        ) as p4:
            try:
                effective_max_results = self._effective_max_results(max_results)
                results = p4.run("dirs", depot_path)
                truncated = len(results) > effective_max_results
                dirs = []
                for entry in results[:effective_max_results]:
                    if isinstance(entry, str):
                        dirs.append({"dir": entry})
                    elif isinstance(entry, dict):
                        dirs.append({"dir": entry.get("dir")})
                return {
                    "status": "success",
                    "message": dirs,
                    "truncated": truncated,
                }
            except P4Exception as e:
                logger.error(f"P4Error: Failed to list directories: {e}")
                return {"status": "error", "message": str(e)}
