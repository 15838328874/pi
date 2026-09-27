"""Skills: reusable capability packages (SKILL.md + optional scripts).

Directory format (PI_SKILLS_DIR points at the root):
  skills/
    code-review/
      SKILL.md          # frontmatter (name / description) + instruction body
      scripts/
        check_diff.py   # one script = one tool

Two seams:
- Tools: ``use_skill`` (progressive loading of the full instructions) plus one
  tool per script (``skill_<skill>_<script>``), executed through ctx.runner so
  script code runs inside the sandbox like every other command.
- Prompt: a compact index (name + one-line description, no body) is injected
  into the system prompt by the runner, mirroring the memory injection.

Skill scripts live outside the workspace, which is all the sandbox mounts -
each execution copies the script into <workspace>/.pi-skills/ first (idempotent,
cheap), then runs it with the workspace as cwd (works for local and docker).
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pi.tools.base import Tool, ToolContext, ToolResult, truncate
from pi.tools.registry import ToolProvider
from pi.tools.sandbox import LocalRunner

log = logging.getLogger("pi.tools.skill")

_NAME_RE = re.compile(r"[^a-z0-9_]+")


@dataclass
class Skill:
    name: str
    description: str
    body: str
    dir: Path
    scripts: list[str] = field(default_factory=list)  # filenames under scripts/


class SkillToolProvider(ToolProvider):
    name = "skills"

    def __init__(self, skill_dirs: list[Path]):
        self.skill_dirs = skill_dirs
        self.skills: dict[str, Skill] = {}
        self._load()

    def _load(self) -> None:
        for root in self.skill_dirs:
            if not root.is_dir():
                log.warning("skills dir not found: %s", root)
                continue
            for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
                try:
                    skill = _parse_skill(skill_dir)
                except Exception as exc:  # noqa: BLE001 - one bad skill must not break the rest
                    log.warning("skill %s failed to load: %s", skill_dir.name, exc)
                    continue
                if skill.name in self.skills:
                    log.warning("duplicate skill %r ignored", skill.name)
                    continue
                self.skills[skill.name] = skill

    def index(self) -> str:
        """Compact index for prompt injection: names + one-line descriptions."""
        if not self.skills:
            return ""
        return "\n".join(f"- {s.name}: {s.description}" for s in self.skills.values())

    async def tools(self) -> list[Tool]:
        tools: list[Tool] = [UseSkillTool(self.skills)]
        for skill in self.skills.values():
            for script in skill.scripts:
                tools.append(SkillScriptTool(skill, script))
        return tools


class UseSkillTool(Tool):
    name = "use_skill"
    description = (
        "Load a skill's full instructions (its SKILL.md body) by name. Use this "
        "before running any skill_*_* script tool so you follow the skill's rules."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "skill name from the available-skills index"}
        },
        "required": ["skill"],
    }

    def __init__(self, skills: dict[str, Skill]) -> None:
        self.skills = skills

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("skill", "")).strip()
        skill = self.skills.get(name)
        if skill is None:
            return ToolResult(content=f"Error: unknown skill {name!r}", is_error=True)
        return ToolResult(content=truncate(skill.body, ctx.max_output))


class SkillScriptTool(Tool):
    """One skill script = one tool; runs through ctx.runner (the sandbox)."""

    def __init__(self, skill: Skill, script: str) -> None:
        stem = Path(script).stem
        self.name = f"skill_{skill.name}_{stem}"
        self.description = f"[skill {skill.name}] run scripts/{script} (sandboxed; use use_skill first)"
        self.input_schema = {
            "type": "object",
            "properties": {
                "args": {
                    "type": "string",
                    "description": "extra CLI arguments, appended to the command line as-is",
                },
                "timeout": {
                    "type": "integer",
                    "description": "timeout in seconds (default 120, max 600)",
                },
            },
        }
        self._skill = skill
        self._script = script

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # The sandbox only mounts the workspace, so stage the script inside it
        # first (idempotent copy, small file).
        src = self._skill.dir / "scripts" / self._script
        dst_dir = ctx.cwd / ".pi-skills" / self._skill.name
        await asyncio.to_thread(dst_dir.mkdir, parents=True, exist_ok=True)
        dst = dst_dir / self._script
        await asyncio.to_thread(shutil.copy2, src, dst)

        extra = str(args.get("args", "") or "").strip()
        timeout = max(1, min(int(args.get("timeout", 120) or 120), 600))
        rel = f".pi-skills/{self._skill.name}/{self._script}"
        # Same trust level as the bash tool: the model controls the command line
        # either way, and the script runs inside the workspace sandbox.
        if self._script.endswith(".sh"):
            command = f"bash {rel} {extra}".rstrip()
        else:
            command = f"python3 {rel} {extra}".rstrip()

        runner = ctx.runner if ctx.runner is not None else LocalRunner()
        result = await runner.run(command, ctx.cwd, timeout)
        if result.exit_code != 0 and result.output.startswith("Error:"):
            return ToolResult(content=truncate(result.output, ctx.max_output), is_error=True)
        return ToolResult(
            content=truncate(f"{result.output}\n\n(exit code: {result.exit_code})", ctx.max_output)
        )


def _parse_skill(skill_dir: Path) -> Skill:
    """Parse SKILL.md: frontmatter block (name/description) then the body."""
    md = skill_dir / "SKILL.md"
    if not md.is_file():
        raise ValueError("SKILL.md missing")
    text = md.read_text(encoding="utf-8")
    name, description, body = skill_dir.name, "", text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            front, body = text[:end], text[end + 4 :].lstrip("\n")
            for line in front.splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    key, value = key.strip(), value.strip()
                    if key == "name":
                        name = value
                    elif key == "description":
                        description = value
    name = _NAME_RE.sub("_", name.strip()) or skill_dir.name
    scripts = sorted(
        p.name for p in (skill_dir / "scripts").iterdir() if p.is_file()
    ) if (skill_dir / "scripts").is_dir() else []
    return Skill(name=name, description=description, body=body, dir=skill_dir, scripts=scripts)
