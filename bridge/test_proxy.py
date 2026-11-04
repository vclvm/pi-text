import asyncio
import email.utils
import json
import re
import time
from datetime import datetime, timezone

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
    match = re.search(r'<[A-Za-z][\w-]* id="([0-9a-f]{16})">', all_text(upstream_json))
    return match.group(1) if match else None


def call_block(nonce, name, arguments, tag="pi_tool_call"):
    return f'<{tag} id="{nonce}">\n' + json.dumps({"name": name, "arguments": arguments}) + f"\n</{tag}>"


class FakeRouter:
    """Upstream double. `replies` items are callables (nonce -> str content) or
    (status, body) tuples; every request is recorded in `requests`."""

    def __init__(self, *replies, stream=False, chunk_size=7, finish="stop", reasoning=None):
        self.replies, self.stream, self.chunk_size = list(replies), stream, chunk_size
        self.finish, self.reasoning, self.requests = finish, reasoning, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "json": body,
                              "t": time.monotonic()})
        reply = self.replies.pop(0)
        if isinstance(reply, tuple):  # (status, body) or (status, body, headers)
            return httpx.Response(reply[0], json=reply[1], headers=reply[2] if len(reply) > 2 else None)
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
    router = FakeRouter(lambda n: call_block("deadbeefdeadbeef", "bash", {"command": "ls"}))
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
    router = FakeRouter((403, {"error": {"message": "forbidden", "type": "auth", "code": "no_access"}}))
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
    assert r.status_code == 403
    assert r.json()["error"] == {"message": "upstream: forbidden", "type": "auth", "code": "no_access"}

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
    tail, raw = scanner.finish()  # never completed into a real block: it was text
    assert raw is None and tail == [("text", "<tool_call id")]


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
    assert assistant["status"] == "completed" and "<pi_tool_call id=" in assistant["content"][0]["text"]
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
    router = FakeResponses(*[lambda n: ""] * 3, fail="model crashed")
    with make_client(router, url=RESPONSES_URL) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
    assert r.status_code == 502 and "model crashed" in r.json()["error"]["message"]

    router = FakeResponses(*[lambda n: ""] * 3, fail="model crashed", stream=True)
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


# --------------------------------------------------------------------------- trace

def test_trace_shows_tool_results_retry_and_calls(tmp_path):
    log_file = tmp_path / "trace.log"
    router = FakeRouter(lambda n: call_block("0123456789abcdef", "bash", {}),
                        lambda n: call_block(n, "bash", {"command": "cat a.txt"}))
    payload = tools_payload(messages=[
        {"role": "user", "content": "show a.txt"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": '{"command": "ls"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "a.txt\n"}])
    with make_client(router, trace=str(log_file)) as client:
        assert client.post("/v1/chat/completions", json=payload).status_code == 200
    text = log_file.read_text()
    assert "<- client ran bash (call_1)" in text and '"command": "ls"' in text
    assert "<- tool result" in text and "a.txt" in text
    assert "invalid tool protocol" in text and "upstream retry 1/2" in text
    assert "-> client: call bash" in text and "cat a.txt" in text and "finish=tool_calls" in text


def test_trace_truncates(tmp_path):
    log_file = tmp_path / "t.log"
    tracer = proxy.Tracer(proxy.Settings(trace=str(log_file), trace_max=10))
    tracer("x", "a" * 50)
    assert "[40 more chars]" in log_file.read_text()


# --------------------------------------------------------------------------- upstream-parsed (native) tool calls

def native_stream(chunks):
    sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return lambda request: httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})


def tc_delta(**function):
    return {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": function}]}}]}


QWEN_NATIVE = [  # what a vLLM/sglang hermes parser makes of "<text> <tool_call>…</tool_call>"
    {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "reading all project files"}}]},
    {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "chatcmpl-tool-1", "type": "function", "function": {"name": "bash", "arguments": ""}}]}}]},
    tc_delta(arguments='{"command": '),
    tc_delta(arguments='"ls -la"}'),
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
]


def test_native_tool_calls_from_upstream_are_converted_stream():
    settings = proxy.Settings(upstream_url=BASE, upstream_stream=True)
    with TestClient(proxy.create_app(settings, transport=httpx.MockTransport(native_stream(QWEN_NATIVE)))) as c:
        r = c.post("/v1/chat/completions", json=tools_payload(stream=True))
    events = sse_events(r)
    assert deltas(events, "content").strip() == "reading all project files"
    calls = [e["choices"][0]["delta"]["tool_calls"] for e in events
             if e["choices"] and "tool_calls" in e["choices"][0]["delta"]]
    assert calls and calls[0][0]["function"]["name"] == "bash"
    assert json.loads(calls[0][0]["function"]["arguments"]) == {"command": "ls -la"}
    assert [e["choices"][0]["finish_reason"] for e in events if e["choices"]][-1] == "tool_calls"


def test_native_tool_calls_non_stream_and_invalid_one_is_retried():
    replies = [
        {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
            {"id": "x", "type": "function", "function": {"name": "nope", "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}]},
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "y", "type": "function", "function": {"name": "bash", "arguments": '{"command": "pwd"}'}}]},
            "finish_reason": "tool_calls"}]},
    ]
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=replies.pop(0)))
    with TestClient(proxy.create_app(proxy.Settings(upstream_url=BASE, upstream_stream=False),
                                     transport=transport)) as c:
        r = c.post("/v1/chat/completions", json=tools_payload())
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"]) == {"command": "pwd"}


def test_upstream_tool_calls_finish_without_calls_becomes_stop():
    chunks = [{"choices": [{"index": 0, "delta": {"content": "reading all project files"}}]},
              {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}]
    settings = proxy.Settings(upstream_url=BASE, upstream_stream=True)
    with TestClient(proxy.create_app(settings, transport=httpx.MockTransport(native_stream(chunks)))) as c:
        r = c.post("/v1/chat/completions", json=tools_payload(stream=True))
    events = sse_events(r)
    assert [e["choices"][0]["finish_reason"] for e in events if e["choices"]][-1] == "stop"
    assert not any(e["choices"] and "tool_calls" in e["choices"][0]["delta"] for e in events)


def test_responses_function_call_item_is_converted():
    final = {"id": "r", "status": "completed", "output": [
        {"type": "function_call", "call_id": "fc_1", "name": "bash", "arguments": '{"command": "ls"}'}],
        "usage": {"input_tokens": 1, "output_tokens": 1}}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=final))
    settings = proxy.Settings(upstream_url=RESPONSES_URL, upstream_stream=False)
    with TestClient(proxy.create_app(settings, transport=transport)) as c:
        r = c.post("/v1/chat/completions", json=tools_payload())
    assert r.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "bash"


# --------------------------------------------------------------------------- protocol tag

def test_default_tag_is_not_tool_call_and_tool_call_alias_works():
    router = FakeRouter(lambda n: call_block(n, "bash", {"command": "ls"}, tag="tool_call"))
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
    upstream = all_text(router.requests[0]["json"])
    assert "<pi_tool_call id=" in upstream and "<tool_call" not in upstream
    assert r.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "bash"


def test_custom_tag_end_to_end_streaming(monkeypatch):
    monkeypatch.setenv("PROTOCOL_TAG", "<act>")
    assert proxy.Settings.from_env().protocol_tag == "act"
    router = FakeRouter(lambda n: "ok\n" + call_block(n, "bash", {"command": "ls"}, tag="act"),
                        stream=True, chunk_size=2)
    with make_client(router, protocol_tag="act") as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=True))
    assert '<act id="' in all_text(router.requests[0]["json"])
    events = sse_events(r)
    assert deltas(events, "content").strip() == "ok"
    assert any(e["choices"] and "tool_calls" in e["choices"][0]["delta"] for e in events)


def test_bad_protocol_tag_rejected():
    with pytest.raises(ValueError, match="PROTOCOL_TAG"):
        proxy.Settings(protocol_tag="a b")


def test_dump_has_raw_chunks_and_trace_flags_stripped_calls(tmp_path):
    chunks = [{"choices": [{"index": 0, "delta": {"content": "reading all project files"}}]},
              {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}]
    settings = proxy.Settings(upstream_url=BASE, upstream_stream=True, debug_dump_dir=str(tmp_path / "d"),
                              trace=str(tmp_path / "t.log"))
    with TestClient(proxy.create_app(settings, transport=httpx.MockTransport(native_stream(chunks)))) as c:
        c.post("/v1/chat/completions", json=tools_payload(stream=True))
    dump = json.loads(next((tmp_path / "d").iterdir()).read_text())
    assert dump["attempts"][0]["upstream_chunks"] == chunks
    assert "returned no tool calls" in (tmp_path / "t.log").read_text()


# --------------------------------------------------------------------------- talking ABOUT the protocol

DESCRIBE_PROJECT = (
    "This bridge asks the model to write `<pi_tool_call id=\"NONCE\">` blocks; Qwen likes `<tool_call>`.\n"
    "Example from the README:\n```\n<pi_tool_call id=\"SESSION_NONCE\">\n"
    '{"name": "bash", "arguments": {"command": "ls"}}\n</pi_tool_call>\n```\n'
    "Source: `return f'<{tag} id=\"{nonce}\">\\n{body}\\n</{tag}>'`; reasoning in inline `<think>…</think>`;\n"
    "results come back as <tool_result name=\"bash\" call_id=\"x\"> blocks.")


@pytest.mark.parametrize("stream", [False, True])
def test_describing_the_protocol_is_plain_text(stream):
    router = FakeRouter(lambda n: DESCRIBE_PROJECT, stream=stream, chunk_size=3)
    with make_client(router, max_retries=0) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=stream))
    assert r.status_code == 200 and len(router.requests) == 1  # no retry
    if stream:
        events = sse_events(r)
        text, calls = deltas(events, "content"), [e for e in events if e["choices"]
                                                  and "tool_calls" in e["choices"][0]["delta"]]
        assert deltas(events, "reasoning_content") == "" and not calls
    else:
        message = r.json()["choices"][0]["message"]
        text = message["content"]
        assert "tool_calls" not in message and "reasoning_content" not in message
    assert text == DESCRIBE_PROJECT


def test_description_then_real_call():
    router = FakeRouter(lambda n: "Uses `<tool_call>` tags, see <pi_tool_call id=\"X\"> docs. Reading:\n"
                        + call_block(n, "read", {"path": "README.md"}) + "\n" + call_block(n, "bash", {"command": "ls"}))
    tools = tools_payload()["tools"] + [{"type": "function", "function": {
        "name": "read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
    with make_client(router, max_retries=0) as client:
        message = client.post("/v1/chat/completions", json=tools_payload(tools=tools)).json()["choices"][0]["message"]
    assert message["content"].startswith("Uses `<tool_call>` tags, see <pi_tool_call id=\"X\"> docs.")
    assert [c["function"]["name"] for c in message["tool_calls"]] == ["read", "bash"]


def test_quoted_block_after_real_call_stays_text():
    calls, text = proxy.parse_tool_calls(
        call_block("n" * 16, "bash", {"command": "ls"}).replace("n" * 16, "0" * 16)
        + '\nlike <tool_call id="SESSION_NONCE">{"name": "bash"}</tool_call>',
        [{"name": "bash", "parameters": {"type": "object"}}], "0" * 16)
    assert len(calls) == 1 and "SESSION_NONCE" in text


def test_think_only_at_start():
    scanner = proxy.ReplyScanner(detect_calls=True)
    out = scanner.feed("<think>plan</think>Answer mentions <think> tags.")
    tail, _ = scanner.finish()
    pieces = out + tail
    assert ("reasoning", "plan") in pieces
    assert "".join(v for k, v in pieces if k == "text") == "Answer mentions <think> tags."


def test_idless_tag_in_code_is_text_but_outside_is_a_call_attempt():
    code = "The test does:\n```python\nreply = '<tool_call>{\"name\":\"bash\",\"arguments\":{}}</tool_call>'\n```\nDone."
    router = FakeRouter(lambda n: code)
    with make_client(router, max_retries=0) as client:
        assert client.post("/v1/chat/completions", json=tools_payload()).json()["choices"][0]["message"]["content"] == code
    router = FakeRouter(lambda n: 'Running:\n<tool_call>{"name":"bash","arguments":{"command":"ls"}}</tool_call>')
    with make_client(router, max_retries=0, require_nonce=False) as client:
        message = client.post("/v1/chat/completions", json=tools_payload()).json()["choices"][0]["message"]
    assert message["tool_calls"][0]["function"]["name"] == "bash"


# --------------------------------------------------------------------------- rate limits

UNSUPPORTED = {"error": {"message": "the model you requested is unsupported. please edit your configuration "
                                    "to use an authorized model", "type": "invalid_request_error"}}


def test_parse_rate_limit_headers():
    now = 1_800_000_000.0
    info = proxy.parse_rate_limit({"x-ratelimit-remaining-requests": "3", "x-ratelimit-reset-requests": "6m0s",
                                   "x-ratelimit-remaining-tokens": "1200", "x-ratelimit-reset-tokens": "20ms",
                                   "Retry-After": "7", "content-type": "application/json"}, now=now)
    assert (info.remaining_requests, info.reset_requests, info.remaining_tokens) == (3, 360, 1200)
    assert info.reset_tokens == pytest.approx(0.02) and info.retry_after == 7
    assert "content-type" not in info.headers and "retry-after" in info.headers

    assert proxy.parse_rate_limit({"retry-after-ms": "1500", "retry-after": "9"}).retry_after == 1.5
    http_date = email.utils.formatdate(now + 30, usegmt=True)
    assert proxy.parse_rate_limit({"retry-after": http_date}, now=now).retry_after == pytest.approx(30)
    # OpenRouter: epoch milliseconds; GitHub style: epoch seconds; IETF draft: delta seconds
    assert proxy.parse_rate_limit({"x-ratelimit-reset": str(int((now + 12) * 1000))}, now=now).reset_requests \
        == pytest.approx(12)
    assert proxy.parse_rate_limit({"x-ratelimit-reset": str(int(now + 45))}, now=now).reset_requests \
        == pytest.approx(45)
    info = proxy.parse_rate_limit({"ratelimit": "limit=100, remaining=0, reset=5"}, now=now)
    assert (info.remaining_requests, info.reset_requests) == (0, 5)


@pytest.mark.parametrize("body, wait", [
    ({"error": {"message": "Rate limit exceeded. Please try again in 1m30s."}}, 90),
    ({"error": {"message": "Too many requests, retry after 20 seconds"}}, 20),
    ({"error": {"message": "limit reached", "retry_after": 12}}, 12),
    ({"error": {"message": "limit reached", "retryAfterMs": 2500}}, 2.5),
    ({"error": {"message": "quota exceeded, resets at 2027-01-15T08:01:00Z"}}, 60),
    ({"error": {"message": "quota exceeded", "reset_at": "2027-01-15T08:00:30+00:00"}}, 30),
    ("Rate limit: 10 requests per minute. Try again in 42 s", 42),
    ({"error": {"message": "slow down"}}, None),
])
def test_wait_from_429_body(body, wait):
    now = datetime(2027, 1, 15, 8, 0, 0, tzinfo=timezone.utc).timestamp()
    raw = body if isinstance(body, str) else json.dumps(body)
    got = proxy.parse_rate_limit({}, raw.encode(), now=now).retry_after
    assert got == (None if wait is None else pytest.approx(wait))


def test_429_is_waited_out_and_retried():
    ok = lambda nonce: "Done."
    router = FakeRouter((429, {"error": {"message": "slow down"}}, {"retry-after": "0.3"}), ok)
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
        assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "Done."
        limits = client.get("/bridge/limits").json()
    assert len(router.requests) == 2
    assert router.requests[1]["t"] - router.requests[0]["t"] >= 0.3
    assert limits["rate_limited_total"] == 1 and limits["last_response"]["status"] == 200


def test_long_429_wait_happens_inside_the_stream():
    router = FakeRouter((429, {"error": {"message": "slow down"}}, {"retry-after": "0.4"}),
                        lambda nonce: "Hello there.", stream=True)
    with make_client(router, ping_interval=0.1) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=True))
    assert r.status_code == 200 and ": ping" in r.text
    assert deltas(sse_events(r), "content").strip() == "Hello there."
    assert len(router.requests) == 2


def test_429_gives_up_with_retry_after_and_a_retryable_message():
    body = {"error": {"message": "Quota exceeded for this minute"}}
    router = FakeRouter((429, body, {"retry-after": "30"}))
    with make_client(router, rate_limit_max_reset=1) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 30
    error = r.json()["error"]
    assert error["message"].startswith("rate limit (429)") and "quota" not in error["message"].lower()
    assert "Quota exceeded" in error["upstream_message"]
    assert len(router.requests) == 1


def test_429_without_a_named_reset_is_retried_until_it_passes(monkeypatch):
    monkeypatch.setattr(proxy.RateLimiter, "BACKOFF_START", 0.01)
    router = FakeRouter(*[(429, {"error": {"message": "slow down"}})] * 8, lambda nonce: "Done.")
    with make_client(router, rate_limit_retry_max=0.02) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
    assert r.status_code == 200 and len(router.requests) == 9
    gaps = [b["t"] - a["t"] for a, b in zip(router.requests, router.requests[1:])]
    assert max(gaps) < 0.5  # capped at RATE_LIMIT_RETRY_MAX


def test_a_far_named_reset_is_still_tried_every_retry_max_seconds():
    limited = (429, {"error": {"message": "Limit resets at: soon"}}, {"retry-after": "60"})
    router = FakeRouter(limited, limited, lambda nonce: "Done.")
    with make_client(router, rate_limit_retry_max=0.05) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
    assert r.status_code == 200 and len(router.requests) == 3
    assert router.requests[-1]["t"] - router.requests[0]["t"] < 2


def test_named_resets_are_waited_out_however_often_they_come():
    limited = (429, {"error": {"message": "slow down"}}, {"retry-after": "0.05"})
    router = FakeRouter(*[limited] * 8, lambda nonce: "Done.")
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
    assert r.status_code == 200 and len(router.requests) == 9


def test_hard_quota_429_is_not_retried():
    router = FakeRouter((429, {"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}}))
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
    assert r.status_code == 429 and "current quota" in r.json()["error"]["message"]
    assert len(router.requests) == 1


def test_min_request_interval_spaces_requests():
    router = FakeRouter(lambda n: "a", lambda n: "b")
    with make_client(router, min_request_interval=0.3) as client:
        client.post("/v1/chat/completions", json=tools_payload())
        client.post("/v1/chat/completions", json=tools_payload())
    assert router.requests[1]["t"] - router.requests[0]["t"] >= 0.3


def test_exhausted_request_window_blocks_before_the_429():
    class Router(FakeRouter):
        def __call__(self, request):
            response = super().__call__(request)
            if len(self.requests) == 1:
                response.headers.update({"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "300ms"})
            return response

    router = Router(lambda n: "a", lambda n: "b")
    with make_client(router) as client:
        client.post("/v1/chat/completions", json=tools_payload())
        client.post("/v1/chat/completions", json=tools_payload())
    assert router.requests[1]["t"] - router.requests[0]["t"] >= 0.3


def test_unsupported_model_falls_back_and_is_remembered():
    router = FakeRouter((404, UNSUPPORTED), lambda n: "from fallback", lambda n: "again")
    with make_client(router, model_fallbacks={"qwen-actual-id": "backup-model"}) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
        assert r.status_code == 200 and r.json()["model"] == "qwen-actual-id"
        client.post("/v1/chat/completions", json=tools_payload())
        refused = client.get("/bridge/limits").json()["refused_models"]
    assert [q["json"]["model"] for q in router.requests] == ["qwen-actual-id", "backup-model", "backup-model"]
    assert "qwen-actual-id" in refused

    router = FakeRouter((404, UNSUPPORTED))  # no fallback configured: the error passes through
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
    assert r.status_code == 404 and "unsupported" in r.json()["error"]["message"]


def test_model_fallbacks_are_validated():
    with pytest.raises(ValueError):
        proxy.Settings(model_fallbacks={"a": 1})


# --------------------------------------------------------------------------- live events (pibox diag)

def monitor_events(client):
    return list(client.app.state.bridge.monitor.recent)


def test_events_show_where_the_time_went():
    router = FakeRouter((429, {"error": {"message": "slow down"}}, {"retry-after": "0.3"}),
                        lambda nonce: "Hello there.", stream=True, reasoning="hmm")
    with make_client(router, ping_interval=5) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=True))
        events = monitor_events(client)
    assert r.status_code == 200
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "request" and kinds[-1] == "end"
    for k in ("send", "headers", "rate_limited", "wait", "first_token", "reply", "done"):
        assert k in kinds, kinds
    assert len({e["rid"] for e in events}) == 1
    statuses = [e["status"] for e in events if e["kind"] == "headers"]
    assert statuses == [429, 200]
    first = next(e for e in events if e["kind"] == "first_token")
    assert first["token"] == "reasoning"
    reply = next(e for e in events if e["kind"] == "reply")
    assert reply["chars"] == len("Hello there.") and reply["reasoning_chars"] == 3
    end = events[-1]
    assert end["outcome"] == "ok" and end["spent"]["rate_limit"] >= 0.3
    assert events[0]["last"] == "List the files in this directory." and events[0]["tools"] == 2


def test_events_report_errors_and_pi_retries():
    router = FakeRouter((403, {"error": {"message": "forbidden"}}), lambda nonce: "ok")
    with make_client(router) as client:
        assert client.post("/v1/chat/completions", json=tools_payload()).status_code == 403
        assert client.post("/v1/chat/completions", json=tools_payload()).status_code == 200
        events = monitor_events(client)
    ends = [e for e in events if e["kind"] == "end"]
    assert [e["outcome"] for e in ends] == ["error", "ok"]
    requests = [e for e in events if e["kind"] == "request"]
    assert requests[1]["repeat_of_failed"] == requests[0]["rid"] and requests[1]["idle"] >= 0
    assert "messages_hash" not in requests[0]


def test_event_stream_replays_history_then_ticks():
    monitor = proxy.Monitor()
    monitor.emit("abc", "request", model="m", messages_hash="h")
    monitor.emit("abc", "send", model="m")

    async def take(n):
        lines, stream = [], monitor.stream(history=10)
        async for line in stream:
            lines.append(json.loads(line))
            if len(lines) == n:
                await stream.aclose()
                return lines

    lines = asyncio.run(take(3))
    assert [l["kind"] for l in lines] == ["request", "send", "tick"]
    assert lines[0]["replay"] and lines[2]["active"][0]["phase"] == "upstream_headers"
    assert not monitor.subscribers


# --------------------------------------------------------------------------- cut-off and empty replies

class RawStreamRouter:
    """Streams the given raw SSE bodies, one per request."""
    stream = True

    def __init__(self, *bodies):
        self.bodies, self.requests = list(bodies), []

    def __call__(self, request):
        self.requests.append(json.loads(request.content))
        return httpx.Response(200, content=self.bodies.pop(0).encode(), headers={"content-type": "text/event-stream"})


def sse_body(*deltas, finish="stop", done=True):
    lines = [{"choices": [{"index": 0, "delta": d}]} for d in deltas]
    if finish:
        lines.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    return "".join(f"data: {json.dumps(x)}\n\n" for x in lines) + ("data: [DONE]\n\n" if done else "")


def test_cut_off_stream_before_any_text_is_requested_again():
    router = RawStreamRouter(sse_body({"reasoning_content": "hmm"}, finish=None, done=False),
                             sse_body({"content": "Hello."}))
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=True))
        kinds = [e["kind"] for e in client.app.state.bridge.monitor.recent]
    assert deltas(sse_events(r), "content") == "Hello."
    assert len(router.requests) == 2 and "resend" in kinds


def test_empty_replies_end_in_a_retryable_error():
    router = RawStreamRouter(*[sse_body({"reasoning_content": "hmm"})] * 3)
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=True))
    assert len(router.requests) == 3
    error = json.loads([l for l in r.text.splitlines() if l.startswith("data: {")][-1][6:])["error"]
    assert "stream was interrupted" in error["message"] and "empty" in error["message"]


def test_stream_cut_after_text_is_an_error_not_a_normal_end():
    router = RawStreamRouter(sse_body({"content": "Half a sen"}, finish=None, done=False))
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=True))
    assert len(router.requests) == 1
    assert "stream was interrupted" in r.text and "[DONE]" not in r.text


def test_reply_cut_by_max_tokens_is_not_resent():
    router = RawStreamRouter(sse_body({"content": "A long answer that was"}, finish="length"))
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=True))
    assert len(router.requests) == 1
    assert [c["choices"][0]["finish_reason"] for c in sse_events(r) if c["choices"]][-1] == "length"


# --------------------------------------------------------------------------- lenient tool-call bodies

LS_TOOLS = [{"name": "ls", "parameters": {"type": "object", "properties": {
                "path": {"type": "string"}, "limit": {"type": "integer"}}}},
            {"name": "bash", "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                                             "required": ["command"]}}]


@pytest.mark.parametrize("body, expected", [
    ('ls\n    path: "."</arg_value>\n', ("ls", {"path": "."})),
    ("ls\n<arg_key>path</arg_key>\n<arg_value>src</arg_value>\n<arg_key>limit</arg_key><arg_value>5</arg_value>",
     ("ls", {"path": "src", "limit": 5})),
    ('bash {"command": "ls -la"}', ("bash", {"command": "ls -la"})),
    ("bash\ncommand=git status", ("bash", {"command": "git status"})),
    ("ls \npath \nsrc/main</arg_value>\nlimit\n5</arg_value>", ("ls", {"path": "src/main", "limit": 5})),
])
def test_tool_calls_in_other_styles_are_accepted(body, expected):
    nonce = "0123456789abcdef"
    calls, _ = proxy.parse_tool_calls(f'<pi_tool_call id="{nonce}">\n{body}\n</pi_tool_call>', LS_TOOLS, nonce)
    assert [(c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls] == [expected]


@pytest.mark.parametrize("body", [
    'bash commandcommand="ls -la /tmp" \n\nWait, the parameter is "command".',
    "bash\tcwd命令</arg_value>ls a/ && ls b/</arg_value>",
    "rm\npath: /",
    "ls\npath: a\nwhatever this is",
])
def test_ambiguous_bodies_are_still_rejected(body):
    nonce = "0123456789abcdef"
    with pytest.raises(proxy.ProtocolError):
        proxy.parse_tool_calls(f'<pi_tool_call id="{nonce}">\n{body}\n</pi_tool_call>', LS_TOOLS, nonce)


def test_reply_that_was_only_thinking_until_max_tokens_is_asked_again():
    router = RawStreamRouter(sse_body({"reasoning_content": "plan " * 50}, finish="length"),
                             sse_body({"content": "Writing the report now."}))
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=True))
    assert deltas(sse_events(r), "content") == "Writing the report now."
    retry = router.requests[1]["messages"]
    assert retry[-2]["role"] == "assistant" and "plan plan" in retry[-2]["content"]
    assert "was only thinking" in retry[-1]["content"]


def test_endless_thinking_is_stopped_at_the_reasoning_budget():
    thinking = [{"reasoning_content": "plan more " * 5}] * 10  # 500 chars
    router = RawStreamRouter(sse_body(*thinking, {"content": "never reached"}),
                             sse_body({"content": "Acting now."}))
    with make_client(router, reasoning_budget=120) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=True))
        events = list(client.app.state.bridge.monitor.recent)
    assert deltas(sse_events(r), "content") == "Acting now."
    assert len(router.requests) == 2 and "plan more" in router.requests[1]["messages"][-2]["content"]
    assert [e["finish"] for e in events if e["kind"] == "reply"] == ["thinking_budget", "stop"]


def test_reasoning_budget_counts_think_tags_and_spares_answers():
    long_think = "<think>" + "hmm " * 100 + "</think>"
    router = RawStreamRouter(sse_body({"content": long_think}), sse_body({"content": "Done."}))
    with make_client(router, reasoning_budget=100) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=True))
    assert deltas(sse_events(r), "content") == "Done." and len(router.requests) == 2

    router = RawStreamRouter(sse_body({"content": "Answer first. "}, *[{"reasoning_content": "x" * 50}] * 5))
    with make_client(router, reasoning_budget=100) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=True))
    assert len(router.requests) == 1 and deltas(sse_events(r), "content").strip() == "Answer first."


class BrokenStream(httpx.AsyncByteStream):
    def __init__(self, first: bytes):
        self.first = first

    async def __aiter__(self):
        yield self.first
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")


def test_connection_broken_mid_thinking_is_requested_again():
    bodies = [httpx.Response(200, stream=BrokenStream(
                  sse_body({"reasoning_content": "thinking..."}, finish=None, done=False).encode()),
                  headers={"content-type": "text/event-stream"}),
              httpx.Response(200, content=sse_body({"content": "Hello."}).encode(),
                             headers={"content-type": "text/event-stream"})]

    class Router:
        stream, requests = True, []

        def __call__(self, request):
            self.requests.append(request)
            return bodies.pop(0)

    router = Router()
    with make_client(router) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=True))
    assert deltas(sse_events(r), "content") == "Hello." and len(router.requests) == 2


def test_broken_stream_after_text_gives_pi_a_retryable_message():
    body = httpx.Response(200, stream=BrokenStream(sse_body({"content": "Half"}, finish=None, done=False).encode()),
                          headers={"content-type": "text/event-stream"})

    class Router:
        stream, requests = True, []

        def __call__(self, request):
            self.requests.append(request)
            return body

    with make_client(Router()) as client:
        r = client.post("/v1/chat/completions", json=tools_payload(stream=True))
    assert "stream was interrupted" in r.text and "[DONE]" not in r.text


def test_auto_reasoning_budget_follows_max_tokens():
    bridge = proxy.Bridge(proxy.Settings(reasoning_budget=-1), None)
    assert bridge.thinking_budget({"max_tokens": 16384}) == int(16384 * 4 * 0.7)
    assert bridge.thinking_budget({}) == 0
    assert proxy.Bridge(proxy.Settings(reasoning_budget=500), None).thinking_budget({"max_tokens": 9}) == 500


# --------------------------------------------------------------------------- per-model params, kept thinking

def loop_with_thinking():
    payload = tools_payload()
    payload["messages"] = [
        {"role": "system", "content": "You are an agent."},
        {"role": "user", "content": "first task"},
        {"role": "assistant", "content": "done", "reasoning_content": "OLD THOUGHT"},
        {"role": "user", "content": "second task"},
        {"role": "assistant", "content": None, "reasoning_content": "FIRST STEP THOUGHT", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": '{"command": "ls"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "a.txt"},
        {"role": "assistant", "content": "Reading it.", "reasoning_content": "SECOND STEP THOUGHT", "tool_calls": [
            {"id": "c2", "type": "function", "function": {"name": "bash", "arguments": '{"command": "cat a.txt"}'}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "hello"},
    ]
    return payload


def test_kept_thinking_as_field_only_for_the_current_loop():
    prep = proxy.prepare_request(loop_with_thinking(), proxy.Settings(keep_thinking="field"))
    assistants = [m for m in prep.upstream["messages"] if m["role"] == "assistant"]
    assert "reasoning_content" not in assistants[0]  # before the last user message
    assert [m.get("reasoning_content") for m in assistants[1:]] == ["FIRST STEP THOUGHT", "SECOND STEP THOUGHT"]
    assert "THOUGHT" not in all_text([m["content"] for m in prep.upstream["messages"]])
    assert prep.kept_thinking == len("FIRST STEP THOUGHT") + len("SECOND STEP THOUGHT")


def test_kept_thinking_as_text_and_off():
    prep = proxy.prepare_request(loop_with_thinking(), proxy.Settings(keep_thinking="text"))
    last = [m for m in prep.upstream["messages"] if m["role"] == "assistant"][-1]
    assert last["content"].startswith("<think>\nSECOND STEP THOUGHT\n</think>\nReading it.")
    assert "reasoning_content" not in last and prep.kept_thinking > 0
    off = proxy.prepare_request(loop_with_thinking(), proxy.Settings())
    assert "THOUGHT" not in json.dumps(off.upstream) and off.kept_thinking == 0


def test_kept_thinking_newest_first_within_the_limit():
    kept = proxy.kept_thoughts(loop_with_thinking()["messages"], 300)
    assert kept == {4: "FIRST STEP THOUGHT", 6: "SECOND STEP THOUGHT"}
    payload = loop_with_thinking()
    payload["messages"][4]["reasoning_content"] = "x" * 1000
    kept = proxy.kept_thoughts(payload["messages"], 400)
    assert kept[6] == "SECOND STEP THOUGHT" and kept[4].startswith("…") and len(kept[4]) == 400 - 19


def test_model_params_per_model_and_keep_thinking_override():
    s = proxy.Settings(model_params={"glm*": {"temperature": 1.0, "reasoning_effort": "high"},
                                     "glm-latest": {"top_p": 0.95, "keep_thinking": "text"}},
                       keep_thinking="field")
    assert proxy.model_params(s, "glm-latest") == {"temperature": 1.0, "reasoning_effort": "high",
                                                   "top_p": 0.95, "keep_thinking": "text"}
    assert proxy.model_params(s, "qwen-latest") == {}
    assert proxy.keep_thinking_mode(s, "glm-latest") == "text" and proxy.keep_thinking_mode(s, "qwen") == "field"
    with pytest.raises(ValueError, match="must not set: messages"):
        proxy.Settings(model_params={"*": {"messages": []}})
    with pytest.raises(ValueError, match="keep_thinking"):
        proxy.Settings(model_params={"*": {"keep_thinking": "maybe"}})


def test_model_params_sent_and_client_wins():
    router = FakeRouter(lambda n: "hi", lambda n: "hi", lambda n: "hi")
    params = {"glm*": {"temperature": 1.0, "reasoning_effort": "high", "chat_template_kwargs": {"x": 1},
                       "keep_thinking": "off"}}
    with make_client(router, model_params=params, extra_body={"temperature": 0.2}) as client:
        client.post("/v1/chat/completions", json=tools_payload(model="glm-latest"))
        client.post("/v1/chat/completions", json=tools_payload(model="glm-latest", reasoning_effort="low"))
        client.post("/v1/chat/completions", json=tools_payload(model="qwen-latest"))
    first, second, other = (r["json"] for r in router.requests)
    assert first["temperature"] == 1.0 and first["reasoning_effort"] == "high"
    assert first["chat_template_kwargs"] == {"x": 1} and "keep_thinking" not in first
    assert second["reasoning_effort"] == "low"           # sent by the client (Pi's thinking level)
    assert other["temperature"] == 0.2 and "reasoning_effort" not in other


def test_refused_model_param_is_dropped_and_stays_off():
    refusal = (400, {"error": {"message": "litellm.UnsupportedParamsError: openai does not support "
                                          "parameters: ['reasoning_effort'], for model=glm"}})
    router = FakeRouter(lambda n: "hi", refusal, lambda n: "hi", lambda n: "hi")
    params = {"glm*": {"temperature": 1.0, "reasoning_effort": "high"}}
    with make_client(router, model_params=params) as client:
        assert client.post("/v1/chat/completions", json=tools_payload(model="other")).status_code == 200
        assert client.post("/v1/chat/completions", json=tools_payload(model="glm-latest")).status_code == 200
        assert client.post("/v1/chat/completions", json=tools_payload(model="glm-latest")).status_code == 200
        info = client.get("/bridge/info", params={"models": "glm-latest"}).json()
    sent = [r["json"] for r in router.requests]
    assert "reasoning_effort" in sent[1] and "reasoning_effort" not in sent[2] and "reasoning_effort" not in sent[3]
    assert sent[2]["temperature"] == 1.0
    assert info["dropped_params"] == {"glm-latest": ["reasoning_effort"]}
    assert info["models"]["glm-latest"]["params"] == {"temperature": 1.0}


def test_error_not_naming_a_param_is_passed_on():
    router = FakeRouter((400, {"error": {"message": "context length exceeded"}}))
    with make_client(router, model_params={"*": {"temperature": 1.0}}) as client:
        r = client.post("/v1/chat/completions", json=tools_payload())
    assert r.status_code == 400 and len(router.requests) == 1


def test_litellm_limit_headers_parsed():
    info = proxy.parse_rate_limit({"x-ratelimit-model_per_key-limit-tokens": "8000000",
                                   "x-ratelimit-model_per_key-remaining-tokens": "2414691",
                                   "x-litellm-key-spend": "441569.7"})
    assert info.model_limit_tokens == 8000000 and info.model_remaining_tokens == 2414691
    assert info.key_spend == 441569.7 and info.remaining_tokens is None  # display only


def test_keep_thinking_settings_from_env(monkeypatch):
    monkeypatch.setenv("MODEL_PARAMS", '{"glm*": {"reasoning_effort": "high"}}')
    s = proxy.Settings.from_env()
    assert s.keep_thinking == "field" and s.keep_thinking_chars == 60000
    assert s.model_params == {"glm*": {"reasoning_effort": "high"}}
    monkeypatch.setenv("KEEP_THINKING", "nope")
    with pytest.raises(ValueError, match="KEEP_THINKING"):
        proxy.Settings.from_env()


def test_call_summary_shows_the_start_of_a_long_command():
    command = "find ~ -name x.jar 2>/dev/null | head -5; mvn -q help:evaluate -Dexpression=settings.localRepository"
    call = {"function": {"name": "bash", "arguments": json.dumps({"command": command})}}
    summary = proxy._call_summary(call, 40)
    assert summary.startswith("bash: find ~ -name x.jar") and summary.endswith("…") and len(summary) == 46


def test_arguments_split_between_name_and_arguments_are_joined():
    nonce = "0123456789abcdef"
    body = json.dumps({"name": "ls \npath \nsrc</arg_value>", "arguments": {"limit": 5}})
    calls, _ = proxy.parse_tool_calls(f'<pi_tool_call id="{nonce}">\n{body}\n</pi_tool_call>', LS_TOOLS, nonce)
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "src", "limit": 5}
