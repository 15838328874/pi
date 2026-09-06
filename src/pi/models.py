"""Internal message model, mirroring pi-ai's block-based messages.

A Message carries a role and a list of typed blocks:
- TextBlock       -> plain text content
- ToolCallBlock   -> assistant requests a tool call (arguments is a JSON string)
- ToolResultBlock -> user-side tool result fed back to the model
- FileBlock       -> file attached to a user message, referenced by public URL

Providers translate this canonical form into OpenAI / Anthropic wire formats.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, StringConstraints


class Role(str, Enum):
    system = "system"
    user = "user"
    assistant = "assistant"


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ToolCallBlock(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: str = "{}"  # JSON-encoded object


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


class FileBlock(BaseModel):
    """A file attached to a user message (upload), referenced by public URL.

    The gateway downloads the file itself when the request is sent, so this
    block only ever carries the URL plus a display name. Only some models
    accept it; the provider decides whether to translate it or fail loudly.
    """

    type: Literal["file"] = "file"
    file_url: str
    name: str = ""


Block = Annotated[
    Union[TextBlock, ToolCallBlock, ToolResultBlock, FileBlock], Field(discriminator="type")
]
"""
    Union[TextBlock, ToolCallBlock, ToolResultBlock, FileBlock]
    表示这个字段可以是四种类型中的任意一种。
    Field(discriminator="type")
    这是关键部分。它告诉 Pydantic：用 JSON 数据中的 "type" 字段来区分具体是哪种类型。
    没有 discriminator 的话，Pydantic 会依次尝试每种类型，既慢又可能出错。有了它，Pydantic 直接根据 type 字段的值精准匹配，性能更好、错误更明确。

"""

class Message(BaseModel):
    role: Role
    blocks: list[Block] = Field(default_factory=list)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class Plan(BaseModel):
    """Structured task plan, produced by the model through the submit_plan tool.

    The bounds are load-bearing: this is untrusted model output that gets
    persisted into a TEXT column and replayed over SSE. 200 + 20*300 caps a plan
    at ~6 KB, which a single column, a single event frame and a 50-session list
    response all absorb comfortably.

    Steps are plain strings rather than objects. Nothing in this schema tracks
    step progress - a status field would be dead weight until the approval and
    resume phases give it a lifecycle, and freezing the enum values now would
    only have to be undone later.
    """

    title: str = Field(min_length=1, max_length=200, description="One-line goal of the task.")
    steps: list[Annotated[str, StringConstraints(min_length=1, max_length=300)]] = Field(
        min_length=1, max_length=20, description="Ordered, concrete steps."
    )


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
