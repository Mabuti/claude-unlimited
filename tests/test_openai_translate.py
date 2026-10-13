import json

import pytest

from claude_unlimited import openai_translate
from claude_unlimited.openai_models import OpenAIModelTarget
from claude_unlimited.openai_translate import ResponseTranslator, anthropic_request_to_openai

TARGET = OpenAIModelTarget("gpt-5.6-terra", "medium")


# ---- request translation ----

def test_simple_text_request_translates_model_instructions_and_input():
    body = {
        "model": "claude-sonnet-5",
        "system": "You are a helpful assistant.",
        "messages": [{"role": "user", "content": "Hello there"}],
    }
    out = anthropic_request_to_openai(body, TARGET)
    assert out["model"] == "gpt-5.6-terra"
    assert out["instructions"] == "You are a helpful assistant."
    assert out["reasoning"] == {"effort": "medium"}
    assert out["stream"] is True
    assert out["store"] is False
    assert out["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hello there"}]}
    ]


def test_system_as_content_block_list_is_joined():
    body = {"system": [{"type": "text", "text": "Part one."}, {"type": "text", "text": "Part two."}],
            "messages": []}
    out = anthropic_request_to_openai(body, TARGET)
    assert out["instructions"] == "Part one.\n\nPart two."


def test_no_system_omits_instructions_key():
    out = anthropic_request_to_openai({"messages": []}, TARGET)
    assert "instructions" not in out


def test_assistant_text_message_uses_output_text_not_input_text():
    body = {"messages": [{"role": "assistant", "content": "Sure, here you go."}]}
    out = anthropic_request_to_openai(body, TARGET)
    assert out["input"] == [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Sure, here you go."}]}
    ]


def test_tool_use_block_becomes_a_function_call_item():
    body = {"messages": [{
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {"command": "ls"}},
        ],
    }]}
    out = anthropic_request_to_openai(body, TARGET)
    assert out["input"] == [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Let me check."}]},
        {"type": "function_call", "call_id": "call_1", "name": "Bash", "arguments": json.dumps({"command": "ls"})},
    ]


def test_tool_result_block_becomes_a_function_call_output_item():
    body = {"messages": [{
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "total 0"}],
    }]}
    out = anthropic_request_to_openai(body, TARGET)
    assert out["input"] == [
        {"type": "function_call_output", "call_id": "call_1", "output": "total 0"},
    ]


def test_tool_result_with_structured_content_blocks_joins_text_parts():
    body = {"messages": [{
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call_2",
                      "content": [{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}]}],
    }]}
    out = anthropic_request_to_openai(body, TARGET)
    assert out["input"][0]["output"] == "line one\nline two"


def test_base64_image_block_becomes_a_data_uri_input_image():
    body = {"messages": [{
        "role": "user",
        "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}],
    }]}
    out = anthropic_request_to_openai(body, TARGET)
    assert out["input"][0]["content"][0] == {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}


def test_tools_array_maps_name_description_and_input_schema_to_parameters():
    body = {"messages": [], "tools": [
        {"name": "Bash", "description": "Run a shell command",
         "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}},
    ]}
    out = anthropic_request_to_openai(body, TARGET)
    assert out["tools"] == [{
        "type": "function", "name": "Bash", "description": "Run a shell command",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
        "strict": False,
    }]


def test_no_tools_omits_tools_key():
    out = anthropic_request_to_openai({"messages": []}, TARGET)
    assert "tools" not in out


def test_tool_choice_mapping():
    assert anthropic_request_to_openai({"messages": [], "tool_choice": {"type": "auto"}}, TARGET)["tool_choice"] == "auto"
    assert anthropic_request_to_openai({"messages": [], "tool_choice": {"type": "any"}}, TARGET)["tool_choice"] == "required"
    assert anthropic_request_to_openai({"messages": [], "tool_choice": {"type": "none"}}, TARGET)["tool_choice"] == "none"
    assert anthropic_request_to_openai({"messages": []}, TARGET)["tool_choice"] == "auto"


def test_forcing_one_tool_names_it_as_a_function_not_a_bare_string():
    """A bare tool name is not a shape the Responses API accepts. Sending one
    failed the whole request with `Invalid value: 'Bash'. Supported values are:
    'none', 'auto', and 'required'.` - for every forced tool, not just one."""
    out = anthropic_request_to_openai(
        {"messages": [], "tool_choice": {"type": "tool", "name": "Bash"}}, TARGET)
    assert out["tool_choice"] == {"type": "function", "name": "Bash"}


def test_forcing_web_search_chooses_the_hosted_tool_by_type():
    """A built-in is chosen by its type, never as a function - this is the
    request that first surfaced the bug, on a `how's the weather` prompt."""
    out = anthropic_request_to_openai(
        {"messages": [], "tool_choice": {"type": "tool", "name": "web_search"}}, TARGET)
    assert out["tool_choice"] == {"type": "web_search"}


def test_forcing_a_server_tool_we_cannot_translate_falls_back_to_auto():
    """web_fetch is dropped from `tools`, so forcing it by name would point at
    a tool that is not in the request at all."""
    out = anthropic_request_to_openai(
        {"messages": [], "tool_choice": {"type": "tool", "name": "web_fetch"}}, TARGET)
    assert out["tool_choice"] == "auto"


# ---- Anthropic server-side tools -> OpenAI built-ins ----

@pytest.mark.parametrize("anthropic_type", ["web_search_20250305", "web_search_20260209"])
def test_web_search_becomes_openais_own_built_in(anthropic_type):
    """Anthropic dates these types and bumps the date on revisions, so both the
    basic and the dynamic-filtering spelling must map to the same built-in.
    Translating either into a function tool offered the model a tool that
    nothing on the OpenAI side could execute."""
    out = anthropic_request_to_openai(
        {"messages": [], "tools": [{"type": anthropic_type, "name": "web_search"}]}, TARGET)
    assert out["tools"] == [{"type": "web_search"}]


def test_web_search_carries_over_the_options_both_sides_express():
    out = anthropic_request_to_openai({"messages": [], "tools": [{
        "type": "web_search_20260209",
        "name": "web_search",
        "allowed_domains": ["example.com"],
        "user_location": {"type": "approximate", "country": "RO", "city": "Bucharest"},
    }]}, TARGET)
    assert out["tools"] == [{
        "type": "web_search",
        "filters": {"allowed_domains": ["example.com"]},
        "user_location": {"type": "approximate", "country": "RO", "city": "Bucharest"},
    }]


def test_blocked_domains_is_dropped_rather_than_inverted():
    """OpenAI has no deny-list. Turning one into an allow-list would widen the
    restriction the caller asked for, so it is dropped, not guessed at."""
    out = anthropic_request_to_openai({"messages": [], "tools": [{
        "type": "web_search_20260209", "name": "web_search",
        "blocked_domains": ["blocked.example"],
    }]}, TARGET)
    assert out["tools"] == [{"type": "web_search"}]


def test_a_server_tool_with_no_openai_equivalent_is_dropped():
    """Better no tool than a function tool the model is invited to call and
    nothing can run."""
    out = anthropic_request_to_openai({"messages": [], "tools": [
        {"type": "web_fetch_20260209", "name": "web_fetch"},
        {"name": "Bash", "description": "run a command", "input_schema": {"type": "object"}},
    ]}, TARGET)
    assert [t["type"] for t in out["tools"]] == ["function"]
    assert out["tools"][0]["name"] == "Bash"


def test_ordinary_client_tools_are_untouched_by_the_server_tool_path():
    out = anthropic_request_to_openai({"messages": [], "tools": [
        {"name": "Bash", "description": "run", "input_schema": {"type": "object", "properties": {}}},
    ]}, TARGET)
    assert out["tools"] == [{
        "type": "function", "name": "Bash", "description": "run",
        "parameters": {"type": "object", "properties": {}}, "strict": False,
    }]


def test_unknown_content_block_type_is_skipped_not_fatal():
    body = {"messages": [{"role": "user", "content": [
        {"type": "some_future_block_type", "data": "whatever"},
        {"type": "text", "text": "still here"},
    ]}]}
    out = anthropic_request_to_openai(body, TARGET)
    assert out["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "still here"}]}
    ]


# ---- response translation ----

def _sse_bytes(chunks):
    return b"".join(chunks)


def test_text_only_turn_translates_to_a_full_anthropic_sse_sequence():
    translator = ResponseTranslator()
    events = [
        {"type": "response.created", "response": {"model": "gpt-5.6-terra"}},
        {"type": "response.output_item.added", "item": {"type": "message"}},
        {"type": "response.output_text.delta", "delta": "Hel"},
        {"type": "response.output_text.delta", "delta": "lo"},
        {"type": "response.output_item.done", "item": {"type": "message"}},
        # The real Responses API shape: cache and reasoning counts are nested,
        # and input_tokens INCLUDES the cached part.
        {"type": "response.completed", "response": {"usage": {
            "input_tokens": 10, "input_tokens_details": {"cached_tokens": 3},
            "output_tokens": 2, "output_tokens_details": {"reasoning_tokens": 1}}}},
    ]
    out = b""
    for event in events:
        out += _sse_bytes(translator.feed(event))
    text = out.decode()

    assert "event: message_start" in text
    assert '"type": "text", "text": ""' in text
    assert '"text": "Hel"' in text
    assert '"text": "lo"' in text
    assert "event: content_block_stop" in text
    assert '"stop_reason": "end_turn"' in text
    assert "event: message_stop" in text
    # Anthropic's input_tokens excludes cache reads, so the 10 splits 7 + 3;
    # the total Claude Code adds up for its context is unchanged.
    assert translator.usage.input_tokens == 7
    assert translator.usage.output_tokens == 2
    assert translator.usage.cache_read_input_tokens == 3
    assert translator.usage.reasoning_tokens == 1
    assert '"cache_read_input_tokens": 3' in text and '"reasoning_tokens": 1' in text


def test_the_flat_cached_field_that_does_not_exist_is_not_read():
    # The bug: a flat `cached_input_tokens` was read, so every Codex request
    # was recorded as fully uncached — which made "is caching working?"
    # unanswerable from our own data.
    translator = ResponseTranslator()
    list(translator.feed({"type": "response.completed", "response": {"usage": {
        "input_tokens": 50_000, "cached_input_tokens": 49_000, "output_tokens": 10}}}))
    assert translator.usage.cache_read_input_tokens == 0
    assert translator.usage.input_tokens == 50_000


def test_cached_tokens_never_exceed_the_input_total():
    translator = ResponseTranslator()
    list(translator.feed({"type": "response.completed", "response": {"usage": {
        "input_tokens": 5, "input_tokens_details": {"cached_tokens": 9}, "output_tokens": 1}}}))
    assert (translator.usage.input_tokens, translator.usage.cache_read_input_tokens) == (0, 5)


def test_tool_call_turn_sets_tool_use_stop_reason():
    translator = ResponseTranslator()
    events = [
        {"type": "response.created", "response": {"model": "gpt-5.6-terra"}},
        {"type": "response.output_item.added", "item": {"type": "function_call", "call_id": "call_1", "name": "Bash"}},
        {"type": "response.function_call_arguments.delta", "delta": '{"command"'},
        {"type": "response.function_call_arguments.delta", "delta": ':"ls"}'},
        {"type": "response.output_item.done", "item": {"type": "function_call"}},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 5, "output_tokens": 3}}},
    ]
    out = b""
    for event in events:
        out += _sse_bytes(translator.feed(event))
    text = out.decode()

    assert '"type": "tool_use", "id": "call_1", "name": "Bash"' in text
    assert '"type": "input_json_delta", "partial_json": "{\\"command\\""' in text
    assert '"stop_reason": "tool_use"' in text


def test_tool_call_without_an_id_still_gets_one():
    # An empty id breaks the client's pairing of tool_result to tool_use, and
    # Claude Code renders a tool_use block arriving without one as an error.
    translator = ResponseTranslator()
    events = [
        {"type": "response.created", "response": {"model": "gpt-5.6-terra"}},
        {"type": "response.output_item.added", "item": {"type": "function_call", "name": "Bash"}},
    ]
    out = b""
    for event in events:
        out += _sse_bytes(translator.feed(event))
    block = json.loads(out.decode().split("data: ")[-1].strip())["content_block"]

    assert block["type"] == "tool_use"
    assert block["id"].startswith("toolu_")


def test_response_failed_sets_error_stop_reason():
    translator = ResponseTranslator()
    list(translator.feed({"type": "response.created", "response": {}}))
    out = b"".join(translator.feed({"type": "response.failed", "response": {"usage": {}}}))
    assert b'"stop_reason": "error"' in out


def test_unrecognized_event_type_is_silently_ignored():
    translator = ResponseTranslator()
    # A future or unknown event type must never raise: the translator
    # deliberately avoids an exhaustive match.
    assert list(translator.feed({"type": "response.some_future_event"})) == []


def test_message_start_only_fires_once():
    translator = ResponseTranslator()
    first = b"".join(translator.feed({"type": "response.created", "response": {}}))
    second = b"".join(translator.feed({"type": "response.created", "response": {}}))
    assert b"message_start" in first
    assert second == b""


# ---- a failed stream must not read as a successful empty answer ------------

def _sse(events):
    return [f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode("utf-8") for e in events]


def test_a_stream_that_errors_assembles_into_an_error_body():
    """The non-streaming path used to ignore `error` events and return a 200
    with no content, which the client reports as "produced an empty
    response" — hiding every upstream failure behind a wrong explanation."""
    chunks = _sse([
        {"type": "message_start", "message": {"id": "msg_1", "model": "gpt-test"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "half"}},
        {"type": "error", "error": {"type": "api_error", "message": "upstream stream failed: timeout"}},
    ])
    body = openai_translate.assemble_message_from_sse(iter(chunks))
    assert body["type"] == "error"
    assert "timeout" in body["error"]["message"]


def test_a_stream_that_never_started_assembles_into_an_error_body():
    body = openai_translate.assemble_message_from_sse(iter([]))
    assert body["type"] == "error" and "no response" in body["error"]["message"]


def test_a_normal_stream_still_assembles_into_a_message():
    chunks = _sse([
        {"type": "message_start", "message": {"id": "msg_1", "model": "gpt-test"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
        {"type": "message_stop"},
    ])
    body = openai_translate.assemble_message_from_sse(iter(chunks))
    assert body["type"] == "message" and body["content"][0]["text"] == "hello"


def _translate(events):
    t = ResponseTranslator()
    out = b""
    for e in events:
        out += b"".join(t.feed(e))
    return out.decode("utf-8")


def test_a_message_item_with_no_text_produces_no_empty_block():
    """Anthropic rejects an empty text block on the NEXT request ("400
    messages: text content blocks must be non-empty"), and Claude Code keeps
    whatever we send in its history — so one empty block here breaks the
    conversation later, usually on a different account."""
    out = _translate([
        {"type": "response.created", "response": {"id": "r1", "model": "gpt-test"}},
        {"type": "response.output_item.added", "item": {"type": "message"}},
        {"type": "response.output_item.done", "item": {"type": "message"}},
        {"type": "response.output_item.added",
         "item": {"type": "function_call", "call_id": "call_1", "name": "Read"}},
        {"type": "response.function_call_arguments.delta", "delta": '{"path":"a"}'},
        {"type": "response.output_item.done", "item": {"type": "function_call"}},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 1, "output_tokens": 1}}},
    ])
    assert '"type": "text"' not in out
    assert '"type": "tool_use"' in out
    # The one block that IS emitted must still be index 0: a skipped index
    # leaves a gap the client has to reconcile.
    assert '"index": 0' in out and '"index": 1' not in out


def test_a_message_item_with_text_still_produces_one_text_block():
    out = _translate([
        {"type": "response.created", "response": {"id": "r1", "model": "gpt-test"}},
        {"type": "response.output_item.added", "item": {"type": "message"}},
        {"type": "response.output_text.delta", "delta": "hello"},
        {"type": "response.output_item.done", "item": {"type": "message"}},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 1, "output_tokens": 1}}},
    ])
    assert out.count('"type": "content_block_start"') == 1
    assert '"text": "hello"' in out


def test_an_assembled_message_never_carries_an_empty_text_block():
    chunks = _sse([
        {"type": "message_start", "message": {"id": "msg_1", "model": "gpt-test"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "call_1", "name": "Read", "input": {}}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": '{"path":"a"}'}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 2}},
    ])
    body = openai_translate.assemble_message_from_sse(iter(chunks))
    assert [b["type"] for b in body["content"]] == ["tool_use"]


# ---- tool arguments delivered without delta events ----

def _tool_item_event(kind, number=0, arguments=None):
    item = {"type": "function_call", "id": f"fc_{number}",
            "call_id": f"call_{number}", "name": "Skill"}
    if arguments is not None:
        item["arguments"] = arguments
    return {"type": f"response.output_item.{kind}", "output_index": number,
            "item": item}


def _argument_event(kind, number=0, text='', key="item_id"):
    event = {"type": f"response.function_call_arguments.{kind}",
             "delta" if kind == "delta" else "arguments": text}
    if key == "item_id":
        event[key] = f"fc_{number}"
    elif key == "output_index":
        event[key] = number
    return event


def _tool_stream(events):
    events = [{"type": "response.created", "response": {}}] + events
    events.append({"type": "response.completed", "response": {}})
    out = _translate(events)
    return [json.loads(line[6:]) for line in out.splitlines() if line.startswith("data: ")]


def _tool_arguments(events):
    # Verify ordering for every block, including interleaved closures.
    opened, stopped, parts = set(), set(), {}
    for event in events:
        kind, index = event["type"], event.get("index")
        if kind == "content_block_start":
            assert index not in opened
            opened.add(index)
            parts[index] = []
        elif kind == "content_block_delta":
            assert index in opened and index not in stopped
            parts[index].append(event["delta"]["partial_json"])
        elif kind == "content_block_stop":
            assert index in opened and index not in stopped
            stopped.add(index)
    assert opened == stopped
    return parts


@pytest.mark.parametrize("key", ["item_id", "output_index", None])
def test_tool_arguments_done_only(key):
    arguments = '{"skill":"review"}'
    translator = ResponseTranslator()
    list(translator.feed(_tool_item_event("added")))
    immediate = list(translator.feed(_argument_event("done", text=arguments, key=key)))
    assert len(immediate) == 1
    assert json.loads(immediate[0].decode().split("data: ")[1])["delta"] == {
        "type": "input_json_delta", "partial_json": arguments,
    }
    out = _tool_stream([
        _tool_item_event("added"),
        _argument_event("done", text=arguments, key=key),
        _tool_item_event("done", arguments=arguments),
    ])
    assert _tool_arguments(out) == {0: [arguments]}
    assert out[-2]["delta"]["stop_reason"] == "tool_use"


def test_tool_arguments_output_item_done_only_after_empty_events():
    arguments = '{"skill":"review"}'
    out = _tool_stream([
        _tool_item_event("added"),
        _argument_event("delta"),
        _argument_event("done"),
        _tool_item_event("done", arguments=arguments),
    ])
    assert _tool_arguments(out) == {0: [arguments]}


@pytest.mark.parametrize("key", ["item_id", "output_index"])
def test_tool_arguments_delta_only_ignores_unknown_and_closed_calls(key):
    arguments = '{"skill":"review"}'
    out = _tool_stream([
        _tool_item_event("added"),
        _argument_event("delta", number=99, text='{"wrong":true}', key=key),
        _argument_event("delta", text=arguments[:10], key=key),
        _argument_event("delta", text=arguments[10:], key=key),
        _tool_item_event("done"),
        _tool_item_event("added", number=1),
        _argument_event("delta", text='{"late":true}', key=key),
        _argument_event("delta", number=1, text='{}', key=key),
        _tool_item_event("done", number=1),
    ])
    assert _tool_arguments(out) == {0: [arguments[:10], arguments[10:]], 1: ['{}']}


@pytest.mark.parametrize("source", ["done", "output_item.done"])
@pytest.mark.parametrize("different", ['{"skill":"different-private-input"}', ''])
def test_tool_arguments_delta_plus_done_keeps_deltas_and_warns_on_mismatch(source, different, capsys):
    arguments = '{"skill":"private-input"}'
    matching = (_argument_event("done", text=arguments) if source == "done"
                else _tool_item_event("done", arguments=arguments))
    out = _tool_stream([
        _tool_item_event("added"),
        _argument_event("delta", text=arguments),
        matching,
        _tool_item_event("done", arguments=arguments),
    ])
    assert _tool_arguments(out) == {0: [arguments]}
    assert capsys.readouterr().err == ""

    mismatching = (_argument_event("done", text=different) if source == "done"
                   else _tool_item_event("done", arguments=different))
    out = _tool_stream([
        _tool_item_event("added"),
        _argument_event("delta", text=arguments[:10]),
        _argument_event("delta", text=arguments[10:]),
        mismatching,
        _tool_item_event("done", arguments=different),
    ])
    assert _tool_arguments(out) == {0: [arguments[:10], arguments[10:]]}
    warning = capsys.readouterr().err
    assert warning.count("WARNING") == 1
    assert "Skill" in warning and "arguments differ" in warning
    assert "private-input" not in warning


@pytest.mark.parametrize("key", ["item_id", "output_index"])
@pytest.mark.parametrize("delivery", ["delta", "done", "output_item.done"])
def test_tool_arguments_interleaved_calls(key, delivery):
    first, second = '{"skill":"first"}', '{"skill":"second"}'
    added = [_tool_item_event("added", number=n) for n in (0, 1)]
    closed = [_tool_item_event("done", number=n, arguments=args)
              for n, args in ((0, first), (1, second))]
    if key == "item_id":
        for event in added + closed:
            event.pop("output_index")
    else:
        for event in added + closed:
            event["item"].pop("id")
    if delivery == "delta":
        middle = [
            _argument_event("delta", 0, first[:10], key),
            _argument_event("delta", 1, second[:10], key),
            _argument_event("delta", 0, first[10:], key),
            closed[0],
            _argument_event("delta", 1, second[10:], key),
            closed[1],
        ]
        expected = {0: [first[:10], first[10:]], 1: [second[:10], second[10:]]}
    elif delivery == "done":
        for event in closed:
            event["item"].pop("arguments")
        middle = [_argument_event("done", 0, first, key), closed[0],
                  _argument_event("done", 1, second, key), closed[1]]
        expected = {0: [first], 1: [second]}
    else:
        middle, expected = closed, {0: [first], 1: [second]}
    assert _tool_arguments(_tool_stream(added + middle)) == expected


def test_tool_arguments_done_only_non_streaming_assembly():
    out = _tool_stream([
        _tool_item_event("added"),
        _argument_event("done", text='{"skill":"review"}'),
        _tool_item_event("done"),
    ])
    message = openai_translate.assemble_message_from_sse(iter(_sse(out)))
    assert message["content"] == [
        {"type": "tool_use", "id": "call_0", "name": "Skill", "input": {"skill": "review"}},
    ]
    assert message["stop_reason"] == "tool_use"


def test_tool_arguments_missing_warning_is_rate_limited_across_requests(monkeypatch, capsys):
    import time

    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(openai_translate, "_missing_arguments_warning_at", float("-inf"), raising=False)
    events = [_tool_item_event("added"), _argument_event("delta"),
              _argument_event("done"), _tool_item_event("done"), _tool_item_event("done")]
    for _ in range(2):
        assert _tool_arguments(_tool_stream(events)) == {0: []}
    warning = capsys.readouterr().err
    assert warning.count("WARNING") == 1
    assert "Skill" in warning and "no arguments" in warning
    for kind in ("output_item.added", "function_call_arguments.delta",
                 "function_call_arguments.done", "output_item.done"):
        assert f"response.{kind}" in warning
    now[0] += 60
    _tool_stream(events)
    assert capsys.readouterr().err.count("WARNING") == 1


@pytest.mark.parametrize("terminal", ["response.completed", "response.failed", "abort"])
def test_tool_arguments_all_open_calls_close_on_terminal(terminal):
    translator = ResponseTranslator()
    chunks = []
    for event in [_tool_item_event("added"), _tool_item_event("added", number=1),
                  _argument_event("delta", 0, '{}'), _argument_event("delta", 1, '{}')]:
        chunks.extend(translator.feed(event))
    chunks.extend(translator.abort("test failure") if terminal == "abort"
                  else translator.feed({"type": terminal, "response": {}}))
    out = [json.loads(chunk.decode().split("data: ")[1]) for chunk in chunks]
    assert _tool_arguments(out) == {0: ['{}'], 1: ['{}']}


def test_tool_arguments_reasoning_done_does_not_close_tool_call():
    out = _tool_stream([
        _tool_item_event("added"),
        {"type": "response.output_item.done", "item": {"type": "reasoning"}},
        _argument_event("delta", text='{}'),
        _tool_item_event("done"),
    ])
    assert _tool_arguments(out) == {0: ['{}']}
