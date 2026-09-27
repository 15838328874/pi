"""Context compaction: summarize older history via the LLM, keep recent messages.

Mirrors pi-coding-agent's compaction module. Trigger: estimated conversation
size (chars) exceeds a threshold. The older messages are replaced by a single
summary message; the most recent `keep_last` messages stay verbatim.
"""

from __future__ import annotations

from pi.llm.base import LLMProvider, StreamEnd, TextDelta
from pi.models import Message, Role, TextBlock, ToolCallBlock, ToolResultBlock

COMPACTION_SYSTEM = "You are a precise assistant that summarizes coding-agent conversations."

COMPACTION_PROMPT = """\
下面是一场编程智能体会话的较早部分记录。请把它压缩成一份简洁纪要，作为后续对话的上下文。必须保留：
- 用户提出的目标与约束
- 已做出的决定及理由
- 已创建/修改的文件路径与关键变更内容
- 命令执行的关键结果（构建/测试是否通过，报错摘要）
- 尚未完成的事项
用要点列出，直接输出纪要本身，不要寒暄。

<conversation>
"""


def estimate_size(messages: list[Message]) -> int:
    """Rough context size in chars (text + tool call arguments + tool results)."""
    total = 0
    for m in messages:
        for b in m.blocks:
            if isinstance(b, TextBlock):
                total += len(b.text)
            elif isinstance(b, ToolCallBlock):
                total += len(b.name) + len(b.arguments)
            elif isinstance(b, ToolResultBlock):
                total += len(b.content)
    return total


def render_messages(messages: list[Message], max_block: int = 2000) -> str:
    lines: list[str] = []
    for m in messages:
        for b in m.blocks:
            if isinstance(b, TextBlock):
                body = b.text[:max_block]
                lines.append(f"[{m.role.value}] {body}")
            elif isinstance(b, ToolCallBlock):
                lines.append(f"[assistant->tool:{b.name}] {b.arguments[:max_block]}")
            elif isinstance(b, ToolResultBlock):
                status = "error" if b.is_error else "ok"
                lines.append(f"[tool:{b.tool_use_id} {status}] {b.content[:max_block]}")
    return "\n".join(lines)


async def compact(
    provider: LLMProvider,
    messages: list[Message],
    keep_last: int = 8,
) -> tuple[list[Message], int]:
    """Compact history. Returns (new_messages, number_of_messages_dropped).

    If there is nothing older than keep_last, or the summary comes back empty,
    the original list is returned unchanged (dropped == 0).
    """
    if keep_last >= len(messages):
        return messages, 0

    head, tail = messages[:-keep_last], messages[-keep_last:]

    prompt = COMPACTION_PROMPT + render_messages(head) + "\n</conversation>"
    parts: list[str] = []
    async for ev in provider.stream(
        COMPACTION_SYSTEM,
        [Message(role=Role.user, blocks=[TextBlock(text=prompt)])],
        [],
    ):
        if isinstance(ev, TextDelta):
            parts.append(ev.text)
        elif isinstance(ev, StreamEnd):
            pass  # completion marker

    summary = "".join(parts).strip()
    if not summary:
        return messages, 0

    marker = Message(
        role=Role.user,
        blocks=[
            TextBlock(
                text=(
                    "[earlier conversation was compacted into this summary]\n"
                    f"{summary}\n"
                    "[end of summary - recent messages follow]"
                )
            )
        ],
    )
    return [marker] + tail, len(head)
