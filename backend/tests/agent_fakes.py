from collections.abc import AsyncIterator

from app.agents.providers.base import ChatChunk, ChatMessage, LLMProvider, ToolDefinition


class ChatOnlyProvider(LLMProvider):
    """Base for fakes that script `chat()`; the stream is empty and never read."""

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.4,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ChatChunk]:
        return
        yield
