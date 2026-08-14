"""Groq adapter. Adapter satisfying ``ChatModelPort``.

⚠ **Never executed against the real API in this repository's history.** There is no
`GROQ_API_KEY` in the development environment, so this adapter is written against Groq's
documented OpenAI-compatible schema and exercised only against a stubbed transport. The
request shape and the `usage` field names are the most likely things to be wrong. Verify the
first time a key exists; do not treat any Groq number as measured until then.

Production only. Evaluation runs against Ollama — a 20-PR trajectory eval is 500K+ tokens and
would exhaust the 100K/day free tier in one run.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from app.domain import log_events
from app.domain.ports import ChatMessage, Completion, LoggerPort
from app.domain.values import TokenUsage
from app.infrastructure.llm.http import LlmTransportError, post_json_with_retry


class GroqChatModel:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        logger: LoggerPort,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError(
                "GroqChatModel requires an API key. Set QUORUM_GROQ_API_KEY, or select "
                "QUORUM_LLM_PROVIDER=ollama for local work."
            )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._logger = logger
        self._timeout = timeout
        self._client = client

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        node: str,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        json_schema: Mapping[str, Any] | None = None,
    ) -> Completion:
        chosen = model or self._model
        payload: dict[str, Any] = {
            "model": chosen,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if json_schema is not None:
            # why: Groq's OpenAI-compatible surface takes {"type": "json_object"} rather than
            #      a schema, so unlike Ollama the schema cannot constrain decoding -- it only
            #      guarantees syntactically valid JSON. The specialist parser therefore has to
            #      validate shape itself and drop on mismatch, which it does.
            #      alt: assume schema enforcement (would silently trust malformed output)
            payload["response_format"] = {"type": "json_object"}

        headers = {"Authorization": f"Bearer {self._api_key}"}
        started = time.perf_counter()
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        owns_client = self._client is None
        try:
            body = await post_json_with_retry(
                client, f"{self._base_url}/chat/completions", payload, headers=headers
            )
        except LlmTransportError as exc:
            # The key is in `headers`, never in the payload, and the error text is truncated
            # and never includes headers -- a leaked bearer token in a log line is an incident.
            self._emit(
                log_events.LLM_FAILED,
                provider="groq",
                model=chosen,
                node=node,
                error=str(exc)[:200],
            )
            raise
        finally:
            if owns_client:
                await client.aclose()

        latency_ms = int((time.perf_counter() - started) * 1000)
        choices = body.get("choices") or [{}]
        first = choices[0]
        usage_body = body.get("usage", {})
        usage = TokenUsage(
            provider="groq",
            model=chosen,
            node=node,
            prompt_tokens=int(usage_body.get("prompt_tokens", 0)),
            output_tokens=int(usage_body.get("completion_tokens", 0)),
            latency_ms=latency_ms,
            finish_reason=first.get("finish_reason"),
        )
        self._emit(
            log_events.LLM_CALLED,
            provider="groq",
            model=chosen,
            node=node,
            prompt_tokens=usage.prompt_tokens,
            output_tokens=usage.output_tokens,
            latency_ms=latency_ms,
            finish_reason=usage.finish_reason,
        )
        return Completion(content=str(first.get("message", {}).get("content", "")), usage=usage)

    def _emit(self, event: str, **fields: object) -> None:
        # Telemetry never fails a request. Suppression here is the policy, not an oversight.
        with contextlib.suppress(Exception):
            self._logger.info(event, **fields)
