import json

import pytest

import claude_unlimited.usage_tracking as usage_tracking

# Shaped exactly like a captured SSE stream from a Haiku request through the
# proxy; see usage_tracking.py's module docstring.
REAL_SHAPED_SSE = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"model":"claude-haiku-4-5-20251001","id":"msg_1",'
    b'"type":"message","role":"assistant","content":[],"stop_reason":null,"stop_sequence":null,'
    b'"usage":{"input_tokens":8,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,'
    b'"output_tokens":1}}}\n\n'
    b'event: content_block_start\n'
    b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    b'event: ping\n'
    b'data: {"type": "ping"}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hey"}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"! How\'s it going"}}\n\n'
    b'event: content_block_stop\n'
    b'data: {"type":"content_block_stop","index":0}\n\n'
    b'event: message_delta\n'
    b'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens","stop_sequence":null},'
    b'"usage":{"input_tokens":8,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,'
    b'"output_tokens":10}}\n\n'
    b'event: message_stop\n'
    b'data: {"type":"message_stop"}\n\n'
)


def _chunks_of(data: bytes, size: int):
    for i in range(0, len(data), size):
        yield data[i:i + size]


def test_tee_yields_every_byte_unchanged_regardless_of_chunking():
    for chunk_size in (1, 3, 7, 64, 4096):
        capture = usage_tracking.UsageCapture()
        forwarded = b"".join(capture.wrap(_chunks_of(REAL_SHAPED_SSE, chunk_size), "text/event-stream"))
        assert forwarded == REAL_SHAPED_SSE, f"tee altered bytes at chunk_size={chunk_size}"


def test_sse_captures_model_and_final_usage_single_chunk():
    capture = usage_tracking.UsageCapture()
    list(capture.wrap(iter([REAL_SHAPED_SSE]), "text/event-stream"))
    assert capture.model == "claude-haiku-4-5-20251001"
    # message_delta's usage (output_tokens=10) must win over message_start's provisional (1).
    assert capture.usage == {"input_tokens": 8, "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0, "output_tokens": 10}


def test_sse_captures_correctly_when_split_across_arbitrary_chunk_boundaries():
    for chunk_size in (1, 2, 5, 13, 37):
        capture = usage_tracking.UsageCapture()
        list(capture.wrap(_chunks_of(REAL_SHAPED_SSE, chunk_size), "text/event-stream"))
        assert capture.model == "claude-haiku-4-5-20251001", f"failed at chunk_size={chunk_size}"
        assert capture.usage["output_tokens"] == 10, f"failed at chunk_size={chunk_size}"


def test_sse_with_only_message_start_has_provisional_usage():
    only_start = REAL_SHAPED_SSE.split(b"event: content_block_start")[0]
    capture = usage_tracking.UsageCapture()
    list(capture.wrap(iter([only_start]), "text/event-stream"))
    assert capture.model == "claude-haiku-4-5-20251001"
    assert capture.usage["output_tokens"] == 1  # provisional, never overwritten


def test_non_streaming_json_body_captures_model_and_usage():
    body = json.dumps({
        "id": "msg_1", "model": "claude-sonnet-5", "role": "assistant",
        "usage": {"input_tokens": 42, "output_tokens": 7},
    }).encode("utf-8")
    capture = usage_tracking.UsageCapture()
    forwarded = b"".join(capture.wrap(_chunks_of(body, 9), "application/json"))
    assert forwarded == body
    assert capture.model == "claude-sonnet-5"
    assert capture.usage == {"input_tokens": 42, "output_tokens": 7}


def test_malformed_sse_never_raises_and_still_forwards_bytes():
    garbage = b"event: message_start\ndata: {not valid json at all\n\nmore garbage bytes here\n\n"
    capture = usage_tracking.UsageCapture()
    forwarded = b"".join(capture.wrap(iter([garbage]), "text/event-stream"))
    assert forwarded == garbage  # forwarding survives even though parsing failed
    assert capture.model is None
    assert capture.usage is None


def test_malformed_json_body_never_raises():
    capture = usage_tracking.UsageCapture()
    forwarded = b"".join(capture.wrap(iter([b"not json"]), "application/json"))
    assert forwarded == b"not json"
    assert capture.model is None


def test_empty_response_body_leaves_capture_empty():
    capture = usage_tracking.UsageCapture()
    forwarded = b"".join(capture.wrap(iter([]), "text/event-stream"))
    assert forwarded == b""
    assert capture.model is None
    assert capture.usage is None


def test_oversized_json_body_is_capped_not_buffered_forever():
    huge_chunk = b"x" * 6_000_000
    capture = usage_tracking.UsageCapture()
    forwarded = b"".join(capture.wrap(iter([huge_chunk]), "application/json"))
    assert forwarded == huge_chunk  # still forwarded in full
    assert capture._json_buffer_capped is True
    assert capture.model is None


def test_content_type_missing_defaults_to_non_streaming_path_without_raising():
    capture = usage_tracking.UsageCapture()
    forwarded = b"".join(capture.wrap(iter([b"whatever"]), None))
    assert forwarded == b"whatever"


# ---- OpenAI Responses (the Codex CLI, relayed untranslated) ----

OPENAI_USAGE = {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 600},
                "output_tokens": 42, "output_tokens_details": {"reasoning_tokens": 30}, "total_tokens": 1042}
MAPPED_USAGE = {"input_tokens": 400, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 600, "output_tokens": 42}


def _responses_sse(terminal: str) -> bytes:
    return (b'event: response.created\ndata: {"type":"response.created","response":{"model":"gpt-5.6-sol"}}\n\n'
            b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"pong"}\n\n'
            + f"event: {terminal}\ndata: ".encode()
            + json.dumps({"type": terminal, "response": {"model": "gpt-5.6-sol", "usage": OPENAI_USAGE}}).encode()
            + b"\n\n")


@pytest.mark.parametrize("terminal", ["response.completed", "response.incomplete"])
def test_responses_stream_usage_is_mapped_to_the_internal_shape(terminal):
    stream = _responses_sse(terminal)
    capture = usage_tracking.UsageCapture()
    forwarded = b"".join(capture.wrap(iter([stream[:37], stream[37:]]), "text/event-stream"))

    assert forwarded == stream
    assert capture.model == "gpt-5.6-sol"
    assert capture.usage == MAPPED_USAGE


def test_responses_cached_tokens_never_drive_input_below_zero():
    usage = {"input_tokens": 5, "input_tokens_details": {"cached_tokens": 9}, "output_tokens": 1}
    stream = (b'event: response.completed\ndata: '
              + json.dumps({"type": "response.completed", "response": {"model": "m", "usage": usage}}).encode()
              + b"\n\n")
    capture = usage_tracking.UsageCapture()
    b"".join(capture.wrap(iter([stream]), "text/event-stream"))

    assert capture.usage["input_tokens"] == 0
    assert capture.usage["cache_read_input_tokens"] == 9


def test_non_streaming_responses_body_usage_is_mapped():
    body = json.dumps({"object": "response", "model": "gpt-5.6-sol", "output": [], "usage": OPENAI_USAGE}).encode()
    capture = usage_tracking.UsageCapture()
    b"".join(capture.wrap(iter([body]), "application/json"))

    assert capture.model == "gpt-5.6-sol"
    assert capture.usage == MAPPED_USAGE


def test_non_streaming_anthropic_body_is_still_taken_verbatim():
    usage = {"input_tokens": 3, "cache_creation_input_tokens": 1, "cache_read_input_tokens": 2, "output_tokens": 4}
    body = json.dumps({"model": "claude-haiku-4-5", "usage": usage}).encode()
    capture = usage_tracking.UsageCapture()
    b"".join(capture.wrap(iter([body]), "application/json"))

    assert capture.usage == usage


# ---- CRLF-framed streams (the SSE spec allows them; a proxy may rewrite to them) ----

@pytest.mark.parametrize("chunk_size", [1, 2, 7, 4096])
def test_crlf_framed_anthropic_stream_still_records_usage(chunk_size):
    crlf = REAL_SHAPED_SSE.replace(b"\n", b"\r\n")
    capture = usage_tracking.UsageCapture()
    forwarded = b"".join(capture.wrap(_chunks_of(crlf, chunk_size), "text/event-stream"))

    assert forwarded == crlf                      # relayed bytes are untouched
    assert capture.model == "claude-haiku-4-5-20251001"
    assert capture.usage["output_tokens"] == 10


@pytest.mark.parametrize("chunk_size", [1, 3, 4096])
def test_crlf_framed_responses_stream_still_records_usage(chunk_size):
    crlf = _responses_sse("response.completed").replace(b"\n", b"\r\n")
    capture = usage_tracking.UsageCapture()
    forwarded = b"".join(capture.wrap(_chunks_of(crlf, chunk_size), "text/event-stream"))

    assert forwarded == crlf
    assert capture.model == "gpt-5.6-sol"
    assert capture.usage == MAPPED_USAGE


# The ChatGPT Codex backend streams SSE with no Content-Type header at all
# (measured live against chatgpt.com, 2026-09-25), so every real `cu codex`
# turn used to be parsed as JSON and record nothing.
@pytest.mark.parametrize("chunk_size", [1, 7, 4096])
def test_responses_stream_without_a_content_type_still_records_usage(chunk_size):
    body = _responses_sse("response.completed")
    capture = usage_tracking.UsageCapture()
    forwarded = b"".join(capture.wrap(_chunks_of(body, chunk_size), None))

    assert forwarded == body
    assert capture.model == "gpt-5.6-sol"
    assert capture.usage == MAPPED_USAGE


def test_json_body_without_a_content_type_is_still_parsed_as_json():
    body = json.dumps({"model": "claude-haiku-4-5",
                       "usage": {"input_tokens": 3, "output_tokens": 4}}).encode()
    capture = usage_tracking.UsageCapture()
    forwarded = b"".join(capture.wrap(iter([body]), None))

    assert forwarded == body
    assert capture.model == "claude-haiku-4-5"
    assert capture.usage["output_tokens"] == 4
