"""Ollama adapter. Adapter satisfying ``ChatModelPort``.

This is the provider evaluation runs against, never Groq: a 20-PR trajectory eval is 500K+
tokens and would exhaust the Groq daily quota in a single run. Provider selection is a config
value, so the swap is `QUORUM_LLM_PROVIDER=ollama` and nothing else.
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


class OllamaChatModel:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        logger: LoggerPort,
        timeout: float = 180.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._logger = logger
        # why: a 30B model on a local GPU can take 60s+ for a long specialist prompt. The
        #      httpx default of 5s would turn every synthesis call into a timeout.
        #      alt: keep the default and cap prompt size (would gut the retrieved context)
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
            "stream": False,
            "options": {"temperature": temperature},
        }
        if max_tokens is not None:
            payload["options"]["num_predict"] = max_tokens
        if json_schema is not None:
            # Ollama takes a JSON schema directly in `format` and constrains decoding to it,
            # which is stronger than asking for JSON in the prompt and hoping.
            payload["format"] = dict(json_schema)

        started = time.perf_counter()
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        owns_client = self._client is None
        try:
            body = await post_json_with_retry(client, f"{self._base_url}/api/chat", payload)
        except LlmTransportError as exc:
            self._emit(
                log_events.LLM_FAILED,
                provider="ollama",
                model=chosen,
                node=node,
                error=str(exc)[:200],
            )
            raise
        finally:
            if owns_client:
                await client.aclose()

        latency_ms = int((time.perf_counter() - started) * 1000)
        content = str(body.get("message", {}).get("content", ""))
        usage = TokenUsage(
            provider="ollama",
            model=chosen,
            node=node,
            # Provider-reported counts, never an estimate: the daily budget is derived by
            # summing these, and an estimate that drifts makes the cap meaningless.
            prompt_tokens=int(body.get("prompt_eval_count", 0)),
            output_tokens=int(body.get("eval_count", 0)),
            latency_ms=latency_ms,
            finish_reason=body.get("done_reason"),
        )
        self._emit(
            log_events.LLM_CALLED,
            provider="ollama",
            model=chosen,
            node=node,
            prompt_tokens=usage.prompt_tokens,
            output_tokens=usage.output_tokens,
            latency_ms=latency_ms,
            finish_reason=usage.finish_reason,
        )
        return Completion(content=content, usage=usage)

    def _emit(self, event: str, **fields: object) -> None:
        # Telemetry never fails a request.
        # Telemetry never fails a request. Suppression here is the policy, not an oversight.
        with contextlib.suppress(Exception):
            self._logger.info(event, **fields)
