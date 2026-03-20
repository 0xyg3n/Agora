"""Custom OpenAI Codex LLM plugin for LiveKit Agents.

Uses the ChatGPT backend API with Codex OAuth tokens (JWT Bearer auth),
replicating how OpenClaw authenticates with openai-codex provider.

Endpoint: https://chatgpt.com/backend-api/codex/responses
Auth: Authorization: Bearer <jwt>, chatgpt-account-id from JWT claims.
"""

from __future__ import annotations

import base64
import json
import os
import platform
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
from livekit.agents import APIConnectionError, APIStatusError, APITimeoutError, llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions, NotGivenOr
from livekit.agents.utils import is_given

_DEFAULT_BASE_URL = "https://chatgpt.com/backend-api"
_JWT_CLAIM_PATH = "https://api.openai.com/auth"


def _extract_account_id(token: str) -> str:
    """Extract chatgpt_account_id from JWT claims."""
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("Invalid JWT token")
    # JWT base64url decode (add padding)
    padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))
    account_id = payload.get(_JWT_CLAIM_PATH, {}).get("chatgpt_account_id")
    if not account_id:
        raise ValueError("No chatgpt_account_id in JWT token")
    return account_id


def _build_headers(token: str, account_id: str) -> dict[str, str]:
    """Build request headers matching OpenClaw's openai-codex-responses pattern."""
    ua = f"pi ({platform.system()} {platform.release()}; {platform.machine()})"
    return {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": "pi",
        "User-Agent": ua,
        "accept": "text/event-stream",
        "content-type": "application/json",
    }


class CodexLLM(llm.LLM):
    """LiveKit LLM adapter for OpenAI Codex (ChatGPT backend API)."""

    def __init__(
        self,
        *,
        model: str = "gpt-5.4",
        base_url: str | None = None,
        max_tokens: int = 1024,
        temperature: float | None = None,
    ):
        super().__init__()
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature

        # Resolve token from env — prefer CODEX_OAUTH_TOKEN, fall back to codex CLI auth.json
        token = os.environ.get("CODEX_OAUTH_TOKEN") or os.environ.get("OPENAI_OAUTH_TOKEN")
        if not token:
            token = self._read_codex_cli_token()
        if not token:
            raise ValueError(
                "OpenAI Codex OAuth token required — set CODEX_OAUTH_TOKEN or "
                "OPENAI_OAUTH_TOKEN, or login with `codex auth`"
            )

        self._token = token
        self._account_id = _extract_account_id(token)
        resolved_base = base_url or os.environ.get("CODEX_BASE_URL") or _DEFAULT_BASE_URL
        self._url = resolved_base.rstrip("/")
        if not self._url.endswith("/codex/responses"):
            if self._url.endswith("/codex"):
                self._url += "/responses"
            else:
                self._url += "/codex/responses"

        self._client = httpx.AsyncClient(
            timeout=120.0,
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=10,
                keepalive_expiry=120,
            ),
        )

    @staticmethod
    def _read_codex_cli_token() -> str | None:
        """Try reading token from ~/.codex/auth.json (Codex CLI)."""
        auth_path = os.path.expanduser("~/.codex/auth.json")
        try:
            with open(auth_path) as f:
                data = json.load(f)
            tokens = data.get("tokens", {})
            return tokens.get("access_token")
        except Exception:
            return None

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "openai-codex"

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[llm.ToolChoice] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN,
    ) -> "CodexLLMStream":
        # Convert chat context to OpenAI Responses API format
        # This correctly handles tool calls (function_call) and tool results (function_call_output)
        responses_ctx, _extra_data = chat_ctx.to_provider_format(
            format="openai.responses",
            inject_dummy_user_message=True,
        )
        all_items = cast(list[dict], responses_ctx)

        # Codex Responses API requires 'instructions' separate from 'input'
        instructions_parts: list[str] = []
        messages: list[dict] = []
        for item in all_items:
            role = item.get("role", "")
            item_type = item.get("type", "")

            if role == "system":
                # Extract system messages as instructions
                content = item.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        c.get("text", "") if isinstance(c, dict) else str(c)
                        for c in content
                    )
                instructions_parts.append(content)
            elif item_type in ("function_call", "function_call_output"):
                # Tool call/result items pass through directly
                messages.append(item)
            else:
                # Regular message — ensure content is a string and wrap with type
                content = item.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        c.get("text", "") if isinstance(c, dict) else str(c)
                        for c in content
                    )
                messages.append({
                    "type": "message",
                    "role": role or "user",
                    "content": content,
                })

        instructions = "\n\n".join(instructions_parts) or "You are a helpful assistant."

        # Codex API requires at least one input message
        if not messages:
            messages = [{"type": "message", "role": "user", "content": "Hello"}]

        body: dict[str, Any] = {
            "model": self._model,
            "instructions": instructions,
            "input": messages,
            "stream": True,
            "store": False,
        }

        if self._temperature is not None:
            body["temperature"] = self._temperature

        # Pass tools so the model can make proper function calls
        if tools:
            oai_tools = []
            for t in tools:
                if isinstance(t, llm.FunctionTool):
                    schema = llm.utils.build_legacy_openai_schema(t, internally_tagged=True)
                    oai_tools.append(schema)
                elif isinstance(t, llm.RawFunctionTool):
                    schema = dict(t.info.raw_schema)
                    schema["type"] = "function"
                    oai_tools.append(schema)
            if oai_tools:
                body["tools"] = oai_tools

        headers = _build_headers(self._token, self._account_id)

        return CodexLLMStream(
            self,
            url=self._url,
            body=body,
            headers=headers,
            client=self._client,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
        )


class CodexLLMStream(llm.LLMStream):
    """Streams Codex API responses (SSE) chunk-by-chunk."""

    def __init__(
        self,
        llm_instance: CodexLLM,
        *,
        url: str,
        body: dict,
        headers: dict,
        client: httpx.AsyncClient,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(llm_instance, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._url = url
        self._body = body
        self._headers = headers
        self._client = client
        self._request_id = ""
        self._input_tokens = 0
        self._output_tokens = 0

        # Tool call state
        self._tool_call_id: str | None = None
        self._fnc_name: str | None = None
        self._fnc_raw_arguments: str | None = None

    async def _run(self) -> None:
        import logging as _logging
        _log = _logging.getLogger("codex-llm")
        _log.info(f"Codex request body keys: {list(self._body.keys())}")
        _log.info(f"Codex input count: {len(self._body.get('input', []))}")
        _log.info(f"Codex instructions len: {len(self._body.get('instructions', ''))}")
        if self._body.get('input'):
            _log.info(f"Codex first input: {self._body['input'][0]}")

        retryable = True
        try:
            async with self._client.stream(
                "POST", self._url, json=self._body, headers=self._headers
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    _log.error(f"Codex API {response.status_code}: {body.decode()[:500]}")
                    raise APIStatusError(
                        f"Codex API error: {response.status_code}",
                        status_code=response.status_code,
                        body=body.decode(errors="replace"),
                    )

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break

                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    chunk = self._parse_event(event)
                    if chunk is not None:
                        self._event_ch.send_nowait(chunk)
                        retryable = False

            # Final usage
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
        except APIStatusError:
            raise
        except httpx.TimeoutException as e:
            raise APITimeoutError(retryable=retryable) from e
        except Exception as e:
            raise APIConnectionError(retryable=retryable) from e

    def _parse_event(self, event: dict) -> llm.ChatChunk | None:
        etype = event.get("type", "")

        if etype == "response.created":
            self._request_id = event.get("response", {}).get("id", "")

        elif etype == "response.output_text.delta":
            delta_text = event.get("delta", "")
            if delta_text:
                return llm.ChatChunk(
                    id=self._request_id,
                    delta=llm.ChoiceDelta(content=delta_text, role="assistant"),
                )

        # --- Tool call handling ---
        elif etype == "response.output_item.added":
            item = event.get("item", {})
            if item.get("type") == "function_call":
                self._tool_call_id = item.get("call_id", "")
                self._fnc_name = item.get("name", "")
                self._fnc_raw_arguments = ""

        elif etype == "response.function_call_arguments.delta":
            if self._fnc_raw_arguments is not None:
                self._fnc_raw_arguments += event.get("delta", "")

        elif etype == "response.function_call_arguments.done":
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

        elif etype == "response.completed":
            usage = event.get("response", {}).get("usage", {})
            self._input_tokens = usage.get("input_tokens", 0)
            self._output_tokens = usage.get("output_tokens", 0)

        return None
