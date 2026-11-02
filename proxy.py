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
import sys
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
except Exception as _e:  # pragma: no cover  (ImportError, or a release broken on this Python)
    logging.getLogger("pi_bridge").warning("json_repair unavailable, sloppy JSON will not be repaired: %r", _e)
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
                      "stop", "seed", "presence_penalty", "frequency_penalty", "reasoning_effort",
                      # OpenRouter extensions: forwarded only when the client sends them, so a plain
                      # OpenAI-compatible router never sees them unless asked to.
                      "provider", "reasoning", "models", "route", "transforms", "plugins", "usage",
                      "top_k", "min_p", "top_a", "repetition_penalty")

# Fields UPSTREAM_EXTRA_BODY must not set: they are owned by the bridge.
RESERVED_EXTRA_FIELDS = ("messages", "input", "instructions", "tools", "tool_choice", "functions",
                         "function_call", "parallel_tool_calls", "stream", "stream_options", "store")
# Chat Completions fields that have no Responses API equivalent (or are mapped explicitly).
CHAT_ONLY_FIELDS = ("messages", "max_tokens", "max_completion_tokens", "reasoning_effort", "stop", "seed",
                    "presence_penalty", "frequency_penalty", "stream", "stream_options")


# --------------------------------------------------------------------------- settings

def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


API_SUFFIXES = {"chat": "/chat/completions", "responses": "/responses", "completions": "/completions"}


def _env_json(name: str) -> dict:
    value = os.environ.get(name, "").strip()
    if not value:
        return {}
    try:
        data = json.loads(value)
    except ValueError as e:
        raise ValueError(f"{name} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{name} must be a JSON object")
    return data


def resolve_endpoint(url: str, api: str = "") -> tuple[str, str]:
    """Return (endpoint_url, api). `url` may be a base URL (…/v1) or a full endpoint;
    the API kind is detected from the suffix unless `api` is given."""
    url = url.strip().rstrip("/")
    api = api.strip().lower()
    if api and api not in API_SUFFIXES:
        raise ValueError(f"PI_TEXT_API must be one of: chat, responses (got {api!r})")
    detected = next((name for name, suffix in API_SUFFIXES.items() if url.endswith(suffix)), None)
    if detected and api and api != detected:
        raise ValueError(f"PI_TEXT_URL ends with {API_SUFFIXES[detected]} but PI_TEXT_API={api}")
    api = api or detected or "chat"
    if api == "completions":
        raise ValueError("The legacy /completions API (prompt strings) is not supported yet; "
                         "use /chat/completions or /responses")
    return (url if detected else url + API_SUFFIXES[api]), api


@dataclass
class Settings:
    upstream_url: str = ""            # base URL (…/v1) or a full endpoint URL
    upstream_api_key: str = ""
    upstream_api: str = ""            # "chat" | "responses"; empty = detect from upstream_url
    injection_role: str = "user"      # "user": append protocol to the last user turn; "system": to the system prompt
    timeout: float = 600.0
    upstream_stream: bool = True      # use streaming towards the upstream router
    upstream_stream_usage: bool = True  # send stream_options.include_usage upstream
    max_retries: int = 2              # corrective re-asks after a malformed tool call
    require_nonce: bool = True        # reject <tool_call> blocks without the session id
    schema_mismatch: str = "retry"    # "retry" (re-ask the model) or "passthrough" (let the client validate)
    ping_interval: float = 15.0       # SSE keep-alive comments while waiting for the model
    debug_dump_dir: str = ""          # if set, every exchange is written there as JSON
    extra_body: dict = field(default_factory=dict)     # merged into every upstream request (client wins)
    extra_headers: dict = field(default_factory=dict)  # added to every upstream request
    trace: str = ""                   # "" off, "-" stderr, otherwise a file to append a readable trace to
    trace_max: int = 4000             # truncate long trace entries to this many characters (0 = never)
    nonce_secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    api: str = field(init=False, default="")       # resolved API kind
    endpoint: str = field(init=False, default="")  # resolved full endpoint URL
    models_url: str = field(init=False, default="")  # upstream …/models, for GET /v1/models

    def __post_init__(self) -> None:
        if self.upstream_url:
            self.endpoint, self.api = resolve_endpoint(self.upstream_url, self.upstream_api)
            self.models_url = self.endpoint.removesuffix(API_SUFFIXES[self.api]) + "/models"
        reserved = sorted(set(self.extra_body) & set(RESERVED_EXTRA_FIELDS))
        if reserved:
            raise ValueError(f"UPSTREAM_EXTRA_BODY must not set: {', '.join(reserved)}")
        bad = [k for k, v in self.extra_headers.items() if not isinstance(v, str)]
        if bad:
            raise ValueError(f"UPSTREAM_HEADERS values must be strings: {', '.join(bad)}")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            upstream_url=os.environ.get("PI_TEXT_URL", ""),
            upstream_api_key=os.environ.get("PI_TEXT_KEY", ""),
            upstream_api=os.environ.get("PI_TEXT_API", ""),
            injection_role=os.environ.get("INJECTION_ROLE", "user"),
            timeout=float(os.environ.get("UPSTREAM_TIMEOUT", "600")),
            upstream_stream=_env_bool("UPSTREAM_STREAM", True),
            upstream_stream_usage=_env_bool("UPSTREAM_STREAM_USAGE", True),
            max_retries=int(os.environ.get("MAX_RETRIES", "2")),
            require_nonce=_env_bool("REQUIRE_NONCE", True),
            schema_mismatch=os.environ.get("SCHEMA_MISMATCH", "retry"),
            ping_interval=float(os.environ.get("PING_INTERVAL", "15")),
            debug_dump_dir=os.environ.get("DEBUG_DUMP_DIR", ""),
            extra_body=_env_json("UPSTREAM_EXTRA_BODY"),
            extra_headers=_env_json("UPSTREAM_HEADERS"),
            trace=os.environ.get("TRACE", ""),
            trace_max=int(os.environ.get("TRACE_MAX", "4000")),
        )


# --------------------------------------------------------------------------- trace

class Tracer:
    """Human-readable log of one exchange: what the client sent since its last turn
    (new prompts, tool results = what the client actually executed), every upstream
    attempt (raw reply, reasoning, parse result) and what was returned."""

    def __init__(self, settings: "Settings"):
        self.target, self.max = settings.trace, settings.trace_max
        self.rid = uuid4().hex[:6]

    def __bool__(self) -> bool:
        return bool(self.target)

    def _cut(self, text: str) -> str:
        if self.max and len(text) > self.max:
            return text[:self.max] + f"\n… [{len(text) - self.max} more chars]"
        return text

    def __call__(self, title: str, body: Any = None) -> None:
        if not self.target:
            return
        if body is not None and not isinstance(body, str):
            body = json.dumps(body, ensure_ascii=False, indent=2, default=str)
        line = f"{time.strftime('%H:%M:%S')} [{self.rid}] {title}"
        if body:
            line += "\n" + "\n".join("    " + l for l in self._cut(body).splitlines())
        try:
            if self.target == "-":
                print(line, file=sys.stderr, flush=True)
            else:
                with open(self.target, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError as e:
            log.warning("trace write failed: %s", e)

    def incoming(self, payload: dict) -> None:
        """Messages after the client's last assistant turn: the new prompt and/or tool results."""
        if not self.target:
            return
        messages = [m for m in payload.get("messages") or [] if isinstance(m, dict)]
        last = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1)
        if last >= 0:
            for call in messages[last].get("tool_calls") or []:
                fn = call.get("function") or {}
                self(f"<- client ran {fn.get('name')} ({call.get('id')})", fn.get("arguments"))
        for m in messages[last + 1:]:
            role = m.get("role")
            if role == "tool":
                self(f"<- tool result {m.get('name') or ''} ({m.get('tool_call_id')})",
                     _text(m.get("content"), placeholder=True))
            else:
                self(f"<- {role}", _text(m.get("content"), placeholder=True))
        tools = [t["name"] for t in _allowed_tools(payload)]
        self(f"   request: model={payload.get('model')} stream={bool(payload.get('stream'))} "
             f"messages={len(messages)} tools={','.join(tools) or '-'} tool_choice={payload.get('tool_choice')}")


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
    upstream = {**settings.extra_body, **{k: payload[k] for k in PASSTHROUGH_FIELDS if k in payload}}
    upstream["messages"] = messages  # chat-shaped; converted per upstream API in Bridge.open()
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

def _raise_if_error(obj: dict) -> None:
    err = obj.get("error")
    if err:
        message = err.get("message") if isinstance(err, dict) else str(err)
        raise BridgeError(502, f"upstream: {message}", "upstream_error")


def to_chat_payload(chat: dict, settings: Settings) -> dict:
    payload = dict(chat)
    payload["stream"] = settings.upstream_stream
    if settings.upstream_stream and settings.upstream_stream_usage:
        payload["stream_options"] = {"include_usage": True}
    return payload


def to_responses_payload(chat: dict, settings: Settings) -> dict:
    """Chat-shaped payload -> stateless OpenAI/OpenRouter Responses API request."""
    messages = chat["messages"]
    payload: dict[str, Any] = {"model": chat["model"], "stream": settings.upstream_stream, "store": False}
    if messages and messages[0]["role"] == "system":
        payload["instructions"] = messages[0]["content"]
        messages = messages[1:]
    items = []
    for message in messages:
        if message["role"] == "assistant":
            items.append({"type": "message", "role": "assistant", "id": "msg_" + uuid4().hex,
                          "status": "completed",
                          "content": [{"type": "output_text", "text": message["content"], "annotations": []}]})
        else:
            items.append({"type": "message", "role": message["role"],
                          "content": [{"type": "input_text", "text": message["content"]}]})
    payload["input"] = items
    for key, value in chat.items():  # temperature, top_p, provider, reasoning, extra body, ...
        if key not in CHAT_ONLY_FIELDS and key not in payload:
            payload[key] = value
    max_tokens = chat.get("max_completion_tokens", chat.get("max_tokens"))
    if max_tokens is not None:
        payload["max_output_tokens"] = max_tokens
    if chat.get("reasoning_effort"):
        reasoning = payload.get("reasoning")
        payload["reasoning"] = {"effort": chat["reasoning_effort"],
                                **(reasoning if isinstance(reasoning, dict) else {})}
    dropped = [k for k in ("stop", "seed", "presence_penalty", "frequency_penalty") if k in chat]
    if dropped:
        log.debug("not supported by the Responses API, dropped: %s", ", ".join(dropped))
    return payload


# Event names differ between OpenAI and OpenRouter; accept both.
RESPONSES_TEXT_DELTAS = ("response.output_text.delta", "response.content_part.delta")
RESPONSES_REASONING_DELTAS = ("response.reasoning_text.delta", "response.reasoning.delta",
                              "response.reasoning_summary_text.delta")
RESPONSES_TERMINAL = ("response.completed", "response.done", "response.incomplete", "response.failed")


def _responses_usage(usage: Any) -> dict | None:
    if not isinstance(usage, dict):
        return None
    prompt, completion = usage.get("input_tokens") or 0, usage.get("output_tokens") or 0
    out: dict[str, Any] = {"prompt_tokens": prompt, "completion_tokens": completion,
                           "total_tokens": usage.get("total_tokens") or prompt + completion}
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens")
    if cached is not None:
        out["prompt_tokens_details"] = {"cached_tokens": cached}
    reasoning = (usage.get("output_tokens_details") or {}).get("reasoning_tokens")
    if reasoning is not None:
        out["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return out


def _responses_final(response: dict, *, text: bool, reasoning: bool) -> list[tuple[str, Any]]:
    """Events from a complete Response object. Text/reasoning are taken from `output`
    only when they were not streamed as deltas (or in non-streaming mode)."""
    if response.get("status") == "failed" or response.get("error"):
        err = response.get("error") or {}
        message = err.get("message") if isinstance(err, dict) else str(err)
        raise BridgeError(502, f"upstream: {message or 'response failed'}", "upstream_error")
    events: list[tuple[str, Any]] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "reasoning" and reasoning:
            for part in (item.get("content") or []) + (item.get("summary") or []):
                if isinstance(part, dict) and part.get("text"):
                    events.append(("reasoning", part["text"]))
        elif item.get("type") == "function_call":
            events.append(("native_call", (item.get("call_id") or item.get("id"), item.get("name"),
                                           item.get("arguments"))))
        elif item.get("type") == "message" and text:
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text") and part.get("text"):
                    events.append(("content", part["text"]))
    finish = "stop"
    if response.get("status") == "incomplete":
        reason = (response.get("incomplete_details") or {}).get("reason")
        finish = {"max_output_tokens": "length", "max_tokens": "length",
                  "content_filter": "content_filter"}.get(reason, "stop")
    events.append(("finish", finish))
    usage = _responses_usage(response.get("usage"))
    if usage:
        events.append(("usage", usage))
    return events


class ResponsesStream:
    """Stateful parser for Responses API SSE events."""

    def __init__(self) -> None:
        self.text_type: str | None = None       # lock onto one delta event type: no duplicates
        self.reasoning_type: str | None = None
        self.done = False                        # only the first terminal event counts

    def events(self, obj: dict) -> list[tuple[str, Any]]:
        kind = obj.get("type", "")
        if kind == "error" or (not kind.startswith("response.") and obj.get("error")):
            message = obj.get("message") or (obj.get("error") or {}).get("message") or json.dumps(obj)[:500]
            raise BridgeError(502, f"upstream: {message}", "upstream_error")
        delta = obj.get("delta")
        if kind in RESPONSES_TEXT_DELTAS and isinstance(delta, str):
            self.text_type = self.text_type or kind
            return [("content", delta)] if kind == self.text_type and delta else []
        if kind in RESPONSES_REASONING_DELTAS and isinstance(delta, str):
            self.reasoning_type = self.reasoning_type or kind
            return [("reasoning", delta)] if kind == self.reasoning_type and delta else []
        if kind in RESPONSES_TERMINAL and not self.done:
            self.done = True
            return _responses_final(obj.get("response") or {}, text=self.text_type is None,
                                    reasoning=self.reasoning_type is None)
        return []


def _chunk_events(chunk: dict) -> list[tuple[str, Any]]:
    _raise_if_error(chunk)
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
        # The router may run its own tool parser (e.g. vLLM hermes for Qwen) even though we never
        # send `tools`: it cuts <tool_call> out of the text and returns native tool_calls instead.
        for position, call in enumerate(delta.get("tool_calls") or []):
            if isinstance(call, dict):
                function = call.get("function") or {}
                events.append(("native_call", (call.get("index", position), function.get("name"),
                                               function.get("arguments"))))
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
        headers = {**self.s.extra_headers, "Content-Type": "application/json"}
        if self.s.upstream_api_key:
            headers["Authorization"] = f"Bearer {self.s.upstream_api_key}"
        return headers

    async def models(self) -> tuple[int, Any]:
        """Proxy GET …/models (used by Continue/ProxyAI to list models)."""
        headers = {k: v for k, v in self._headers().items() if k != "Content-Type"}
        try:
            response = await self.client.get(self.s.models_url, headers=headers)
        except httpx.TimeoutException as e:
            raise BridgeError(504, f"Upstream timeout: {e!r}", "upstream_timeout") from e
        except httpx.HTTPError as e:
            raise BridgeError(502, f"Upstream connection error: {e!r}", "upstream_error") from e
        if response.status_code >= 400:
            raise _upstream_error(response.status_code, response.content)
        try:
            return response.status_code, response.json()
        except ValueError as e:
            raise BridgeError(502, "Upstream /models returned non-JSON body", "upstream_error") from e

    async def open(self, payload: dict) -> httpx.Response:
        """Send the upstream request; raises BridgeError on HTTP/transport errors.
        In streaming mode only the headers have been received when this returns.
        `payload` is chat-shaped and converted here for the configured upstream API."""
        url = self.s.endpoint
        convert = to_responses_payload if self.s.api == "responses" else to_chat_payload
        payload = convert(payload, self.s)
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
        responses = self.s.api == "responses"
        stream_parser = ResponsesStream() if responses else None
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
                    if not isinstance(chunk, dict):
                        continue
                    for event in (stream_parser.events(chunk) if stream_parser else _chunk_events(chunk)):
                        yield event
            else:
                try:
                    data = response.json()
                except ValueError as e:
                    raise BridgeError(502, "Upstream returned non-JSON body", "upstream_error") from e
                if not isinstance(data, dict):
                    raise BridgeError(502, "Malformed upstream response", "upstream_error")
                _raise_if_error(data)
                if responses:
                    if "output" not in data:
                        raise BridgeError(502, "Malformed upstream response (no output)", "upstream_error")
                    events = _responses_final(data, text=True, reasoning=True)
                elif not data.get("choices"):
                    raise BridgeError(502, "Malformed upstream response (no choices)", "upstream_error")
                else:
                    events = _chunk_events(data)
                for event in events:
                    yield event
        except httpx.TimeoutException as e:
            raise BridgeError(504, f"Upstream timeout: {e!r}", "upstream_timeout") from e
        except httpx.HTTPError as e:
            raise BridgeError(502, f"Upstream stream error: {e!r}", "upstream_error") from e
        finally:
            await response.aclose()

    async def run(self, prep: Prepared, first: httpx.Response | None = None,
                  record: dict | None = None, trace: Tracer | None = None) -> AsyncIterator[tuple[str, Any]]:
        """Yields ("text"|"reasoning", str), ("tool_calls", list), ("usage", dict), ("finish", str)."""
        trace = trace or Tracer(Settings())
        messages = list(prep.upstream["messages"])
        streamed_text = False
        usage = None
        attempts = record.setdefault("attempts", []) if record is not None else []
        try:
            for attempt in range(self.s.max_retries + 1):
                payload = {**prep.upstream, "messages": messages}
                info: dict[str, Any] = {"upstream_request": payload if attempt else "(see upstream)"}
                attempts.append(info)
                if attempt:
                    trace(f"-> upstream retry {attempt}/{self.s.max_retries}", messages[-1]["content"])
                started = time.monotonic()
                response = first if attempt == 0 and first is not None else await self.open(payload)
                scanner = ReplyScanner(detect_calls=bool(prep.tools))
                held: list[str] = []  # text of retry attempts, shown only if useful
                raw: list[str] = []
                thoughts: list[str] = []
                native: dict[Any, list] = {}  # key -> [name, arguments] of upstream-parsed tool calls
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
                        thoughts.append(value)
                        yield "reasoning", value
                    elif kind == "native_call":
                        key, name, args = value
                        slot = native.setdefault(key, [None, ""])
                        slot[0] = name or slot[0]
                        if isinstance(args, str):
                            slot[1] += args
                        elif args is not None:
                            slot[1] = json.dumps(args, ensure_ascii=False)
                    elif kind == "finish":
                        finish = value
                    elif kind == "usage":
                        usage = value  # last attempt only: it reflects the real context size
                if native:
                    # Put the calls back as protocol text so they are validated like any other call.
                    log.info("upstream returned %d native tool call(s); converting", len(native))
                    trace(f"   upstream parsed {len(native)} tool call(s) itself (native tool_calls)",
                          [{"name": n, "arguments": a} for n, a in native.values()])
                    if prep.tools:
                        text = "".join("\n" + render_call(n, _decode_arguments(a or "{}"), prep.nonce)
                                       for n, a in native.values())
                        raw.append(text)
                        for event in route(scanner.feed(text)):
                            yield event
                    else:
                        log.warning("upstream returned tool calls but the client offered no tools; dropped")
                if finish == "tool_calls":
                    finish = "stop"  # only the bridge decides whether there are tool calls
                tail, calls_raw = scanner.finish()
                for event in route(tail):
                    yield event
                info.update(raw="".join(raw), finish_reason=finish)
                if trace:
                    if thoughts:
                        trace("   model reasoning", "".join(thoughts))
                    trace(f"   model reply (attempt {attempt + 1}, finish={finish}, "
                          f"{time.monotonic() - started:.1f}s)", "".join(raw) or "(empty)")
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
                    trace(f"!! invalid tool protocol: {e}")
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
                for call in calls:
                    trace(f"-> client: call {call['function']['name']} ({call['id']})", call["function"]["arguments"])
                if stray:
                    trace("-> client: extra text", stray)
                trace(f"-> client: finish={'tool_calls' if calls else finish or 'stop'} usage={json.dumps(usage)}")
                yield "finish", "tool_calls" if calls else (finish or "stop")
                return
        except BridgeError as e:
            trace(f"!! error {e.status}: {e.message}")
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

    @app.get("/v1/models")
    async def models(request: Request):
        try:
            if not settings.endpoint:
                raise BridgeError(500, "Set PI_TEXT_URL", "configuration_error")
            status, body = await request.app.state.bridge.models()
            return JSONResponse(body, status_code=status)
        except BridgeError as e:
            return JSONResponse(e.to_dict(), status_code=e.status)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        bridge: Bridge = request.app.state.bridge
        try:
            if not settings.endpoint:
                raise BridgeError(500, "Set PI_TEXT_URL", "configuration_error")
            _check_client(request, settings)
            try:
                payload = await request.json()
                if not isinstance(payload, dict):
                    raise ValueError("Request body must be a JSON object")
                prep = prepare_request(payload, settings)
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                raise BridgeError(400, f"Invalid request: {e}", "invalid_request_error") from e
            record = {"request": payload, "upstream": prep.upstream} if settings.debug_dump_dir else None
            trace = Tracer(settings)
            trace.incoming(payload)
            if payload.get("stream", False):
                # Open upstream before answering so HTTP errors keep their status code.
                try:
                    first = await bridge.open(prep.upstream)
                except BridgeError as e:
                    trace(f"!! upstream error {e.status}: {e.message}")
                    raise
                return StreamingResponse(sse(bridge.run(prep, first, record, trace), prep.model,
                                             settings.ping_interval),
                                         media_type="text/event-stream",
                                         headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
            return JSONResponse(await collect(bridge.run(prep, None, record, trace), prep.model))
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
