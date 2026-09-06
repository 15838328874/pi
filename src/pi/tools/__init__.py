"""Built-in tools: bash, read, write, edit, grep, find, ls, submit_plan.

Internet access is NOT a local tool: the model endpoint's own capabilities
(enable_search / web_search / web_extractor / code_interpreter) cover it, so
pi.tools stays workspace-only.
"""

from pi.tools.base import Tool
from pi.tools.bash import BashTool
from pi.tools.edit import EditTool
from pi.tools.find import FindTool
from pi.tools.grep import GrepTool
from pi.tools.ls import LsTool
from pi.tools.plan import SubmitPlanTool
from pi.tools.read import ReadTool
from pi.tools.write import WriteTool


def all_tools() -> list[Tool]:
    return [
        BashTool(),
        ReadTool(),
        WriteTool(),
        EditTool(),
        GrepTool(),
        FindTool(),
        LsTool(),
        SubmitPlanTool(),
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
    "SubmitPlanTool",
]
