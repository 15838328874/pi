"""Internal message model, mirroring pi-ai's block-based messages.

A Message carries a role and a list of typed blocks:
- TextBlock       -> plain text content
- ToolCallBlock   -> assistant requests a tool call (arguments is a JSON string)
- ToolResultBlock -> user-side tool result fed back to the model

Providers translate this canonical form into OpenAI / Anthropic wire formats.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field


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


Block = Annotated[Union[TextBlock, ToolCallBlock, ToolResultBlock], Field(discriminator="type")]
"""
    Union[TextBlock, ToolCallBlock, ToolResultBlock]
    表示这个字段可以是三种类型中的任意一种。
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


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
