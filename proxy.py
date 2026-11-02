"""Text-to-tool bridge for an OpenAI-compatible chat router that rejects `tools`.

Pi/Continue -> this bridge (OpenAI Chat Completions) -> upstream chat endpoint.

The bridge describes the tools in the prompt, asks the model to emit
``<tool_call id="NONCE">{"name": ..., "arguments": {...}}</tool_call>`` blocks,
parses them (also while streaming) and returns ordinary OpenAI ``tool_calls``.
Tools are executed by Pi/Continue, NEVER by this bridge.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from jsonschema import SchemaError, ValidationError, validate

try:  # optional: lenient parsing of almost-JSON produced by weaker models
    import json_repair
except ImportError:  # pragma: no cover
    json_repair = None

log = logging.getLogger("pi_bridge")

OPEN_TAG = "<tool_call"
CLOSE_TAG = "</tool_call>"
THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"
RESULT_TAG = "<tool_result"

# Only these request fields are forwarded upstream. Never forward tools, tool_choice,
# parallel_tool_calls, store, stream_options or other agent metadata.
PASSTHROUGH_FIELDS = ("model", "temperature", "top_p", "max_tokens", "max_completion_tokens",
                      "stop", "seed", "presence_penalty", "frequency_penalty", "reasoning_effort")


# --------------------------------------------------------------------------- settings

def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    upstream_base_url: str = ""
    upstream_api_key: str = ""
    bridge_api_key: str = ""          # if set, clients must send `Authorization: Bearer <key>`
    injection_role: str = "user"      # "user": append protocol to the last user turn; "system": to the system prompt
    timeout: float = 600.0
    upstream_stream: bool = True      # use streaming towards the upstream router
    upstream_stream_usage: bool = True  # send stream_options.include_usage upstream
    max_retries: int = 2              # corrective re-asks after a malformed tool call
    require_nonce: bool = True        # reject <tool_call> blocks without the session id
    schema_mismatch: str = "retry"    # "retry" (re-ask the model) or "passthrough" (let the client validate)
    ping_interval: float = 15.0       # SSE keep-alive comments while waiting for the model
    debug_dump_dir: str = ""          # if set, every exchange is written there as JSON
    nonce_secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            upstream_base_url=os.environ.get("UPSTREAM_BASE_URL", "").rstrip("/"),
            upstream_api_key=os.environ.get("UPSTREAM_API_KEY", ""),
            bridge_api_key=os.environ.get("BRIDGE_API_KEY", ""),
            injection_role=os.environ.get("INJECTION_ROLE", "user"),
            timeout=float(os.environ.get("UPSTREAM_TIMEOUT", "600")),
            upstream_stream=_env_bool("UPSTREAM_STREAM", True),
            upstream_stream_usage=_env_bool("UPSTREAM_STREAM_USAGE", True),
            max_retries=int(os.environ.get("MAX_RETRIES", "2")),
            require_nonce=_env_bool("REQUIRE_NONCE", True),
            schema_mismatch=os.environ.get("SCHEMA_MISMATCH", "retry"),
            ping_interval=float(os.environ.get("PING_INTERVAL", "15")),
            debug_dump_dir=os.environ.get("DEBUG_DUMP_DIR", ""),
        )


# --------------------------------------------------------------------------- errors

class BridgeError(Exception):
    """An error returned to the client in OpenAI format."""

    def __init__(self, status: int, message: str, type_: str = "bridge_error",
                 code: str | None = None, body: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.type, self.code, self.body = status, message, type_, code, body

    def to_dict(self) -> dict:
        if self.body is not None:
            return self.body
        return {"error": {"message": self.message, "type": self.type, "code": self.code}}


class ProtocolError(Exception):
    """The model's reply violates the text tool protocol."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def _upstream_error(status: int, raw: bytes) -> BridgeError:
    text = raw.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        err = dict(data["error"])
        err["message"] = f"upstream: {err.get('message', '')}"
        return BridgeError(status, err["message"], body={"error": err})
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        text = data["error"]
    return BridgeError(status, f"upstream returned {status}: {text[:1000]}", "upstream_error")


# --------------------------------------------------------------------------- request side

def _text(content: Any, *, placeholder: bool = False) -> str:
    """Flatten OpenAI message content to text. Non-text parts are rejected, or
    (placeholder=True) replaced with an explicit marker the model can see."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif not isinstance(part, dict):
                raise ValueError("Malformed message content part")
            elif part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            elif placeholder:
                parts.append(f"[{part.get('type', 'non-text')} content omitted: this model accepts text only]")
            else:
                raise ValueError("Only text content is supported by this bridge (no images/files).")
        return "\n".join(parts)
    raise ValueError("Unsupported chat message content type")


def render_call(name: Any, arguments: Any, nonce: str) -> str:
    body = json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False)
    return f'<tool_call id="{nonce}">\n{body}\n{CLOSE_TAG}'


def _decode_arguments(arguments: Any) -> Any:
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except ValueError:
            return arguments
    return {} if arguments is None else arguments


def build_messages(messages: list, nonce: str) -> list[dict[str, str]]:
    """Rewrite an agent transcript into plain system/user/assistant text messages.

    Past tool calls are rendered in exactly the protocol format (with the current
    nonce) so the model sees consistent examples; tool results become user turns.
    Consecutive messages with the same role are merged."""
    out: list[dict[str, str]] = []
    call_names: dict[Any, Any] = {}
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("Each message must be an object")
        role = message.get("role")
        if role == "assistant":
            parts = []
            text = _text(message.get("content"), placeholder=True).strip()
            if text:
                parts.append(text)
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                call_names[call.get("id")] = function.get("name")
                parts.append(render_call(function.get("name"), _decode_arguments(function.get("arguments")), nonce))
            content = "\n".join(parts)
        elif role == "tool":
            call_id = message.get("tool_call_id") or "unknown"
            name = message.get("name") or call_names.get(call_id) or "unknown"
            result = _text(message.get("content"), placeholder=True)
            content = f'{RESULT_TAG} name="{name}" call_id="{call_id}">\n{result}\n</tool_result>'
            role = "user"
        elif role in ("system", "developer"):
            role, content = "system", _text(message.get("content"))
        elif role == "user":
            content = _text(message.get("content"))
        else:
            raise ValueError(f"Unsupported role: {role}")
        if not content.strip():
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + content
        else:
            out.append({"role": role, "content": content})
    return out


def session_nonce(messages: list, secret: bytes) -> str:
    """A nonce that is stable for one conversation (so the prompt prefix stays
    cacheable) but unpredictable without the per-process secret."""
    anchor = []
    for message in messages:
        if isinstance(message, dict) and message.get("role") in ("system", "developer", "user"):
            anchor.append(json.dumps(message.get("content"), sort_keys=True, ensure_ascii=False, default=str))
            if message.get("role") == "user":
                break
    return hmac.new(secret, "\x00".join(anchor).encode(), hashlib.sha256).hexdigest()[:16]


def _allowed_tools(payload: dict) -> list[dict]:
    choice = payload.get("tool_choice")
    if choice == "none":
        return []
    tools = []
    for tool in payload.get("tools") or []:
        if tool.get("type") == "function":
            function = tool["function"]
            if not isinstance(function.get("name"), str):
                raise ValueError("Tool function must have a name")
            tools.append(function)
    if isinstance(choice, dict):
        selected = (choice.get("function") or {}).get("name")
        if selected:
            tools = [t for t in tools if t["name"] == selected]
    return tools


def build_instruction(tools: list[dict], nonce: str, choice: Any) -> str:
    if isinstance(choice, dict):
        obligation = f"In this reply you MUST call the tool `{tools[0]['name']}`."
    elif choice == "required":
        obligation = "In this reply you MUST call at least one tool."
    else:
        obligation = "Call tools only when you need them; otherwise answer normally."
    lines = [
        "# Tool use",
        "You can use local tools. They are executed outside the model; results arrive in a later message.",
        "To call a tool, write a block in exactly this format:",
        f'<tool_call id="{nonce}">',
        '{"name": "TOOL_NAME", "arguments": {"param": "value"}}',
        CLOSE_TAG,
        "Rules:",
        f'- The id attribute must be exactly "{nonce}".',
        "- The body is one JSON object with \"name\" and \"arguments\"; arguments must match the tool's JSON Schema."
        " Escape newlines (\\n) and double quotes (\\\") inside JSON strings.",
        "- You may write a short sentence before the first block. Write nothing after the last </tool_call>.",
        "- Several independent tool calls in one reply are allowed: one block per call.",
        "- Results come back as <tool_result ...> blocks. Never write <tool_result> blocks yourself.",
        "- When no tool is needed, answer in plain text without any <tool_call> block.",
        obligation,
        "",
        "## Available tools",
    ]
    for tool in tools:
        lines.append(f"### {tool['name']}")
        if tool.get("description"):
            lines.append(str(tool["description"]).strip())
        schema = tool.get("parameters") or {"type": "object", "properties": {}}
        lines.append("Parameters (JSON Schema): " + json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines)


@dataclass
class Prepared:
    upstream: dict
    tools: list[dict]
    nonce: str
    must_call: bool
    model: str


def prepare_request(payload: dict, settings: Settings) -> Prepared:
    if not isinstance(payload.get("model"), str):
        raise ValueError("`model` is required")
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise ValueError("`messages` must be a non-empty list")
    tools = _allowed_tools(payload)
    choice = payload.get("tool_choice")
    nonce = session_nonce(raw_messages, settings.nonce_secret)
    messages = build_messages(raw_messages, nonce)
    if tools:
        instruction = build_instruction(tools, nonce, choice)
        if settings.injection_role == "system":
            # Merged into the (stable) system prompt: keeps a single system message and a cacheable prefix.
            if messages and messages[0]["role"] == "system":
                messages[0]["content"] += "\n\n" + instruction
            else:
                messages.insert(0, {"role": "system", "content": instruction})
        elif messages and messages[-1]["role"] == "user":
            # Works with chat wrappers that ignore custom system prompts.
            messages[-1]["content"] += "\n\n" + instruction
        else:
            messages.append({"role": "user", "content": instruction})
    upstream = {k: payload[k] for k in PASSTHROUGH_FIELDS if k in payload}
    upstream["messages"] = messages
    upstream["stream"] = settings.upstream_stream
    if settings.upstream_stream and settings.upstream_stream_usage:
        upstream["stream_options"] = {"include_usage": True}
    must_call = bool(tools) and (choice == "required" or isinstance(choice, dict))
    return Prepared(upstream, tools, nonce, must_call, payload["model"])


# --------------------------------------------------------------------------- reply side

def _partial_suffix(s: str, tag: str) -> int:
    """Length of the longest suffix of `s` that is a proper prefix of `tag`."""
    for k in range(min(len(tag) - 1, len(s)), 0, -1):
        if s.endswith(tag[:k]):
            return k
    return 0


class ReplyScanner:
    """Incrementally splits model output into visible text, <think> reasoning and
    the tool-call region (everything from the first <tool_call on, buffered)."""

    def __init__(self, detect_calls: bool):
        self.detect_calls = detect_calls
        self.buf = ""
        self.state = "text"  # text | think | calls
        self.calls: str | None = None
        self.started = False

    def _emit_text(self, s: str, out: list) -> None:
        if not self.started:
            s = s.lstrip()
            if not s:
                return
            self.started = True
        out.append(("text", s))

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        self.buf += chunk
        while self.buf:
            if self.state == "calls":
                self.calls += self.buf
                self.buf = ""
                break
            if self.state == "think":
                i = self.buf.find(THINK_CLOSE)
                if i >= 0:
                    if i:
                        out.append(("reasoning", self.buf[:i]))
                    self.buf = self.buf[i + len(THINK_CLOSE):]
                    self.state = "text"
                    continue
                cut = len(self.buf) - _partial_suffix(self.buf, THINK_CLOSE)
                if cut:
                    out.append(("reasoning", self.buf[:cut]))
                self.buf = self.buf[cut:]
                break
            tags = [(THINK_OPEN, "think")] + ([(OPEN_TAG, "calls")] if self.detect_calls else [])
            hits = [(self.buf.find(tag), tag, state) for tag, state in tags]
            hits = [h for h in hits if h[0] >= 0]
            if hits:
                i, tag, state = min(hits)
                if i:
                    self._emit_text(self.buf[:i], out)
                if state == "think":
                    self.buf = self.buf[i + len(tag):]
                    self.state = "think"
                    continue
                self.calls, self.buf, self.state = self.buf[i:], "", "calls"
                break
            cut = len(self.buf) - max(_partial_suffix(self.buf, tag) for tag, _ in tags)
            if cut:
                self._emit_text(self.buf[:cut], out)
            self.buf = self.buf[cut:]
            break
        return out

    def finish(self) -> tuple[list[tuple[str, str]], str | None]:
        out: list[tuple[str, str]] = []
        if self.buf and self.state == "think":
            out.append(("reasoning", self.buf))
        elif self.buf and self.state == "text":
            self._emit_text(self.buf, out)
        self.buf = ""
        return out, self.calls


_ID_RE = re.compile(r"""\bid\s*=\s*["']([^"']*)["']""")
_FENCE_RE = re.compile(r"\s*(?:```[\w-]*)?\s*")
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def _skip_ws_fence(s: str, i: int) -> int:
    return _FENCE_RE.match(s, i).end()


def _make_call(obj: Any, by_name: dict, schema_mismatch: str) -> dict:
    if not isinstance(obj, dict):
        raise ProtocolError('A tool call body must be a JSON object {"name": ..., "arguments": {...}}.')
    name = obj.get("name")
    args = obj.get("arguments", obj.get("parameters", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError as e:
            raise ProtocolError(f"`arguments` of {name!r} is a string that is not valid JSON.") from e
    if name not in by_name:
        raise ProtocolError(f"Unknown tool {name!r}. Available tools: {', '.join(by_name)}.")
    if not isinstance(args, dict):
        raise ProtocolError(f"`arguments` of {name!r} must be a JSON object.")
    try:
        validate(instance=args, schema=by_name[name].get("parameters") or {"type": "object"})
    except ValidationError as e:
        where = "/".join(str(p) for p in e.absolute_path) or "(root)"
        if schema_mismatch == "retry":
            raise ProtocolError(f"Arguments of {name!r} do not match its schema at {where}: {e.message}") from e
        log.warning("schema mismatch for %s passed through to client: %s", name, e.message)
    except SchemaError as e:
        log.warning("tool %s has an invalid JSON Schema, validation skipped: %s", name, e.message)
    return {"id": "call_" + uuid4().hex[:24], "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def parse_tool_calls(raw: str, tools: list[dict], nonce: str, *, require_nonce: bool = True,
                     truncated: bool = False, schema_mismatch: str = "retry") -> tuple[list[dict], str]:
    """Parse the tool-call region of a reply. Returns (tool_calls, stray_text)."""
    by_name = {t["name"]: t for t in tools}
    decoder = json.JSONDecoder()
    calls: list[dict] = []
    stray: list[str] = []
    pos = 0
    while True:
        start = raw.find(OPEN_TAG, pos)
        if start < 0:
            stray.append(raw[pos:])
            break
        stray.append(raw[pos:start])
        gt = raw.find(">", start)
        if gt < 0:
            if truncated:
                raise ProtocolError("The reply was cut off (max tokens) inside a tool call.", retryable=False)
            raise ProtocolError("Unterminated <tool_call> tag.")
        attrs = raw[start + len(OPEN_TAG):gt]
        if attrs and not attrs[0].isspace():
            raise ProtocolError(f"Malformed tag {raw[start:gt + 1]!r}; use <tool_call id=\"...\">.")
        match = _ID_RE.search(attrs)
        got = match.group(1) if match else None
        if got != nonce and (require_nonce or got is not None):
            raise ProtocolError(f'The tool call has id={got!r}, but it must be id="{nonce}".')
        i = _skip_ws_fence(raw, gt + 1)
        try:
            obj, end = decoder.raw_decode(raw, i)
        except json.JSONDecodeError as e:
            close = raw.find(CLOSE_TAG, i)
            if close < 0:
                if truncated:
                    raise ProtocolError("The reply was cut off (max tokens) inside a tool call.",
                                        retryable=False) from e
                raise ProtocolError(f"Tool call body is not valid JSON ({e.msg}) and </tool_call> is missing.") from e
            obj = None
            if json_repair is not None:  # only for complete blocks: never "repair" truncated output
                body = raw[i:close].rstrip().removesuffix("```")
                try:
                    obj = json_repair.loads(body)
                except Exception:  # noqa: BLE001
                    obj = None
            if not isinstance(obj, dict):
                raise ProtocolError(f"Tool call body is not valid JSON: {e.msg} (position {e.pos - i}).") from e
            log.info("repaired malformed JSON in tool call")
            end = close
        j = _skip_ws_fence(raw, end)
        pos = j + len(CLOSE_TAG) if raw.startswith(CLOSE_TAG, j) else j
        calls.append(_make_call(obj, by_name, schema_mismatch))
    text = "".join(stray)
    if RESULT_TAG in text:  # the model hallucinated tool results: drop everything from there
        log.warning("model wrote its own <tool_result>; discarding the rest of the reply")
        text = text[:text.find(RESULT_TAG)]
    text = "\n".join(line for line in text.splitlines() if not line.strip().startswith("```")).strip()
    return calls, text


def correction_message(error: str, nonce: str) -> str:
    return (f"[bridge] Your previous reply could not be processed: {error}\n"
            "Send the reply again. Tool calls must use exactly this format, with nothing after the last block:\n"
            f'<tool_call id="{nonce}">\n{{"name": "TOOL_NAME", "arguments": {{...}}}}\n{CLOSE_TAG}')


# --------------------------------------------------------------------------- upstream + engine

def _chunk_events(chunk: dict) -> list[tuple[str, Any]]:
    if "error" in chunk:
        err = chunk["error"]
        message = err.get("message") if isinstance(err, dict) else str(err)
        raise BridgeError(502, f"upstream: {message}", "upstream_error")
    events: list[tuple[str, Any]] = []
    choices = chunk.get("choices") or []
    if choices:
        delta = choices[0].get("delta") or choices[0].get("message") or {}
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            events.append(("reasoning", reasoning))
        content = delta.get("content")
        if isinstance(content, list):
            content = _text(content, placeholder=True)
        if isinstance(content, str) and content:
            events.append(("content", content))
        if choices[0].get("finish_reason"):
            events.append(("finish", choices[0]["finish_reason"]))
    if chunk.get("usage"):
        events.append(("usage", chunk["usage"]))
    return events


class Bridge:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.s = settings
        self.client = client

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.s.upstream_api_key:
            headers["Authorization"] = f"Bearer {self.s.upstream_api_key}"
        return headers

    async def open(self, payload: dict) -> httpx.Response:
        """Send the upstream request; raises BridgeError on HTTP/transport errors.
        In streaming mode only the headers have been received when this returns."""
        url = f"{self.s.upstream_base_url}/chat/completions"
        try:
            if self.s.upstream_stream:
                request = self.client.build_request("POST", url, json=payload, headers=self._headers())
                response = await self.client.send(request, stream=True)
            else:
                response = await self.client.post(url, json=payload, headers=self._headers())
        except httpx.TimeoutException as e:
            raise BridgeError(504, f"Upstream timeout: {e!r}", "upstream_timeout") from e
        except httpx.HTTPError as e:
            raise BridgeError(502, f"Upstream connection error: {e!r}", "upstream_error") from e
        if response.status_code >= 400:
            body = await response.aread()
            await response.aclose()
            raise _upstream_error(response.status_code, body)
        return response

    async def events(self, response: httpx.Response) -> AsyncIterator[tuple[str, Any]]:
        try:
            if self.s.upstream_stream:
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        log.warning("ignoring unparsable upstream SSE line: %.200s", data)
                        continue
                    for event in _chunk_events(chunk):
                        yield event
            else:
                try:
                    data = response.json()
                except ValueError as e:
                    raise BridgeError(502, "Upstream returned non-JSON body", "upstream_error") from e
                if not isinstance(data, dict) or ("error" not in data and not data.get("choices")):
                    raise BridgeError(502, "Malformed upstream response (no choices)", "upstream_error")
                for event in _chunk_events(data):
                    yield event
        except httpx.TimeoutException as e:
            raise BridgeError(504, f"Upstream timeout: {e!r}", "upstream_timeout") from e
        except httpx.HTTPError as e:
            raise BridgeError(502, f"Upstream stream error: {e!r}", "upstream_error") from e
        finally:
            await response.aclose()

    async def run(self, prep: Prepared, first: httpx.Response | None = None,
                  record: dict | None = None) -> AsyncIterator[tuple[str, Any]]:
        """Yields ("text"|"reasoning", str), ("tool_calls", list), ("usage", dict), ("finish", str)."""
        messages = list(prep.upstream["messages"])
        streamed_text = False
        usage = None
        attempts = record.setdefault("attempts", []) if record is not None else []
        try:
            for attempt in range(self.s.max_retries + 1):
                payload = {**prep.upstream, "messages": messages}
                info: dict[str, Any] = {"upstream_request": payload if attempt else "(see upstream)"}
                attempts.append(info)
                response = first if attempt == 0 and first is not None else await self.open(payload)
                scanner = ReplyScanner(detect_calls=bool(prep.tools))
                held: list[str] = []  # text of retry attempts, shown only if useful
                raw: list[str] = []
                finish = None

                def route(pieces):
                    nonlocal streamed_text
                    for kind, value in pieces:
                        if kind == "reasoning":
                            yield "reasoning", value
                        elif attempt == 0:
                            streamed_text = True
                            yield "text", value
                        else:
                            held.append(value)

                async for kind, value in self.events(response):
                    if kind == "content":
                        raw.append(value)
                        for event in route(scanner.feed(value)):
                            yield event
                    elif kind == "reasoning":
                        yield "reasoning", value
                    elif kind == "finish":
                        finish = value
                    elif kind == "usage":
                        usage = value  # last attempt only: it reflects the real context size
                tail, calls_raw = scanner.finish()
                for event in route(tail):
                    yield event
                info.update(raw="".join(raw), finish_reason=finish)
                try:
                    if calls_raw is None:
                        if prep.must_call:
                            raise ProtocolError("You must call a tool in this reply, but there is no <tool_call> block.")
                        calls, stray = [], ""
                    else:
                        calls, stray = parse_tool_calls(calls_raw, prep.tools, prep.nonce,
                                                        require_nonce=self.s.require_nonce,
                                                        truncated=finish == "length",
                                                        schema_mismatch=self.s.schema_mismatch)
                except ProtocolError as e:
                    info["error"] = str(e)
                    log.warning("attempt %d: invalid tool protocol: %s | raw reply: %.2000s",
                                attempt + 1, e, "".join(raw))
                    if e.retryable and attempt < self.s.max_retries:
                        messages = messages + [
                            {"role": "assistant", "content": _THINK_RE.sub("", "".join(raw)).strip() or "(empty)"},
                            {"role": "user", "content": correction_message(str(e), prep.nonce)},
                        ]
                        continue
                    raise BridgeError(502, f"Model reply violates the tool protocol: {e}",
                                      "bridge_protocol_error") from e
                if held and not (calls and streamed_text):
                    yield "text", "".join(held)
                if stray:
                    yield "text", ("\n" if streamed_text or held else "") + stray
                if calls:
                    yield "tool_calls", calls
                if usage:
                    yield "usage", usage
                info["result"] = {"tool_calls": calls, "finish_reason": "tool_calls" if calls else finish or "stop"}
                yield "finish", "tool_calls" if calls else (finish or "stop")
                return
        except BridgeError as e:
            if record is not None:
                record["error"] = e.to_dict()
            raise
        finally:
            if first is not None and not first.is_closed:
                await first.aclose()
            if record is not None:
                self.dump(record)

    def dump(self, record: dict) -> None:
        if not self.s.debug_dump_dir:
            return
        try:
            directory = Path(self.s.debug_dump_dir)
            directory.mkdir(parents=True, exist_ok=True)
            name = time.strftime("%Y%m%d-%H%M%S") + f"-{uuid4().hex[:8]}.json"
            (directory / name).write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str))
        except OSError as e:
            log.warning("debug dump failed: %s", e)


# --------------------------------------------------------------------------- client side

async def with_pings(events: AsyncIterator, interval: float) -> AsyncIterator:
    """Re-yield `events`, yielding None whenever nothing arrived for `interval` seconds."""
    iterator = events.__aiter__()
    pending = asyncio.ensure_future(iterator.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield None
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            yield item
            pending = asyncio.ensure_future(iterator.__anext__())
    finally:
        if not pending.done():
            pending.cancel()
            try:
                await pending
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        aclose = getattr(events, "aclose", None)
        if aclose is not None:
            await aclose()


def _chunk(meta: dict, delta: dict, finish_reason: str | None = None) -> str:
    obj = {**meta, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


async def sse(events: AsyncIterator, model: str, ping_interval: float) -> AsyncIterator[str]:
    meta = {"id": "chatcmpl-" + uuid4().hex, "object": "chat.completion.chunk",
            "created": int(time.time()), "model": model}
    usage = None
    yield _chunk(meta, {"role": "assistant", "content": ""})
    try:
        async for event in with_pings(events, ping_interval):
            if event is None:
                yield ": ping\n\n"
                continue
            kind, value = event
            if kind == "text":
                yield _chunk(meta, {"content": value})
            elif kind == "reasoning":
                yield _chunk(meta, {"reasoning_content": value})
            elif kind == "tool_calls":
                yield _chunk(meta, {"tool_calls": [{"index": i, **c} for i, c in enumerate(value)]})
            elif kind == "usage":
                usage = value
            elif kind == "finish":
                yield _chunk(meta, {}, value)
    except BridgeError as e:
        log.warning("stream aborted: %s", e.message)
        yield "data: " + json.dumps(e.to_dict(), ensure_ascii=False) + "\n\n"
        return
    if usage:
        yield "data: " + json.dumps({**meta, "choices": [], "usage": usage}, ensure_ascii=False) + "\n\n"
    yield "data: [DONE]\n\n"


async def collect(events: AsyncIterator, model: str) -> dict:
    text, reasoning, calls, usage, finish = [], [], [], None, "stop"
    async for kind, value in events:
        if kind == "text":
            text.append(value)
        elif kind == "reasoning":
            reasoning.append(value)
        elif kind == "tool_calls":
            calls = value
        elif kind == "usage":
            usage = value
        elif kind == "finish":
            finish = value
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text).strip() or (None if calls else "")}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if calls:
        message["tool_calls"] = calls
    result = {"id": "chatcmpl-" + uuid4().hex, "object": "chat.completion", "created": int(time.time()),
              "model": model, "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
    if usage:
        result["usage"] = usage
    return result


def _check_client(request: Request, settings: Settings) -> None:
    if settings.bridge_api_key:
        expected = f"Bearer {settings.bridge_api_key}"
        if not hmac.compare_digest(request.headers.get("authorization", ""), expected):
            raise BridgeError(401, "Invalid or missing bridge API key", "authentication_error")
    # Requiring JSON forces a CORS preflight, so random web pages cannot hit the local bridge.
    if "application/json" not in request.headers.get("content-type", ""):
        raise BridgeError(415, "Content-Type must be application/json", "invalid_request_error")


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        timeout = httpx.Timeout(settings.timeout, connect=30.0)
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            app.state.bridge = Bridge(settings, client)
            yield

    app = FastAPI(title="Text Tool Calls Bridge", lifespan=lifespan)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        bridge: Bridge = request.app.state.bridge
        try:
            if not settings.upstream_base_url:
                raise BridgeError(500, "Set UPSTREAM_BASE_URL", "configuration_error")
            _check_client(request, settings)
            try:
                payload = await request.json()
                if not isinstance(payload, dict):
                    raise ValueError("Request body must be a JSON object")
                prep = prepare_request(payload, settings)
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                raise BridgeError(400, f"Invalid request: {e}", "invalid_request_error") from e
            record = {"request": payload, "upstream": prep.upstream} if settings.debug_dump_dir else None
            if payload.get("stream", False):
                # Open upstream before answering so HTTP errors keep their status code.
                first = await bridge.open(prep.upstream)
                return StreamingResponse(sse(bridge.run(prep, first, record), prep.model, settings.ping_interval),
                                         media_type="text/event-stream",
                                         headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
            return JSONResponse(await collect(bridge.run(prep, None, record), prep.model))
        except BridgeError as e:
            return JSONResponse(e.to_dict(), status_code=e.status)

    return app


def _setup_logging() -> None:
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        log.addHandler(handler)
    log.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())


_setup_logging()
app = create_app()
