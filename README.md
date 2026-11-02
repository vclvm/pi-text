# Pi text-tool bridge

An OpenAI Chat Completions adapter for a router that accepts ordinary chat but
refuses requests containing native `tools`. The bridge describes the tools in the
prompt, lets the model answer with text blocks

```
<tool_call id="SESSION_NONCE">
{"name": "bash", "arguments": {"command": "ls"}}
</tool_call>
```

and turns them back into regular OpenAI `tool_calls`. It **never executes tools**;
Pi executes them as usual.

## Features

- Text before tool calls is allowed and shown to the user; several tool calls per reply.
- True streaming: text is forwarded token by token, the tool-call region is
  buffered and parsed at the end. SSE `: ping` keep-alives while buffering.
- Reasoning: upstream `reasoning_content`/`reasoning` and inline `<think>…</think>`
  are forwarded to Pi as `reasoning_content`, never parsed as tool calls.
- Past tool calls are replayed in the same protocol format, tool results as
  `<tool_result name=… call_id=…>` user turns; consecutive same-role messages are merged.
- Per-conversation nonce (HMAC of the conversation start with a per-process
  secret): stable across turns, so the prompt prefix stays cacheable.
- Validation of tool name, JSON and JSON Schema. On failure the model is re-asked
  with the error (`MAX_RETRIES`). Complete-but-sloppy JSON (trailing commas, raw
  newlines) is repaired with `json-repair`; truncated output (`finish_reason=length`)
  is never repaired or retried.
- Upstream errors keep their HTTP status and OpenAI `{"error": {...}}` body.
- Optional client key and a mandatory `Content-Type: application/json`, so web
  pages in your browser cannot use the local bridge.

## Setup (Linux/Fedora)

```bash
cd pi-text-tools-proxy
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export UPSTREAM_BASE_URL='https://YOUR-ROUTER/v1'
export UPSTREAM_API_KEY='YOUR_KEY'
export BRIDGE_API_KEY='local-bridge'      # must match apiKey in models.json
.venv/bin/uvicorn proxy:app --host 127.0.0.1 --port 8787
```

Use the **OpenAI Chat Completions** endpoint, not Responses. If router URLs include
`/chat/completions`, remove this suffix from `UPSTREAM_BASE_URL`.

Merge `models.json.example` into `~/.pi/agent/models.json` (**do not overwrite an
existing file**), replace the model IDs with the names accepted by the router and
set real context windows and costs. `reasoning: true` is set for GPT-OSS and
Nemotron; adjust to your models.

```
pi --list-models
pi        # inside Pi: /model -> text-tools/YOUR_MODEL_ID
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `UPSTREAM_BASE_URL` | — | Router base URL ending in `/v1` |
| `UPSTREAM_API_KEY` | — | Sent as `Authorization: Bearer` upstream |
| `BRIDGE_API_KEY` | empty (off) | Required client key |
| `INJECTION_ROLE` | `user` | `user`: protocol appended to the last user turn (works with wrappers that ignore system prompts). `system`: merged into the system prompt |
| `UPSTREAM_STREAM` | `true` | Stream from the router. With `false` the full reply is awaited (no pings until the router responds) |
| `UPSTREAM_STREAM_USAGE` | `true` | Send `stream_options.include_usage`; disable if the router rejects it |
| `MAX_RETRIES` | `2` | Corrective re-asks after an invalid tool call |
| `REQUIRE_NONCE` | `true` | Reject `<tool_call>` blocks without the session id. `false` accepts a missing id (Hermes-style models often drop it) but still rejects a wrong one |
| `SCHEMA_MISMATCH` | `retry` | `passthrough` hands schema-invalid arguments to Pi and lets Pi report the error to the model |
| `PING_INTERVAL` | `15` | Seconds between SSE keep-alives |
| `UPSTREAM_TIMEOUT` | `600` | Upstream read timeout, seconds |
| `DEBUG_DUMP_DIR` | empty (off) | Write every exchange (requests, raw replies, errors) as JSON. Contains your code and prompts |
| `LOG_LEVEL` | `INFO` | Invalid model replies are logged at `WARNING` with the raw text |

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

## Notes and limitations

- A proof-of-concept, not a replacement for native tool calling. Try it first in a
  disposable directory. Pi runs tools without confirmation by default.
- Usage reported to Pi is from the last upstream attempt (it reflects the real
  context size); tokens spent on retries are not included.
- Non-text content in user messages is rejected; in tool results it is replaced
  by an explicit `[... content omitted]` marker.
- After an error mid-stream (e.g. retries exhausted) the bridge sends an SSE
  `{"error": …}` event; before the stream starts it returns a normal HTTP error.
- The nonce changes when the bridge restarts (one cache miss) and protects only
  against the model accidentally quoting tool-call examples, not against prompt
  injection in tool output.
- The bridge binds to localhost. Do not expose it publicly.
- Do not use text emulation to violate an upstream provider's access policy or
  terms; check the contract if agent use is explicitly forbidden.
