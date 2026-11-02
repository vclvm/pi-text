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


def nonce_of(upstream_json):
    text = "\n".join(m["content"] for m in upstream_json["messages"])
    return re.search(r'<tool_call id="([0-9a-f]+)">', text).group(1)


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
        content = reply(nonce_of(body)) if "tool_call id=" in json.dumps(body) else reply(None)
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
    settings = proxy.Settings(upstream_base_url=BASE, upstream_api_key="secret",
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


def test_client_auth_and_content_type():
    router = FakeRouter(lambda n: "hi")
    with make_client(router, bridge_api_key="local-bridge") as client:
        assert client.post("/v1/chat/completions", json=tools_payload()).status_code == 401
        r = client.post("/v1/chat/completions", content=json.dumps(tools_payload()),
                        headers={"Authorization": "Bearer local-bridge", "Content-Type": "text/plain"})
        assert r.status_code == 415
        r = client.post("/v1/chat/completions", json=tools_payload(),
                        headers={"Authorization": "Bearer local-bridge"})
        assert r.status_code == 200
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
