"""Built-in tools: bash, read, write, edit, grep, find, ls, memory, files, spawn_subagents."""

from pi.tools.base import Tool
from pi.tools.bash import BashTool
from pi.tools.edit import EditTool
from pi.tools.files import FetchFileTool, ListFilesTool
from pi.tools.find import FindTool
from pi.tools.grep import GrepTool
from pi.tools.ls import LsTool
from pi.tools.memory import RecallTool, RememberTool
from pi.tools.read import ReadTool
from pi.tools.subagent import SpawnSubagentsTool
from pi.tools.web_search import WebSearchTool
from pi.tools.write import WriteTool


def all_tools(
    subagent_depth: int = 0,
    max_subagent_depth: int = 3,
) -> list[Tool]:
    return [
        BashTool(),
        ReadTool(),
        WriteTool(),
        EditTool(),
        GrepTool(),
        FindTool(),
        LsTool(),
        ListFilesTool(),
        FetchFileTool(),
        RememberTool(),
        RecallTool(),
        SpawnSubagentsTool(depth=subagent_depth, max_depth=max_subagent_depth),
        WebSearchTool(),
    ]


__all__ = [
    "all_tools",
    "Tool",
    "BashTool",
    "ReadTool",
    "WriteTool",
    "EditTool",
    "GrepTool",
    "FindTool",
    "LsTool",
    "ListFilesTool",
    "FetchFileTool",
    "SpawnSubagentsTool",
    "WebSearchTool",
]
