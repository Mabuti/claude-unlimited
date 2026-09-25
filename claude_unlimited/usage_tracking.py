"""Per-request token/model usage capture from Anthropic response bodies (and
from OpenAI Responses bodies relayed untranslated for the Codex CLI — see
the end of this docstring).

An Anthropic SSE stream has this structure (content elided):

    event: message_start
    data: {"type":"message_start","message":{"model":"claude-haiku-4-5-20251001",...,
           "usage":{"input_tokens":8,"cache_creation_input_tokens":0,
           "cache_read_input_tokens":0,"output_tokens":1,...}}}

    event: content_block_start / content_block_delta / content_block_stop
    (the actual text — never read by this module)

    event: message_delta
    data: {"type":"message_delta","delta":{"stop_reason":"max_tokens",...},
           "usage":{"input_tokens":8,"cache_creation_input_tokens":0,
           "cache_read_input_tokens":0,"output_tokens":10}}

    event: message_stop

`message_start` carries the model name and a provisional usage snapshot
(output_tokens is a placeholder, always small); `message_delta` carries the
authoritative final usage (all four fields, self-contained — not a diff).
This module reads only `type`, `message.model`, and `usage` — never message
content — and only from its own separate copy of the bytes.

Safety invariant: UsageCapture.wrap() is a strict tee. It yields every chunk
exactly as received, in order and unmodified, and only inspects a separate
copy of the bytes. Every parsing step is guarded so a failure here can never
raise past this module or alter what the client receives; the worst case is
that one request's usage isn't captured.

OpenAI Responses: the stream's terminal `response.completed` (or
`response.incomplete`) event carries `response.model` and `response.usage`;
a non-streaming body (e.g. /v1/responses/compact) carries `usage` at the top
level. That usage is mapped to the Anthropic shape above: input_tokens minus
input_tokens_details.cached_tokens, cache_read_input_tokens = cached_tokens,
output_tokens as-is, cache_creation_input_tokens = 0.
"""

from __future__ import annotations

import json
from typing import Iterator, Optional

_JSON_BODY_CAP_BYTES = 5_000_000  # give up buffering non-streaming bodies larger than this


class UsageCapture:
    def __init__(self) -> None:
        self.model: Optional[str] = None
        self.usage: Optional[dict] = None
        self._sse_buffer = b""
        self._json_buffer = b""
        self._json_buffer_capped = False

    def wrap(self, chunks: Iterator[bytes], content_type: Optional[str]) -> Iterator[bytes]:
        is_sse = "text/event-stream" in (content_type or "")
        for chunk in chunks:
            try:
                if is_sse:
                    self._feed_sse(chunk)
                else:
                    self._feed_json(chunk)
            except Exception:
                pass  # capture must never affect forwarding
            yield chunk
        if not is_sse:
            try:
                self._finalize_json()
            except Exception:
                pass

    # ---- SSE (streaming) ----

    def _feed_sse(self, chunk: bytes) -> None:
        self._sse_buffer += chunk
        while b"\n\n" in self._sse_buffer:
            event_block, self._sse_buffer = self._sse_buffer.split(b"\n\n", 1)
            self._parse_sse_event(event_block)

    def _parse_sse_event(self, block: bytes) -> None:
        data_lines = [line[len(b"data:"):].strip() for line in block.split(b"\n") if line.startswith(b"data:")]
        if not data_lines:
            return
        payload = json.loads(b"\n".join(data_lines).decode("utf-8"))
        event_type = payload.get("type")
        if event_type == "message_start":
            message = payload.get("message") or {}
            if message.get("model"):
                self.model = message["model"]
            usage = message.get("usage")
            if isinstance(usage, dict) and self.usage is None:
                self.usage = usage  # provisional; message_delta overwrites with the final count
        elif event_type == "message_delta":
            usage = payload.get("usage")
            if isinstance(usage, dict):
                self.usage = usage  # authoritative, self-contained (not a diff)
        elif event_type in ("response.completed", "response.incomplete"):
            # An OpenAI Responses stream (the Codex CLI, relayed untranslated):
            # the terminal event carries the model and the final usage.
            self._take_openai_response(payload.get("response"))

    # ---- plain JSON (non-streaming) ----

    def _feed_json(self, chunk: bytes) -> None:
        if self._json_buffer_capped:
            return
        self._json_buffer += chunk
        if len(self._json_buffer) > _JSON_BODY_CAP_BYTES:
            self._json_buffer_capped = True
            self._json_buffer = b""

    def _finalize_json(self) -> None:
        if self._json_buffer_capped or not self._json_buffer:
            return
        payload = json.loads(self._json_buffer.decode("utf-8"))
        if _is_openai_usage(payload.get("usage")):
            self._take_openai_response(payload)
            return
        if payload.get("model"):
            self.model = payload["model"]
        usage = payload.get("usage")
        if isinstance(usage, dict):
            self.usage = usage

    # ---- OpenAI Responses ----

    def _take_openai_response(self, response) -> None:
        if not isinstance(response, dict):
            return
        if isinstance(response.get("model"), str) and response["model"]:
            self.model = response["model"]
        usage = _openai_usage(response.get("usage"))
        if usage is not None:
            self.usage = usage


def _is_openai_usage(usage) -> bool:
    """An OpenAI Responses usage object, as opposed to Anthropic's: it reports
    cached input inside input_tokens_details and has no cache_* fields."""
    return (isinstance(usage, dict)
            and "cache_read_input_tokens" not in usage
            and "cache_creation_input_tokens" not in usage
            and ("input_tokens_details" in usage or "output_tokens_details" in usage
                 or "total_tokens" in usage))


def _openai_usage(usage) -> Optional[dict]:
    """OpenAI Responses usage in this module's (Anthropic) shape. OpenAI's
    input_tokens INCLUDES the cached part; Anthropic's excludes it, so the
    cached tokens move from one field to the other."""
    if not isinstance(usage, dict):
        return None
    details = usage.get("input_tokens_details")
    cached = int((details or {}).get("cached_tokens") or 0) if isinstance(details, dict) else 0
    return {
        "input_tokens": max(0, int(usage.get("input_tokens") or 0) - cached),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": cached,
        "output_tokens": int(usage.get("output_tokens") or 0),
    }
