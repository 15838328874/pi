"""Record a structured plan for a multi-step task, and end the turn doing it."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from pi.models import Plan
from pi.tools.base import Tool, ToolContext, ToolResult, truncate


class SubmitPlanTool(Tool):
    name = "submit_plan"
    description = (
        "Record a plan for a multi-step task: a one-line title plus ordered, "
        "concrete steps. This tool is terminal - a successful call ends the turn "
        "immediately, so any other tool call in the same batch is skipped and never "
        "executes. Call it first and alone, then wait for the user's next message "
        "before doing the work."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": "One-line goal of the task.",
            },
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {"type": "string", "minLength": 1, "maxLength": 300},
                "description": "Ordered, concrete steps. Plain strings, no status field.",
            },
        },
        "required": ["title", "steps"],
    }
    terminal = True

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            plan = Plan.model_validate(args)
        except ValidationError as exc:
            # A 500-step submission dumps several KB of pydantic detail, and that
            # text goes straight into the model's history, so it gets cut.
            return ToolResult(
                content=f"Error: invalid plan: {truncate(str(exc), 500)}", is_error=True
            )

        # Kept under the loop's 200-char SSE preview so clients see all of it.
        return ToolResult(
            content=(
                f"Plan recorded ({len(plan.steps)} steps). This turn ends now; any "
                f"other tool call in this batch was skipped and did not run. Wait "
                f"for the user's next message."
            ),
            payload=plan,
        )
