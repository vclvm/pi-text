import asyncio
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

import proxy

BASE = "https://router.example/v1"


def tools_payload(stream=False, **extra):
    payload = {
        "model": "qwen-actual-id", "stream": stream,
        "messages": [{"role": "system", "content": "You are an agent."},
                     {"role": "user", "content": "List the files in this directory."}],
        "tools": [
            {"type": "function", "function": {
                "name": "bash", "description": "run a shell command",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                               "required": ["command"], "additionalProperties": False}}},
            {"type": "function", "function": {
                "name": "write", "description": "write a file",
                "parameters": {"type": "object",
                               "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                               "required": ["path", "content"]}}},
        ],
        "tool_choice": "auto", "parallel_tool_calls": True, "store": False,
        "stream_options": {"include_usage": True},
    }
    payload.update(extra)
    return payload


def all_text(obj):
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return "\n".join(all_text(v) for v in obj.values())
    if isinstance(obj, list):
        return "\n".join(all_text(v) for v in obj)
    return ""


def nonce_of(upstream_json):
    match = re.search(r'<tool_call id="([0-9a-f]+)">', all_text(upstream_json))
    return match.group(1) if match else None


def call_block(nonce, name, arguments):
    return f'<tool_call id="{nonce}">\n' + json.dumps({"name": name, "arguments": arguments}) + "\n</tool_call>"


class FakeRouter:
    """Upstream double. `replies` items are callables (nonce -> str content) or
    (status, body) tuples; every request is recorded in `requests`."""

    def __init__(self, *replies, stream=False, chunk_size=7, finish="stop", reasoning=None):
        self.replies, self.stream, self.chunk_size = list(replies), stream, chunk_size
        self.finish, self.reasoning, self.requests = finish, reasoning, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "json": body})
        reply = self.replies.pop(0)
        if isinstance(reply, tuple):
            return httpx.Response(reply[0], json=reply[1])
        content = reply(nonce_of(body))
        usage = {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125}
        if not self.stream:
            message = {"role": "assistant", "content": content}
            if self.reasoning:
                message["reasoning_content"] = self.reasoning
            return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": self.finish}],
                                             "usage": usage})
        lines = []
        if self.reasoning:
            lines.append({"choices": [{"index": 0, "delta": {"reasoning_content": self.reasoning}}]})
        for i in range(0, len(content), self.chunk_size):
            lines.append({"choices": [{"index": 0, "delta": {"content": content[i:i + self.chunk_size]}}]})
        lines.append({"choices": [{"index": 0, "delta": {}, "finish_reason": self.finish}]})
        lines.append({"choices": [], "usage": usage})
        sse = "".join(f"data: {json.dumps(x)}\n\n" for x in lines) + "data: [DONE]\n\n"
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})


def make_client(router, **overrides):
    settings = proxy.Settings(upstream_url=overrides.pop("url", BASE), upstream_api_key="secret",
                              upstream_stream=router.stream, **overrides)
    return TestClient(proxy.create_app(settings, transport=httpx.MockTransport(router)))


def sse_events(response):
    data = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert data[-1] == "[DONE]", response.text
    return [json.loads(d) for d in data[:-1]]


def deltas(events, key):
    return "".join(e["choices"][0]["delta"].get(key) or "" for e in events if e["choices"])


# --------------------------------------------------------------------------- end to end

def test_text_before_call_non_stream():
    router = FakeRouter(lambda n: "Let me look.\n" + call_block(n, "bash", {"command": "ls"}))
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
    assert r.status_code == 200, r.text
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "Let me look."
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "bash"
    assert json.loads(call["function"]["arguments"]) == {"command": "ls"}
    assert r.json()["usage"]["total_tokens"] == 125
    sent = router.requests[0]
    assert sent["url"] == f"{BASE}/chat/completions"
    assert sent["headers"]["authorization"] == "Bearer secret"
    assert not {"tools", "tool_choice", "parallel_tool_calls", "store", "stream_options"} & set(sent["json"])
    assert sent["json"]["stream"] is False


def test_streaming_with_tag_split_across_chunks():
    router = FakeRouter(lambda n: "Checking.\n" + call_block(n, "bash", {"command": "pwd"}),
                        stream=True, chunk_size=3)
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=True))
    assert r.status_code == 200 and "text/event-stream" in r.headers["content-type"]
    events = sse_events(r)
    assert router.requests[0]["json"]["stream"] is True
    assert router.requests[0]["json"]["stream_options"] == {"include_usage": True}
    text = deltas(events, "content")
    assert text.strip() == "Checking." and "<" not in text
    calls = [e["choices"][0]["delta"]["tool_calls"] for e in events
             if e["choices"] and "tool_calls" in e["choices"][0]["delta"]]
    assert calls[0][0]["function"]["name"] == "bash" and calls[0][0]["index"] == 0
    assert [e["choices"][0]["finish_reason"] for e in events if e["choices"]][-1] == "tool_calls"
    assert events[-1]["usage"]["total_tokens"] == 125


def test_think_tags_and_reasoning_content_become_reasoning():
    router = FakeRouter(lambda n: "<think>secret plan</think>\nAll done.", stream=True,
                        chunk_size=4, reasoning="native reasoning. ")
    with make_client(router) as client:
        events = sse_events(client.post("/v1/chat/completions", json=tools_payload(stream=True)))
    assert deltas(events, "content") == "All done."
    assert deltas(events, "reasoning_content") == "native reasoning. secret plan"
    assert [e["choices"][0]["finish_reason"] for e in events if e["choices"]][-1] == "stop"


def test_multiple_calls_in_one_reply():
    router = FakeRouter(lambda n: call_block(n, "bash", {"command": "ls"}) + "\n"
                        + call_block(n, "bash", {"command": "pwd"}))
    with make_client(router) as client:
        message = client.post("/v1/chat/completions", json=tools_payload()).json()["choices"][0]["message"]
    assert [json.loads(c["function"]["arguments"])["command"] for c in message["tool_calls"]] == ["ls", "pwd"]
    assert message["content"] is None


def test_code_containing_closing_tag_and_fences():
    code = 'print("</tool_call>")\nx = "{"\n'
    router = FakeRouter(lambda n: f'```xml\n<tool_call id="{n}">\n```json\n'
                        + json.dumps({"name": "write", "arguments": {"path": "a.py", "content": code}})
                        + "\n```\n</tool_call>\n```")
    with make_client(router) as client:
        message = client.post("/v1/chat/completions", json=tools_payload()).json()["choices"][0]["message"]
    assert json.loads(message["tool_calls"][0]["function"]["arguments"])["content"] == code


def test_retry_after_schema_mismatch():
    router = FakeRouter(lambda n: call_block(n, "bash", {"cmd": "ls"}),
                        lambda n: call_block(n, "bash", {"command": "ls"}))
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["finish_reason"] == "tool_calls"
    assert len(router.requests) == 2
    retry_messages = router.requests[1]["json"]["messages"]
    assert retry_messages[-2]["role"] == "assistant" and '"cmd"' in retry_messages[-2]["content"]
    assert retry_messages[-1]["role"] == "user" and retry_messages[-1]["content"].startswith("[bridge]")


def test_retry_text_not_duplicated_in_stream():
    router = FakeRouter(lambda n: "Looking.\n" + call_block(n, "nope", {}),
                        lambda n: "Looking.\n" + call_block(n, "bash", {"command": "ls"}), stream=True)
    with make_client(router) as client:
        events = sse_events(client.post("/v1/chat/completions", json=tools_payload(stream=True)))
    assert deltas(events, "content").strip() == "Looking."


def test_retries_exhausted_gives_openai_error():
    router = FakeRouter(*[lambda n: call_block(n, "nope", {})] * 3)
    with make_client(router, max_retries=2) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "bridge_protocol_error"
    assert "Unknown tool" in r.json()["error"]["message"]
    assert len(router.requests) == 3


def test_retries_exhausted_in_stream_sends_error_event():
    router = FakeRouter(lambda n: call_block(n, "nope", {}), stream=True)
    with make_client(router, max_retries=0) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=True))
    error_events = [json.loads(line[6:]) for line in r.text.splitlines()
                    if line.startswith("data: {") and '"error"' in line]
    assert error_events and error_events[0]["error"]["type"] == "bridge_protocol_error"


def test_nonce_required_by_default():
    router = FakeRouter(lambda n: '<tool_call>{"name":"bash","arguments":{"command":"ls"}}</tool_call>')
    with make_client(router, max_retries=0) as client:
        assert client.post("/v1/chat/completions", json=tools_payload()).status_code == 502
    router = FakeRouter(lambda n: '<tool_call>{"name":"bash","arguments":{"command":"ls"}}</tool_call>')
    with make_client(router, max_retries=0, require_nonce=False) as client:
        assert client.post("/v1/chat/completions", json=tools_payload()).status_code == 200
    router = FakeRouter(lambda n: call_block("deadbeef", "bash", {"command": "ls"}))
    with make_client(router, max_retries=0, require_nonce=False) as client:
        assert client.post("/v1/chat/completions", json=tools_payload()).status_code == 502


def test_required_tool_choice_retries_plain_text():
    router = FakeRouter(lambda n: "I would run ls.", lambda n: call_block(n, "bash", {"command": "ls"}))
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(tool_choice="required"))
    assert r.json()["choices"][0]["finish_reason"] == "tool_calls"
    assert len(router.requests) == 2


def test_truncated_call_is_not_retried_or_repaired():
    router = FakeRouter(lambda n: f'<tool_call id="{n}">\n{{"name": "write", "arguments": {{"path": "a", "content": "hal',
                        finish="length")
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
    assert r.status_code == 502 and "cut off" in r.json()["error"]["message"]
    assert len(router.requests) == 1


def test_length_finish_reason_propagates_for_text():
    router = FakeRouter(lambda n: "A long answer that was cut", finish="length")
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
    assert r.json()["choices"][0]["finish_reason"] == "length"


def test_hallucinated_tool_result_is_dropped():
    router = FakeRouter(lambda n: call_block(n, "bash", {"command": "ls"})
                        + '\n<tool_result name="bash">a.txt</tool_result>\nThere is a.txt')
    with make_client(router) as client:
        message = client.post("/v1/chat/completions", json=tools_payload()).json()["choices"][0]["message"]
    assert message["content"] is None and len(message["tool_calls"]) == 1


def test_lenient_json_repair_for_complete_block():
    router = FakeRouter(lambda n: f'<tool_call id="{n}">\n{{"name": "bash", "arguments": {{"command": "ls",}}}}\n</tool_call>')
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
    assert json.loads(r.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]) == {"command": "ls"}


def test_upstream_error_status_and_body_pass_through():
    router = FakeRouter((429, {"error": {"message": "slow down", "type": "rate_limit", "code": "rate_limit"}}))
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
    assert r.status_code == 429
    assert r.json()["error"] == {"message": "upstream: slow down", "type": "rate_limit", "code": "rate_limit"}

    router = FakeRouter((401, {"error": "bad key"}), stream=True)
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=True))
    assert r.status_code == 401 and "bad key" in r.json()["error"]["message"]


def test_content_type_required_and_no_client_key():
    router = FakeRouter(lambda n: "hi")
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", content=json.dumps(tools_payload()),
                        headers={"Content-Type": "text/plain"})
        assert r.status_code == 415
        assert client.post("/v1/chat/completions", json=tools_payload()).status_code == 200
    assert len(router.requests) == 1


def test_bad_requests_are_400():
    with make_client(FakeRouter()) as client:
        assert client.post("/v1/chat/completions", json={"model": "x", "messages": []}).status_code == 400
        bad = tools_payload(messages=[{"role": "user", "content": [42]}])
        assert client.post("/v1/chat/completions", json=bad).status_code == 400
        image = tools_payload(messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://invalid"}}]}])
        r = client.post("/v1/chat/completions", json=image)
        assert r.status_code == 400 and "Only text" in r.json()["error"]["message"]


# --------------------------------------------------------------------------- request building

def conversation():
    payload = tools_payload()
    payload["messages"] += [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": '{"command": "ls"}'}},
            {"id": "call_2", "type": "function", "function": {"name": "bash", "arguments": '{"command": "pwd"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "a.txt"},
        {"role": "tool", "tool_call_id": "call_2", "content": [{"type": "text", "text": "/tmp"},
                                                              {"type": "image_url", "image_url": {"url": "x"}}]},
    ]
    return payload


def test_history_uses_protocol_format_and_alternates_roles():
    settings = proxy.Settings()
    prep = proxy.prepare_request(conversation(), settings)
    messages = prep.upstream["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[2]["content"] == (proxy.render_call("bash", {"command": "ls"}, prep.nonce) + "\n"
                                      + proxy.render_call("bash", {"command": "pwd"}, prep.nonce))
    assert '<tool_result name="bash" call_id="call_1">\na.txt\n</tool_result>' in messages[3]["content"]
    assert "[image_url content omitted" in messages[3]["content"]
    assert messages[3]["content"].rstrip().endswith(messages[3]["content"].split("# Tool use")[-1].rstrip())
    assert "# Tool use" in messages[3]["content"] and "# Tool use" not in messages[1]["content"]
    # The history call is parseable by the bridge itself.
    calls, _ = proxy.parse_tool_calls(messages[2]["content"], prep.tools, prep.nonce)
    assert [c["function"]["name"] for c in calls] == ["bash", "bash"]


def test_nonce_stable_within_conversation_and_secret_dependent():
    settings = proxy.Settings()
    first = proxy.prepare_request(tools_payload(), settings)
    later = proxy.prepare_request(conversation(), settings)
    assert first.nonce == later.nonce
    assert proxy.prepare_request(tools_payload(), proxy.Settings()).nonce != first.nonce
    other = tools_payload(messages=[{"role": "user", "content": "something else"}])
    assert proxy.prepare_request(other, settings).nonce != first.nonce


def test_system_injection_merges_into_single_system_message():
    settings = proxy.Settings(injection_role="system")
    payload = conversation()
    payload["messages"].insert(1, {"role": "developer", "content": "Be brief."})
    messages = proxy.prepare_request(payload, settings).upstream["messages"]
    assert [m["role"] for m in messages].count("system") == 1
    assert messages[0]["content"].startswith("You are an agent.\n\nBe brief.\n\n# Tool use")


def test_no_tools_means_no_protocol():
    payload = tools_payload(tool_choice="none")
    prep = proxy.prepare_request(payload, proxy.Settings())
    assert not prep.tools and "# Tool use" not in json.dumps(prep.upstream)
    calls, text = [], ""
    scanner = proxy.ReplyScanner(detect_calls=False)
    out = scanner.feed("see <tool_call> docs")
    tail, raw = scanner.finish()
    assert raw is None and "".join(v for _, v in out + tail) == "see <tool_call> docs"
    assert calls == [] and text == ""


def test_reasoning_effort_forwarded():
    prep = proxy.prepare_request(tools_payload(reasoning_effort="high"), proxy.Settings(upstream_stream=False))
    assert prep.upstream["reasoning_effort"] == "high"
    assert "stream_options" not in prep.upstream


# --------------------------------------------------------------------------- helpers

def test_with_pings_emits_keepalives():
    async def slow():
        await asyncio.sleep(0.25)
        yield "x"

    async def run():
        return [item async for item in proxy.with_pings(slow(), 0.05)]

    items = asyncio.run(run())
    assert items[-1] == "x" and items.count(None) >= 2


def test_scanner_holds_partial_tags():
    scanner = proxy.ReplyScanner(detect_calls=True)
    assert scanner.feed("Hello <too") == [("text", "Hello ")]
    assert scanner.feed("l_call id") == []
    tail, raw = scanner.finish()
    assert raw == "<tool_call id" and tail == []


@pytest.mark.parametrize("content", ["plain", "<think>a</think>b", "x <thin", "<think>unterminated"])
def test_scanner_preserves_all_characters(content):
    scanner = proxy.ReplyScanner(detect_calls=True)
    out = []
    for ch in content:
        out += scanner.feed(ch)
    tail, raw = scanner.finish()
    joined = "".join(v for _, v in out + tail)
    assert joined == content.replace("<think>", "").replace("</think>", "") and raw is None


# --------------------------------------------------------------------------- endpoint resolution

@pytest.mark.parametrize("url,api,expected", [
    ("https://r/v1", "", ("https://r/v1/chat/completions", "chat")),
    ("https://r/v1/", "", ("https://r/v1/chat/completions", "chat")),
    ("https://r/v1/chat/completions", "", ("https://r/v1/chat/completions", "chat")),
    ("https://openrouter.ai/api/v1/responses", "", ("https://openrouter.ai/api/v1/responses", "responses")),
    ("https://r/v1", "responses", ("https://r/v1/responses", "responses")),
    ("https://r/v1/responses", "RESPONSES", ("https://r/v1/responses", "responses")),
])
def test_resolve_endpoint(url, api, expected):
    assert proxy.resolve_endpoint(url, api) == expected


@pytest.mark.parametrize("url,api,message", [
    ("https://r/v1/completions", "", "legacy"),
    ("https://r/v1/chat/completions", "responses", "ends with"),
    ("https://r/v1", "completions", "legacy"),
    ("https://r/v1", "anthropic", "must be one of"),
])
def test_resolve_endpoint_errors(url, api, message):
    with pytest.raises(ValueError, match=message):
        proxy.resolve_endpoint(url, api)


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("PI_TEXT_URL", "https://openrouter.ai/api/v1/responses")
    monkeypatch.setenv("PI_TEXT_KEY", "k")
    monkeypatch.delenv("PI_TEXT_API", raising=False)
    s = proxy.Settings.from_env()
    assert (s.endpoint, s.api, s.upstream_api_key) == ("https://openrouter.ai/api/v1/responses", "responses", "k")


# --------------------------------------------------------------------------- Responses API

class FakeResponses:
    """Responses API double. style: "openai" (output_text.delta / response.completed),
    "openrouter" (content_part.delta / response.done) or "final-only" (no deltas)."""

    def __init__(self, *replies, stream=False, style="openai", status="completed",
                 incomplete_reason=None, reasoning=None, fail=None, chunk_size=6):
        self.replies, self.stream, self.style, self.status = list(replies), stream, style, status
        self.incomplete_reason, self.reasoning, self.fail = incomplete_reason, reasoning, fail
        self.chunk_size, self.requests = chunk_size, []

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "json": body})
        text = self.replies.pop(0)(nonce_of(body))
        output = []
        if self.reasoning:
            output.append({"type": "reasoning", "summary": [{"type": "summary_text", "text": self.reasoning}]})
        output.append({"type": "message", "role": "assistant", "id": "msg_1", "status": "completed",
                       "content": [{"type": "output_text", "text": text, "annotations": []}]})
        final = {"id": "resp_1", "object": "response", "status": self.status, "output": output,
                 "usage": {"input_tokens": 80, "output_tokens": 20, "total_tokens": 100,
                           "input_tokens_details": {"cached_tokens": 64}}}
        if self.incomplete_reason:
            final["incomplete_details"] = {"reason": self.incomplete_reason}
        if self.fail:
            final.update(status="failed", error={"code": "server_error", "message": self.fail})
        if not self.stream:
            return httpx.Response(200, json=final)
        text_event, reasoning_event, done_event = {
            "openai": ("response.output_text.delta", "response.reasoning_summary_text.delta", "response.completed"),
            "openrouter": ("response.content_part.delta", "response.reasoning.delta", "response.done"),
            "final-only": (None, None, "response.completed"),
        }[self.style]
        events = [{"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}}]
        if reasoning_event and self.reasoning:
            events.append({"type": reasoning_event, "delta": self.reasoning})
        if text_event:
            events += [{"type": text_event, "delta": text[i:i + self.chunk_size]}
                       for i in range(0, len(text), self.chunk_size)]
            events.append({"type": "response.output_text.done", "text": text})
        events.append({"type": "response.failed" if self.fail else done_event, "response": final})
        sse = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})


RESPONSES_URL = "https://openrouter.ai/api/v1/responses"


def test_responses_request_shape_and_tool_call():
    router = FakeResponses(lambda n: "Looking.\n" + call_block(n, "bash", {"command": "ls"}))
    with make_client(router, url=RESPONSES_URL) as client:
        r = client.post("/v1/chat/completions",
                        json=tools_payload(max_tokens=512, reasoning_effort="low", temperature=0.2, stop=["x"]))
    assert r.status_code == 200, r.text
    body = router.requests[0]["json"]
    assert router.requests[0]["url"] == RESPONSES_URL
    assert body["store"] is False and body["stream"] is False
    assert body["instructions"] == "You are an agent."
    assert body["max_output_tokens"] == 512 and body["reasoning"] == {"effort": "low"}
    assert body["temperature"] == 0.2
    assert not {"messages", "tools", "tool_choice", "stop", "stream_options", "max_tokens"} & set(body)
    assert body["input"][0]["role"] == "user" and body["input"][0]["content"][0]["type"] == "input_text"
    assert "# Tool use" in body["input"][-1]["content"][0]["text"]
    result = r.json()
    message = result["choices"][0]["message"]
    assert message["content"] == "Looking." and message["tool_calls"][0]["function"]["name"] == "bash"
    assert result["choices"][0]["finish_reason"] == "tool_calls"
    assert result["usage"] == {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100,
                               "prompt_tokens_details": {"cached_tokens": 64}}


def test_responses_history_replays_assistant_as_output_text():
    payload = conversation()
    prep = proxy.prepare_request(payload, proxy.Settings())
    body = proxy.to_responses_payload(prep.upstream, proxy.Settings(upstream_url=RESPONSES_URL))
    assistant = [i for i in body["input"] if i["role"] == "assistant"][0]
    assert assistant["content"][0]["type"] == "output_text" and assistant["id"].startswith("msg_")
    assert assistant["status"] == "completed" and "<tool_call id=" in assistant["content"][0]["text"]
    assert [i["role"] for i in body["input"]] == ["user", "assistant", "user"]


@pytest.mark.parametrize("style", ["openai", "openrouter", "final-only"])
def test_responses_streaming_styles(style):
    router = FakeResponses(lambda n: "Checking.\n" + call_block(n, "bash", {"command": "pwd"}),
                           stream=True, style=style, reasoning="plan it")
    with make_client(router, url=RESPONSES_URL) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=True))
    assert r.status_code == 200, r.text
    assert router.requests[0]["json"]["stream"] is True
    events = sse_events(r)
    assert deltas(events, "content").strip() == "Checking."
    assert deltas(events, "reasoning_content") == "plan it"
    calls = [e["choices"][0]["delta"]["tool_calls"] for e in events
             if e["choices"] and "tool_calls" in e["choices"][0]["delta"]]
    assert len(calls) == 1 and json.loads(calls[0][0]["function"]["arguments"]) == {"command": "pwd"}
    assert [e["choices"][0]["finish_reason"] for e in events if e["choices"]][-1] == "tool_calls"
    assert events[-1]["usage"]["total_tokens"] == 100


def test_responses_incomplete_maps_to_length():
    router = FakeResponses(lambda n: "partial answer", status="incomplete", incomplete_reason="max_output_tokens")
    with make_client(router, url=RESPONSES_URL) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
    assert r.json()["choices"][0]["finish_reason"] == "length"


def test_responses_failed_is_an_error():
    router = FakeResponses(lambda n: "", fail="model crashed")
    with make_client(router, url=RESPONSES_URL) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
    assert r.status_code == 502 and "model crashed" in r.json()["error"]["message"]

    router = FakeResponses(lambda n: "", fail="model crashed", stream=True)
    with make_client(router, url=RESPONSES_URL) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=True))
    assert '"error"' in r.text and "model crashed" in r.text


def test_responses_retry_after_bad_call():
    router = FakeResponses(lambda n: call_block(n, "nope", {}),
                           lambda n: call_block(n, "bash", {"command": "ls"}), stream=True)
    with make_client(router, url=RESPONSES_URL) as client:
        events = sse_events(client.post("/v1/chat/completions", json=tools_payload(stream=True)))
    assert len(router.requests) == 2
    retry_input = router.requests[1]["json"]["input"]
    assert retry_input[-2]["role"] == "assistant" and retry_input[-2]["content"][0]["type"] == "output_text"
    assert retry_input[-1]["content"][0]["text"].startswith("[bridge]")
    assert any(e["choices"] and "tool_calls" in e["choices"][0]["delta"] for e in events)


def test_responses_upstream_http_error():
    def handler(request):
        return httpx.Response(400, json={"error": {"code": "invalid_prompt", "message": "store must be false"},
                                         "metadata": None})
    handler.stream = False
    with make_client(handler, url=RESPONSES_URL) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
    assert r.status_code == 400 and r.json()["error"]["message"] == "upstream: store must be false"


# --------------------------------------------------------------------------- OpenRouter extensions

OR_PROVIDER = {"only": ["wafer/fp8"], "allow_fallbacks": False}


def test_openrouter_fields_forwarded_only_when_sent():
    plain = proxy.prepare_request(tools_payload(), proxy.Settings())
    assert not {"provider", "reasoning", "usage", "top_k"} & set(plain.upstream)
    prep = proxy.prepare_request(tools_payload(provider=OR_PROVIDER, reasoning={"effort": "low"}, top_k=20),
                                 proxy.Settings())
    assert prep.upstream["provider"] == OR_PROVIDER
    assert prep.upstream["reasoning"] == {"effort": "low"} and prep.upstream["top_k"] == 20


def test_extra_body_and_headers_end_to_end_streaming():
    router = FakeRouter(lambda n: call_block(n, "bash", {"command": "ls"}), stream=True)
    with make_client(router, extra_body={"provider": OR_PROVIDER},
                     extra_headers={"X-Title": "pi-bridge"}) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=True, model="qwen/qwen3.8-27b"))
    assert r.status_code == 200
    sent = router.requests[0]
    assert sent["json"]["provider"] == OR_PROVIDER and sent["json"]["model"] == "qwen/qwen3.8-27b"
    assert sent["headers"]["x-title"] == "pi-bridge" and sent["headers"]["authorization"] == "Bearer secret"


def test_client_fields_override_extra_body():
    s = proxy.Settings(extra_body={"provider": OR_PROVIDER, "temperature": 0.1})
    prep = proxy.prepare_request(tools_payload(provider={"order": ["x"]}), s)
    assert prep.upstream["provider"] == {"order": ["x"]} and prep.upstream["temperature"] == 0.1


def test_extra_body_reserved_fields_rejected():
    with pytest.raises(ValueError, match="stream, tools"):
        proxy.Settings(extra_body={"tools": [], "stream": False})


def test_extra_settings_from_env(monkeypatch):
    monkeypatch.setenv("UPSTREAM_EXTRA_BODY", json.dumps({"provider": OR_PROVIDER}))
    monkeypatch.setenv("UPSTREAM_HEADERS", '{"HTTP-Referer": "http://localhost"}')
    s = proxy.Settings.from_env()
    assert s.extra_body == {"provider": OR_PROVIDER} and s.extra_headers == {"HTTP-Referer": "http://localhost"}
    monkeypatch.setenv("UPSTREAM_EXTRA_BODY", "[1]")
    with pytest.raises(ValueError, match="JSON object"):
        proxy.Settings.from_env()


def test_openrouter_reasoning_and_usage_details_stream():
    """OpenRouter streams reasoning as delta.reasoning, SSE comments, and reasoning token counts."""
    usage = {"prompt_tokens": 20, "completion_tokens": 300, "total_tokens": 320, "cost": 0.0001,
             "completion_tokens_details": {"reasoning_tokens": 280}}
    lines = [": OPENROUTER PROCESSING\n\n"] + [f"data: {json.dumps(x)}\n\n" for x in (
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "", "reasoning": "s-t-r-a-w..."}}]},
        {"choices": [{"index": 0, "delta": {"content": "There are 3 r's."}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": usage})] + ["data: [DONE]\n\n"]

    def handler(request):
        return httpx.Response(200, content="".join(lines).encode(), headers={"content-type": "text/event-stream"})

    settings = proxy.Settings(upstream_url="https://openrouter.ai/api/v1", upstream_stream=True)
    with TestClient(proxy.create_app(settings, transport=httpx.MockTransport(handler))) as client:
        r = client.post("/v1/chat/completions", json={
            "model": "qwen/qwen3.8-27b", "stream": True, "provider": OR_PROVIDER,
            "messages": [{"role": "user", "content": "How many r's are in the word 'strawberry'?"}]})
    events = sse_events(r)
    assert deltas(events, "content") == "There are 3 r's."
    assert deltas(events, "reasoning_content") == "s-t-r-a-w..."
    assert events[-1]["usage"]["completion_tokens_details"]["reasoning_tokens"] == 280


def test_openrouter_midstream_error_chunk():
    chunk = {"error": {"code": 502, "message": "Provider disconnected"},
             "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "error"}]}
    sse = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
    settings = proxy.Settings(upstream_url=BASE, upstream_stream=True)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=sse.encode()))
    with TestClient(proxy.create_app(settings, transport=transport)) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=True))
    data = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert "Provider disconnected" in data[-1]["error"]["message"]


def test_responses_forwards_provider_and_merges_reasoning():
    s = proxy.Settings(upstream_url=RESPONSES_URL, extra_body={"provider": OR_PROVIDER})
    prep = proxy.prepare_request(tools_payload(reasoning_effort="high", reasoning={"exclude": True},
                                               temperature=0.2, seed=1), s)
    body = proxy.to_responses_payload(prep.upstream, s)
    assert body["provider"] == OR_PROVIDER and body["temperature"] == 0.2
    assert body["reasoning"] == {"effort": "high", "exclude": True}
    assert not {"messages", "seed", "reasoning_effort"} & set(body)


@pytest.mark.parametrize("url, expected", [
    (BASE, f"{BASE}/models"),
    ("https://openrouter.ai/api/v1/chat/completions", "https://openrouter.ai/api/v1/models"),
    (RESPONSES_URL, "https://openrouter.ai/api/v1/models"),
])
def test_models_endpoint_proxied(url, expected):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"object": "list", "data": [{"id": "qwen/qwen3.8-27b"}]})

    settings = proxy.Settings(upstream_url=url, upstream_api_key="k")
    with TestClient(proxy.create_app(settings, transport=httpx.MockTransport(handler))) as client:
        r = client.get("/v1/models")
    assert r.status_code == 200 and r.json()["data"][0]["id"] == "qwen/qwen3.8-27b"
    assert str(seen[0].url) == expected and seen[0].method == "GET"
    assert seen[0].headers["authorization"] == "Bearer k"


def test_models_endpoint_error_status():
    transport = httpx.MockTransport(lambda r: httpx.Response(401, json={"error": {"message": "no auth"}}))
    with TestClient(proxy.create_app(proxy.Settings(upstream_url=BASE), transport=transport)) as client:
        r = client.get("/v1/models")
    assert r.status_code == 401 and "no auth" in r.json()["error"]["message"]
