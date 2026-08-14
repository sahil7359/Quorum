"""Shared HTTP behaviour for LLM providers.

Hand-rolled retry rather than ``tenacity``. It is twenty lines, and the policy needs one
piece of judgement a general-purpose library would not give me for free: **only idempotent
failures are retried.** A 429 or a 5xx is worth another attempt; a 400 means the request is
malformed and retrying it three times just burns three times the quota against a daily cap
of 100K tokens.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class LlmTransportError(RuntimeError):
    """The provider could not be reached, or refused, after exhausting retries."""


async def post_json_with_retry(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    attempts: int = 3,
    base_delay: float = 0.5,
) -> dict[str, Any]:
    last_error: str = "no attempt made"

    for attempt in range(attempts):
        try:
            response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            last_error = f"transport error: {exc}"
        else:
            if response.status_code < 400:
                decoded: dict[str, Any] = response.json()
                return decoded

            # why: a 4xx that is not in RETRYABLE_STATUS means the request itself is wrong.
            #      Retrying it cannot help and spends quota we are capped on. Fail fast.
            #      alt: retry everything (simpler, wastes a scarce daily budget on a bug)
            if response.status_code not in RETRYABLE_STATUS:
                raise LlmTransportError(
                    f"{url} returned {response.status_code}: {response.text[:200]}"
                )
            last_error = f"{response.status_code}: {response.text[:200]}"

        if attempt < attempts - 1:
            # Full jitter. Three specialists retrying in lockstep against a 30 RPM limit
            # would synchronise into exactly the burst the limit is there to prevent.
            delay = random.uniform(0, base_delay * (2**attempt))
            await asyncio.sleep(delay)

    raise LlmTransportError(f"{url} failed after {attempts} attempts; last error: {last_error}")
