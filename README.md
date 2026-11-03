# Pi text-tool bridge

An OpenAI Chat Completions adapter for a router that accepts ordinary chat but
refuses requests containing native `tools`. The bridge describes the tools in the
prompt, lets the model answer with text blocks

```
<pi_tool_call id="SESSION_NONCE">
{"name": "bash", "arguments": {"command": "ls"}}
</pi_tool_call>
```

The tag is deliberately not `<tool_call>` (see `PROTOCOL_TAG`): routers serving Qwen
on vLLM/sglang often run a tool parser on the output that cuts `<tool_call>…</tool_call>`
out of the text even when the request has no `tools`. `<tool_call>` in a reply is
still accepted as an alias.

and turns them back into regular OpenAI `tool_calls`. It **never executes tools**;
Pi executes them as usual.

## Features

- Text before tool calls is allowed and shown to the user; several tool calls per reply.
- Talking *about* the protocol is safe (e.g. "describe this project"): only a real block,
  meaning the tag directly followed by a JSON object and with the session id (or no id,
  outside code/quotes), is a call. Quoted examples (`id="SESSION_NONCE"`, source code,
  fenced snippets) and `<think>` mentioned in the middle of the text stay plain text.
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
- If the router runs its own tool parser anyway (vLLM/sglang for Qwen: it strips `<tool_call>`
  from the text and returns native `tool_calls` although no `tools` were sent), those
  calls are converted back and validated the same way. An upstream
  `finish_reason: tool_calls` is never passed on without actual calls; if the router
  dropped the calls entirely, the bridge logs a warning.
- Upstream errors keep their HTTP status and OpenAI `{"error": {...}}` body.
- No client key: the bridge accepts any local client. It requires
  `Content-Type: application/json`, so web pages in your browser cannot use it.

## Setup (Linux/Fedora, macOS)

Python 3.9+ (3.10+ recommended). On macOS `/usr/bin/python3` is 3.9; it works, but a
newer one avoids old dependency releases: `brew install python@3.12` and use
`python3.12 -m venv .venv`. After pulling a requirements change, re-run
`.venv/bin/pip install -r requirements.txt`.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export PI_TEXT_URL='https://YOUR-ROUTER/v1'
export PI_TEXT_KEY='YOUR_KEY'
.venv/bin/uvicorn proxy:app --host 127.0.0.1 --port 8789
```

### Upstream API

`PI_TEXT_URL` can be a base URL or a full endpoint; the API is detected from the suffix:

| `PI_TEXT_URL` | Upstream API |
|---|---|
| `https://router/v1` | Chat Completions → `…/v1/chat/completions` |
| `https://router/v1/chat/completions` | Chat Completions |
| `https://openrouter.ai/api/v1/responses` | Responses API (stateless, `store: false`) |
| `https://router/v1/completions` | legacy prompt-string API: **not supported yet**, startup fails |

`PI_TEXT_API=chat|responses` forces the API for a base URL (it must not contradict
a full endpoint URL). Pi always talks Chat Completions to the bridge, whatever
the upstream API is.

With the Responses API the leading system message becomes `instructions`,
`max_tokens` becomes `max_output_tokens`, `reasoning_effort` becomes
`reasoning.effort`; `stop`, `seed` and the penalties are dropped. Both the OpenAI and
the OpenRouter flavours of streaming events are accepted.

### OpenRouter

The same bridge works with OpenRouter; nothing changes for the target router
(OpenRouter-only fields are forwarded only when the client or `UPSTREAM_EXTRA_BODY` sets them).

```bash
export PI_TEXT_URL='https://openrouter.ai/api/v1'            # or …/api/v1/responses
export PI_TEXT_KEY='sk-or-...'
# Pin the provider (Pi cannot send `provider` itself):
export UPSTREAM_EXTRA_BODY='{"provider": {"only": ["wafer/fp8"], "allow_fallbacks": false}}'
# Optional OpenRouter attribution headers:
export UPSTREAM_HEADERS='{"HTTP-Referer": "http://localhost", "X-Title": "pi-text-bridge"}'
.venv/bin/uvicorn proxy:app --host 127.0.0.1 --port 8788
```

- Forwarded when present in the client request: `provider`, `reasoning`, `models`,
  `route`, `transforms`, `plugins`, `usage`, `top_k`, `min_p`, `top_a`, `repetition_penalty`.
  A client field replaces the same top-level key from `UPSTREAM_EXTRA_BODY`.
- Reasoning (`delta.reasoning`) is passed on as `reasoning_content`; the usage
  object is passed on unchanged, including `completion_tokens_details.reasoning_tokens`
  and `cost`.
- OpenRouter's `: OPENROUTER PROCESSING` comments are ignored; a mid-stream
  `{"error": …}` chunk becomes an SSE error event.

Quick check without Pi (no tools, so no protocol is injected):

```bash
curl -sN http://127.0.0.1:8788/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "qwen/qwen3.8-27b", "stream": true,
  "messages": [{"role": "user", "content": "How many r'"'"'s are in the word strawberry?"}]
}'
```

The last `data:` chunk before `[DONE]` carries `usage.completion_tokens_details.reasoning_tokens`.

### Continue / ProxyAI

Use the bridge as a generic OpenAI-compatible provider: base URL
`http://127.0.0.1:8789/v1`, any API key, the model id of the router. `GET /v1/models`
is proxied to the upstream `…/models`, so model pickers work.

## Configuring Pi

Pi talks to the bridge, never to the router directly:

```
pi ──(Chat Completions, tools)──▶ bridge 127.0.0.1:8789 ──(text only)──▶ router / OpenRouter
```

Checked against Pi 1.5.0.

### 1. Start the bridge

Keep it running in its own terminal (see [Setup](#setup-linuxfedora-macos)). Pi will show
connection errors if the bridge is down.

### 2. Register the provider in `models.json`

Custom providers live only in the global file `~/.pi/agent/models.json`
(or `$PI_CODING_AGENT_DIR/models.json`); there is no per-project models file.

Two ready-made variants:

| File | Provider | Bridge port | Upstream |
|---|---|---|---|
| `models.openai.json.example` | `text-tools` | 8789 | target OpenAI-compatible router, no `provider` routing |
| `models.openrouter.json.example` | `openrouter-text` | 8788 | OpenRouter, `qwen/qwen3.8-27b` pinned to `wafer/fp8` |

The ports differ so both bridges can run at the same time:

```bash
# OpenAI-compatible router (no UPSTREAM_EXTRA_BODY)
PI_TEXT_URL='https://YOUR-ROUTER/v1' PI_TEXT_KEY='YOUR_KEY' \
  .venv/bin/uvicorn proxy:app --host 127.0.0.1 --port 8789

# OpenRouter: the provider pin lives in the bridge, Pi cannot send it
PI_TEXT_URL='https://openrouter.ai/api/v1' PI_TEXT_KEY='sk-or-...' \
UPSTREAM_EXTRA_BODY='{"provider": {"only": ["wafer/fp8"], "allow_fallbacks": false}}' \
  .venv/bin/uvicorn proxy:app --host 127.0.0.1 --port 8788
```

- No file yet: `cp models.openai.json.example ~/.pi/agent/models.json` (or the OpenRouter one).
  For both, copy one file and add the other file's provider entry to its `"providers"` object.
- File exists: **do not overwrite it**. Copy only the `"text-tools"` / `"openrouter-text"`
  entry into its `"providers"` object.

Then edit the entry:

```json
{
  "$schema": "https://pi.dev/schemas/models.schema.json",
  "providers": {
    "text-tools": {
      "baseUrl": "http://127.0.0.1:8789/v1",
      "api": "openai-completions",
      "apiKey": "unused",
      "compat": {
        "supportsDeveloperRole": false,
        "supportsStore": false,
        "supportsReasoningEffort": false,
        "supportsUsageInStreaming": true
      },
      "models": [
        {
          "id": "EXACT-MODEL-ID-OF-THE-ROUTER",
          "name": "Qwen (text tools)",
          "reasoning": false,
          "input": ["text"],
          "contextWindow": 32768,
          "maxTokens": 8192,
          "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 }
        }
      ]
    }
  }
}
```

| Field | What to put there |
|---|---|
| `baseUrl` | The **bridge** address with `/v1`, not the router URL. Change the port if you start uvicorn on another one |
| `api` | Always `openai-completions`, even when the bridge talks Responses upstream |
| `apiKey` | Any non-empty placeholder. Pi needs a value to enable the provider; the bridge ignores it. The real key goes into `PI_TEXT_KEY` |
| `compat` | Keeps Pi from sending fields the bridge drops anyway (`developer` role, `store`). Set `supportsUsageInStreaming: false` only if token counts never show up |
| `models[].id` | Exactly the model name the router expects; it is forwarded unchanged. On OpenRouter it looks like `openai/gpt-oss-120b`; check [openrouter.ai/models](https://openrouter.ai/models) |
| `models[].name` | Display name in `/model`; also matched by `--model` patterns |
| `reasoning` | `true` for thinking models (gpt-oss, Nemotron, Qwen3 thinking). Pi then shows a thinking level, and the bridge forwards reasoning to Pi |
| `compat.supportsReasoningEffort` (per model) | `true` only if the router accepts `reasoning_effort` (the gpt-oss entry in the example has it). Otherwise thinking levels have no effect |
| `contextWindow`, `maxTokens` | Real limits of the router. Pi uses `contextWindow` to decide when to compact. The tool protocol adds ~1–2k tokens per request |
| `cost` | Real prices per million tokens if you want cost tracking; `0` otherwise |

Several models: add one object per model to `models`, all behind the same bridge.
Another router at the same time: run a second bridge on another port with other
`PI_TEXT_*` values and add a second provider (e.g. `"text-tools-or"`) pointing to it.

### 3. Check that Pi sees the models

```bash
pi --list-models text-tools
pi --list-models openrouter-text
```

Nothing listed usually means invalid JSON (the `$schema` line gives editor
validation) or a missing `apiKey`.

### 4. First run: read-only, ephemeral

Pi runs tools **without confirmation**. For the first tests use a scratch
directory and only the read-only tools:

```bash
mkdir -p /tmp/pi-bridge-test && cd /tmp/pi-bridge-test && echo hello > a.txt
pi --no-session --tools read,grep,find,ls \
   --model text-tools/EXACT-MODEL-ID-OF-THE-ROUTER \
   -p "List the files here and print the content of a.txt"
```

Expect `ls` and `read` calls and then a final answer. In the bridge terminal, a
`WARNING … invalid tool protocol` line means the model broke the format and was
re-asked; see [Troubleshooting](#troubleshooting-with-pi).

### 5. Everyday use

```bash
pi --model text-tools/EXACT-MODEL-ID-OF-THE-ROUTER          # start with this model
pi --model text-tools/EXACT-MODEL-ID-OF-THE-ROUTER:low      # with a thinking level
```

Inside Pi:

- `/model`: pick `text-tools/…`. Opening `/model` re-reads `models.json`, so no restart
  is needed after edits. `Ctrl+S` on a model makes it the default for new sessions.
- `/thinking`: thinking level (only for `reasoning: true` models).
- `Ctrl+P`: cycle models; `/scoped-models` or `--models "text-tools/*"` limits the cycle.

To make it the default permanently, set it in `~/.pi/agent/settings.json` (merge
with the existing keys):

```json
{
  "defaultProvider": "text-tools",
  "defaultModel": "EXACT-MODEL-ID-OF-THE-ROUTER",
  "enabledModels": ["text-tools/*"]
}
```

### Troubleshooting with Pi

| Symptom | Cause / fix |
|---|---|
| `Connection refused` / fetch failed | Bridge not running, or `baseUrl` port is wrong |
| `Set PI_TEXT_URL` (500) | Bridge started without `PI_TEXT_URL` |
| `upstream: …` with 401/403/404 | Wrong `PI_TEXT_KEY`, wrong URL, or a model `id` the router does not know |
| `upstream returned 400` mentioning `stream_options` | `UPSTREAM_STREAM_USAGE=false` |
| `upstream …` mentioning `reasoning_effort` | `compat.supportsReasoningEffort: false` for that model |
| `bridge_protocol_error` … `id=None` | The model writes the block (often `<tool_call>`) without the id (common for Qwen/Hermes-style models): `REQUIRE_NONCE=false` |
| `bridge_protocol_error` with other errors | The model can't follow the format: raise `MAX_RETRIES`, try `INJECTION_ROLE=system`, or use a stronger model |
| `cut off (max tokens) inside a tool call` | `maxTokens` too low for big `write`/`edit` calls: raise it in `models.json` |
| Pi stops after one step / writes fake results | Inspect the exchange with `DEBUG_DUMP_DIR=/tmp/bridge-dumps` |
| Pi stops after a text like "reading files…", `stopReason: toolUse` but no `toolCall` (old bridge versions) | The router parsed `<tool_call>` itself (vLLM/sglang hermes parser for Qwen). Fixed by the default `PROTOCOL_TAG=pi_tool_call`; native `tool_calls` are converted back as well. `TRACE=-` shows `upstream parsed N tool call(s) itself` or `returned no tool calls (router stripped them?)`; `DEBUG_DUMP_DIR` dumps keep the raw upstream chunks (`attempts[].upstream_chunks`) |
| Images pasted into Pi are rejected | The models are configured as text-only (`"input": ["text"]`) |

## Running Pi safely (macOS)

[pi-setup.md](pi-setup.md) is a guide for developers new to Pi and to macOS isolation. It explains
how Pi works and which isolation tools exist, then shows how to confine Pi on macOS 26 so it sees
only the projects folder and an Obsidian vault, and reads the web (GET only) with per-request
approval. Everything (folders, ports, models with their own limits, package mirrors, upstream proxy,
VM size) is set in one `config.yaml` file and run by one `pibox` command, so it also works with
registry mirrors. The
recommended option gives the agent a full development toolchain (Java 17/21/25, Maven, Gradle,
Node.js, Python, its own Docker engine with Compose and Testcontainers), with Maven, Gradle, npm, pip
and Docker settings generated for your artifact repository. Options: a Seatbelt sandbox, a Colima VM, Colima + Docker
Compose with a GET-only egress proxy (recommended), Apple `container`, and tools-only VMs. It includes
Pi extensions for approvals, a path guard and web search/fetch, and a post-session check
for git hooks and Obsidian plugins.

## Full visibility: requests, model replies, executed commands

Three layers, each with its own tool:

| What | Where | How |
|---|---|---|
| What Pi sends, what the model answers raw (incl. `<think>`/reasoning and broken replies + retries), what the bridge returns | bridge | `TRACE=-` (live, stderr) or `TRACE=/path/trace.log` |
| Exact JSON of every exchange (full prompt with the injected protocol) | bridge | `DEBUG_DUMP_DIR=/tmp/bridge-dumps` |
| What Pi actually executed: tool, arguments, output, error flag, duration | Pi session | `python3 pi-trace.py -f` |

Typical setup, three terminals:

```bash
# 1: bridge with a live trace
TRACE=- .venv/bin/uvicorn proxy:app --host 127.0.0.1 --port 8788
# 2: Pi
pi --model openrouter-text/qwen/qwen3.8-27b
# 3: what Pi runs, live (latest session of this directory)
python3 pi-trace.py -f --cwd /path/where/pi/runs
```

Bridge trace for one turn (`<-` from Pi, `->` to Pi; the id in brackets groups one request):

```
12:01:05 [a1b2c3] <- client ran bash (call_9f…)        # the call Pi executed last turn
    {"command": "ls"}
12:01:05 [a1b2c3] <- tool result  (call_9f…)           # and what it got back
    a.txt
12:01:05 [a1b2c3]    request: model=qwen/qwen3.8-27b stream=True messages=5 tools=read,bash,edit,write …
12:01:07 [a1b2c3]    model reasoning
12:01:07 [a1b2c3]    model reply (attempt 1, finish=stop, 1.9s)
    <pi_tool_call id="…">{"name": "read", "arguments": {"path": "a.txt"}}</pi_tool_call>
12:01:07 [a1b2c3] -> client: call read (call_4e…)
12:01:07 [a1b2c3] -> client: finish=tool_calls usage={…}
```

`!! invalid tool protocol` and `-> upstream retry` lines show corrective re-asks.
Long entries are cut at `TRACE_MAX` characters (`0` = never).

`pi-trace.py` (stdlib only) prints a Pi session: prompts, thinking, every `CALL` with
arguments, `RESULT`/`FAILED` with output and duration, token usage per step. Without `-f`
it prints the whole session and exits; pass a `.jsonl` path for an older session. Pi
itself also offers `pi --export SESSION.jsonl out.html` and `pi --mode json -p "…"`
(events as JSON lines).

The trace and dumps contain your prompts, code and command output: keep them local.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PI_TEXT_URL` | — | Router base URL (`…/v1`) or full endpoint (`…/chat/completions`, `…/responses`) |
| `PI_TEXT_API` | detected | `chat` or `responses`; overrides detection for a base URL |
| `PI_TEXT_KEY` | — | Router key, sent as `Authorization: Bearer` upstream |
| `INJECTION_ROLE` | `user` | `user`: protocol appended to the last user turn (works with wrappers that ignore system prompts). `system`: merged into the system prompt |
| `UPSTREAM_STREAM` | `true` | Stream from the router. With `false` the full reply is awaited (no pings until the router responds) |
| `UPSTREAM_STREAM_USAGE` | `true` | Chat API only: send `stream_options.include_usage`; disable if the router rejects it |
| `MAX_RETRIES` | `2` | Corrective re-asks after an invalid tool call |
| `PROTOCOL_TAG` | `pi_tool_call` | Tag name of tool-call blocks. `tool_call` is the Qwen/Hermes-native tag: models follow it best, but routers with a server-side tool parser swallow it |
| `REQUIRE_NONCE` | `true` | Reject tool-call blocks without the session id. `false` accepts a missing id (Hermes-style models often drop it) but still rejects a wrong one |
| `SCHEMA_MISMATCH` | `retry` | `passthrough` hands schema-invalid arguments to Pi and lets Pi report the error to the model |
| `PING_INTERVAL` | `15` | Seconds between SSE keep-alives |
| `UPSTREAM_TIMEOUT` | `600` | Upstream read timeout, seconds |
| `UPSTREAM_EXTRA_BODY` | empty | JSON object merged into every upstream request, e.g. OpenRouter `provider`. Must not set `messages`, `tools`, `stream` and other bridge-owned fields |
| `UPSTREAM_HEADERS` | empty | JSON object of extra upstream headers (e.g. `HTTP-Referer`, `X-Title`) |
| `TRACE` | empty (off) | Readable trace of every turn: `-` = stderr, otherwise a file to append to |
| `TRACE_MAX` | `4000` | Truncate long trace entries to N characters; `0` = never |
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
