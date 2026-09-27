"""Filter that strips <think>...</think> spans from streamed text deltas.

Some OpenAI-compatible endpoints (qwen/deepseek style) inline reasoning in the
content stream wrapped in <think> tags. The tags can span chunk boundaries, so
this is a small buffering state machine.
"""

from __future__ import annotations

OPEN = "<think>"
CLOSE = "</think>"


def _longest_suffix_prefix(haystack: str, needle: str) -> int:
    """Length of the longest suffix of haystack that is a prefix of needle.
       代码通过 _longest_suffix_prefix 函数判断缓冲区末尾是否正在拼凑标签的前几个字符。
       如果有可能构成标签前缀，就先扣留不输出，等下一个 chunk 到来后再判断。
       这就是它能正确处理跨 chunk 标签的关键机制。
    """
    max_len = min(len(haystack), len(needle) - 1)
    for n in range(max_len, 0, -1):
        if haystack.endswith(needle[:n]):
            return n
    return 0


class ThinkFilter:
    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    def feed(self, text: str) -> str:
        self._buf += text
        out: list[str] = []
        while self._buf:
            tag = CLOSE if self._in_think else OPEN
            idx = self._buf.find(tag)
            if idx != -1:
                if not self._in_think:
                    out.append(self._buf[:idx])
                self._buf = self._buf[idx + len(tag) :]
                self._in_think = not self._in_think
                continue
            # no full tag; hold back a possible partial tag at the end
            held = _longest_suffix_prefix(self._buf, tag)
            emit_len = len(self._buf) - held if not self._in_think else 0
            if emit_len > 0:
                out.append(self._buf[:emit_len])
                self._buf = self._buf[emit_len:]
            break
        return "".join(out)

    def flush(self) -> str:
        rest = "" if self._in_think else self._buf
        self._buf = ""
        return rest
