"""Custom Claude LLM plugin for LiveKit Agents.

Uses the Anthropic Python SDK directly. Supports both API key and OAuth
token auth (the latter reads Claude Code CLI credentials automatically).
Streams responses so TTS can start speaking before the full response arrives.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable
from typing import Any, cast

import anthropic
import httpx
from livekit.agents import APIConnectionError, APIStatusError, APITimeoutError, llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions, NotGivenOr
from livekit.agents.utils import is_given

# Beta flags required by Anthropic for OAuth token auth
_OAUTH_BETA = "claude-code-20250219,oauth-2025-04-20"
_OAUTH_UA = "claude-cli/2.1.2 (external, cli)"


def _is_oauth_token(key: str) -> bool:
    return "sk-ant-oat" in key


class ClaudeLLM(llm.LLM):
    """LiveKit LLM adapter for Anthropic Claude API."""

    def __init__(
        self,
        *,
        model: str = "claude-sonnet-4-20250514",
        api_key: str | None = None,
        auth_token: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 1024,
        temperature: float | None = None,
    ):
        super().__init__()
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature

        resolved_base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL")
        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        resolved_auth_token = (
            auth_token
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ANTHROPIC_OAUTH_TOKEN")
        )

        if not resolved_key and not resolved_auth_token:
            raise ValueError(
                "Anthropic auth required — set ANTHROPIC_API_KEY, "
                "ANTHROPIC_AUTH_TOKEN (OAuth), or ANTHROPIC_OAUTH_TOKEN"
            )

        # OAuth tokens (sk-ant-oat01-*) need Bearer auth + special headers
        use_oauth = bool(
            resolved_auth_token and _is_oauth_token(resolved_auth_token)
        ) or bool(
            resolved_key and _is_oauth_token(resolved_key)
        )

        if use_oauth:
            token = resolved_auth_token or resolved_key
            self._client = anthropic.AsyncAnthropic(
                api_key=None,
                auth_token=token,
                base_url=resolved_base_url,
                default_headers={
                    "anthropic-beta": _OAUTH_BETA,
                    "user-agent": _OAUTH_UA,
                    "x-app": "cli",
                },
                http_client=httpx.AsyncClient(
                    timeout=60.0,
                    follow_redirects=True,
                    limits=httpx.Limits(
                        max_connections=50,
                        max_keepalive_connections=10,
                        keepalive_expiry=120,
                    ),
                ),
            )
        else:
            self._client = anthropic.AsyncAnthropic(
                api_key=resolved_key,
                base_url=resolved_base_url,
                http_client=httpx.AsyncClient(
                    timeout=60.0,
                    follow_redirects=True,
                    limits=httpx.Limits(
                        max_connections=50,
                        max_keepalive_connections=10,
                        keepalive_expiry=120,
                    ),
                ),
            )

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "anthropic"

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[llm.ToolChoice] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN,
    ) -> "ClaudeLLMStream":
        extra: dict[str, Any] = {}

        if is_given(extra_kwargs):
            extra.update(extra_kwargs)

        extra["max_tokens"] = self._max_tokens
        if self._temperature is not None:
            extra["temperature"] = self._temperature

        # Convert chat context to Anthropic message format
        anthropic_ctx, extra_data = chat_ctx.to_provider_format(
            format="anthropic",
            inject_dummy_user_message=True,
        )
        messages = cast(list[anthropic.types.MessageParam], anthropic_ctx)

        if extra_data.system_messages:
            extra["system"] = [
                anthropic.types.TextBlockParam(text=content, type="text")
                for content in extra_data.system_messages
            ]

        # Handle tool schemas if provided
        if tools:
            tool_ctx = llm.ToolContext(tools)
            tool_schemas = tool_ctx.parse_function_tools("anthropic")
            if tool_schemas:
                extra["tools"] = tool_schemas

        stream = self._client.messages.create(
            messages=messages,
            model=self._model,
            stream=True,
            timeout=conn_options.timeout,
            **extra,
        )

        return ClaudeLLMStream(
            self,
            anthropic_stream=stream,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
        )


class ClaudeLLMStream(llm.LLMStream):
    """Streams Claude API responses chunk-by-chunk."""

    def __init__(
        self,
        llm_instance: ClaudeLLM,
        *,
        anthropic_stream: Awaitable[anthropic.AsyncStream[anthropic.types.RawMessageStreamEvent]],
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(llm_instance, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._awaitable_stream = anthropic_stream
        self._request_id = ""
        self._input_tokens = 0
        self._output_tokens = 0

        # Tool call state
        self._tool_call_id: str | None = None
        self._fnc_name: str | None = None
        self._fnc_raw_arguments: str | None = None

    async def _run(self) -> None:
        retryable = True
        try:
            stream = await self._awaitable_stream
            async with stream:
                async for event in stream:
                    chunk = self._parse_event(event)
                    if chunk is not None:
                        self._event_ch.send_nowait(chunk)
                        retryable = False

                # Send final usage chunk
                self._event_ch.send_nowait(
                    llm.ChatChunk(
                        id=self._request_id,
                        usage=llm.CompletionUsage(
                            completion_tokens=self._output_tokens,
                            prompt_tokens=self._input_tokens,
                            total_tokens=self._input_tokens + self._output_tokens,
                        ),
                    )
                )
        except anthropic.APITimeoutError as e:
            raise APITimeoutError(retryable=retryable) from e
        except anthropic.APIStatusError as e:
            raise APIStatusError(
                e.message,
                status_code=e.status_code,
                request_id=e.request_id,
                body=e.body,
            ) from e
        except Exception as e:
            raise APIConnectionError(retryable=retryable) from e

    def _parse_event(
        self, event: anthropic.types.RawMessageStreamEvent
    ) -> llm.ChatChunk | None:
        if event.type == "message_start":
            self._request_id = event.message.id
            self._input_tokens = event.message.usage.input_tokens
            self._output_tokens = event.message.usage.output_tokens

        elif event.type == "message_delta":
            self._output_tokens += event.usage.output_tokens

        elif event.type == "content_block_start":
            if event.content_block.type == "tool_use":
                self._tool_call_id = event.content_block.id
                self._fnc_name = event.content_block.name
                self._fnc_raw_arguments = ""

        elif event.type == "content_block_delta":
            delta = event.delta
            if delta.type == "text_delta":
                return llm.ChatChunk(
                    id=self._request_id,
                    delta=llm.ChoiceDelta(content=delta.text, role="assistant"),
                )
            elif delta.type == "input_json_delta":
                if self._fnc_raw_arguments is not None:
                    self._fnc_raw_arguments += delta.partial_json

        elif event.type == "content_block_stop":
            if self._tool_call_id is not None:
                chunk = llm.ChatChunk(
                    id=self._request_id,
                    delta=llm.ChoiceDelta(
                        role="assistant",
                        tool_calls=[
                            llm.FunctionToolCall(
                                arguments=self._fnc_raw_arguments or "",
                                name=self._fnc_name or "",
                                call_id=self._tool_call_id or "",
                            )
                        ],
                    ),
                )
                self._tool_call_id = self._fnc_raw_arguments = self._fnc_name = None
                return chunk

        return None
