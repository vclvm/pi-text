#!/usr/bin/env python3
"""Find out what an OpenAI-compatible router really offers, whatever its documentation says.

    probe.py                 endpoints + models (the default)
    probe.py endpoints       which discovery/management endpoints answer (models, key, credits, health,
                             LiteLLM /model/info, /openapi.json with the full list of routes, …)
    probe.py models          send a tiny chat request to every model: which ones really work
    probe.py limits          what the running bridge knows about the rate limits (GET /bridge/limits)
    probe.py thinking --models M   which request field changes the model's thinking (reasoning_effort,
                             thinking.type, enable_thinking …): the same question with each
    probe.py history --models M    does the model see its own earlier thinking when it is sent back
    probe.py identity --models M   which model really answers behind a name (alias): the router's
                                   /model/info, its x-litellm-model-* headers, the reply's model field
                             (KEEP_THINKING: field = reasoning_content, text = <think> in the content)

Reads PI_TEXT_URL and PI_TEXT_KEY like the bridge (or --url/--key). Requests are spaced by
--interval seconds and a 429 is waited out, so probing doesn't burn the rate limit itself.
Every model test costs a few tokens.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from urllib.parse import urlsplit

import httpx

from proxy import API_SUFFIXES, parse_rate_limit, resolve_endpoint

# Paths relative to the API base (…/v1) and to the server root. A 404 just means "not there".
BASE_PATHS = [
    "models",                       # OpenAI: the model list
    "key", "auth/key", "credits",   # OpenRouter: key limits and usage, balance
    "model/info", "model_group/info", "key/info", "user/info",  # LiteLLM
    "usage", "dashboard/billing/subscription", "dashboard/billing/usage",  # older OpenAI-style billing
    "organization/usage/completions",
]
ROOT_PATHS = [
    "openapi.json", "v1/openapi.json", "docs", "swagger.json",  # the router's real API description
    "health", "health/liveliness", "health/readiness", "v1/health", "metrics",
    "model/info", "key/info", "spend/logs", "global/spend",      # LiteLLM, outside /v1
]
CONTEXT_FIELDS = ("context_length", "context_window", "max_model_len", "max_context_length",
                  "max_input_tokens", "context_size")
OUTPUT_FIELDS = ("max_completion_tokens", "max_output_tokens", "max_tokens")


def out(*args) -> None:
    print(*args, flush=True)


def cut(text: str, n: int = 110) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


class Probe:
    def __init__(self, url: str, key: str, interval: float, timeout: float, verbose: bool):
        self.endpoint, self.api = resolve_endpoint(url)
        self.base = self.endpoint.removesuffix(API_SUFFIXES[self.api])
        parts = urlsplit(self.base)
        self.root = f"{parts.scheme}://{parts.netloc}"
        self.headers = {"Authorization": f"Bearer {key}"} if key else {}
        extra = os.environ.get("UPSTREAM_HEADERS", "").strip()
        if extra:
            self.headers.update(json.loads(extra))
        self.interval, self.verbose = interval, verbose
        self.client = httpx.Client(timeout=timeout, follow_redirects=False)
        self.last = 0.0
        self.limit_headers: dict[str, str] = {}

    def request(self, method: str, url: str, **kw) -> httpx.Response | None:
        """One request: spaced by --interval, a 429 waited out once (up to 120 s)."""
        for attempt in range(2):
            pause = self.last + self.interval - time.monotonic()
            if pause > 0:
                time.sleep(pause)
            self.last = time.monotonic()
            try:
                r = self.client.request(method, url, headers=self.headers, **kw)
            except httpx.HTTPError as e:
                out(f"  {method} {url}: {e!r}")
                return None
            info = parse_rate_limit(r.headers, r.content if r.status_code == 429 else None)
            self.limit_headers.update(info.headers)
            if r.status_code != 429 or attempt:
                return r
            wait = info.retry_after or info.reset_requests or 10
            if wait > 120:
                return r
            out(f"  429 on {url}: waiting {wait:.0f}s ({cut(r.text, 80)})")
            time.sleep(wait + 0.5)
        return r

    # ------------------------------------------------------------------ endpoints
    def endpoints(self) -> None:
        out(f"API base: {self.base}   (upstream API: {self.api})\n")
        seen_openapi = None
        for label, prefix, paths in (("relative to the API base", self.base, BASE_PATHS),
                                     ("relative to the server root", self.root, ROOT_PATHS)):
            out(f"Endpoints {label} ({prefix}):")
            for path in paths:
                url = f"{prefix}/{path}"
                r = self.request("GET", url)
                if r is None:
                    continue
                mark = "✅" if r.status_code < 300 else ("🔒" if r.status_code in (401, 403) else "  ")
                out(f"  {mark} {r.status_code}  /{path:<34} {describe(r)}")
                if r.status_code == 200 and ("openapi" in path or path == "swagger.json"):
                    data = json_or_none(r)
                    if isinstance(data, dict) and "paths" in data:
                        seen_openapi = (url, data)
                if self.verbose and r.status_code == 200:
                    out("      " + cut(r.text, 600))
            out("")
        if seen_openapi:
            url, spec = seen_openapi
            info = spec.get("info") or {}
            out(f"OpenAPI description at {url}: {info.get('title', '?')} {info.get('version', '')}")
            for path, ops in sorted(spec["paths"].items()):
                methods = ",".join(m.upper() for m in ops if m in ("get", "post", "put", "patch", "delete"))
                summary = next((op.get("summary") for op in ops.values() if isinstance(op, dict) and op.get("summary")), "")
                out(f"  {methods:<12} {path:<45} {cut(summary, 60)}")
            out("")
        self.print_limit_headers()

    # ------------------------------------------------------------------ models
    def listed_models(self) -> list[dict]:
        r = self.request("GET", f"{self.base}/models")
        data = json_or_none(r) if r is not None and r.status_code == 200 else None
        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            out(f"GET /models: {r.status_code if r is not None else 'no answer'} {describe(r) if r else ''}")
            return []
        return [m if isinstance(m, dict) else {"id": str(m)} for m in items]

    def chat_body(self, model: str, stream: bool) -> dict:
        if self.api == "responses":
            return {"model": model, "input": "Reply with the single word OK.", "max_output_tokens": 16,
                    "store": False, "stream": stream}
        return {"model": model, "messages": [{"role": "user", "content": "Reply with the single word OK."}],
                "max_tokens": 16, "stream": stream}

    def test_model(self, model: str, stream: bool) -> dict:
        started = time.monotonic()
        r = self.request("POST", self.endpoint, json=self.chat_body(model, stream))
        ms = int((time.monotonic() - started) * 1000)
        if r is None:
            return {"model": model, "ok": False, "status": None, "ms": ms, "detail": "no answer"}
        result = {"model": model, "ok": r.status_code == 200, "status": r.status_code, "ms": ms}
        if r.status_code != 200:
            result["detail"] = error_text(r)
            return result
        if stream:
            result["detail"] = cut(stream_text(r.text) or "(empty reply)", 50)
            served = self.served_as(r, model)
            if served:
                result["detail"] += f"   (answered as {served})"
                result["served_as"] = served
            return result
        data = json_or_none(r) or {}
        text = ""
        if self.api == "responses":
            for item in data.get("output") or []:
                parts = (item.get("content") or []) if isinstance(item, dict) else []
                text += "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        else:
            message = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
            text = message.get("content") or message.get("reasoning_content") or message.get("reasoning") or ""
        usage = data.get("usage") or {}
        result["detail"] = cut(text or "(empty reply)", 40) + (f"   usage {usage.get('prompt_tokens', usage.get('input_tokens'))}"
                                                             f"+{usage.get('completion_tokens', usage.get('output_tokens'))}"
                                                             if usage else "")
        if data.get("model") and data["model"] != model:
            result["detail"] += f"   (answered as {data['model']})"
            result["served_as"] = data["model"]
        return result

    def models(self, only: list[str], include: list[str], stream: bool, as_json: bool, as_yaml: bool) -> int:
        listed = self.listed_models()
        meta = {m.get("id"): m for m in listed if m.get("id")}
        names = only or list(dict.fromkeys([*meta, *include]))
        if not names:
            out("No models to test: /models returned none. Name them: probe.py models --models a,b")
            return 1
        if not as_json:
            out(f"/models lists {len(meta)} model(s); testing {len(names)} with a tiny request "
                f"({'stream' if stream else 'no stream'}, {self.interval}s apart)…\n")
        results = []
        _, deployed = self.deployments()
        for name in names:
            result = self.test_model(name, stream)
            result["deployments"] = [d["model"] for d in deployed.get(name, []) if d["model"]]
            m = meta.get(name, {})
            result["listed"] = name in meta
            result["context"] = find_field(m, CONTEXT_FIELDS)
            result["max_output"] = find_field(m, OUTPUT_FIELDS)
            results.append(result)
            if not as_json:
                mark = "✅" if result["ok"] else "❌"
                listed_mark = "" if result["listed"] else " (not in /models)"
                out(f"{mark} {name}{listed_mark}\n     {result['status']}  {result['ms']} ms  "
                    f"ctx={result['context'] or '?'}  {result.get('detail', '')}")
                if result["deployments"]:
                    out(f"     → {', '.join(dict.fromkeys(result['deployments']))}   (the router's /model/info)")
        if as_json:
            print(json.dumps({"results": results, "limit_headers": self.limit_headers}, indent=2, ensure_ascii=False))
            return 0
        working = [r for r in results if r["ok"]]
        out(f"\n{len(working)} of {len(results)} model(s) work.")
        refused = [r for r in results if not r["ok"] and r["listed"]]
        if refused:
            out("Listed in /models but refused: " + ", ".join(r["model"] for r in refused))
        self.print_limit_headers()
        if as_yaml and working:
            out("\n# for the models: section of config.yaml (check the limits: ? = unknown)")
            out("models:")
            for r in working:
                ctx = r["context"] or 32768
                out(f"  - id: {r['model']}\n    contextWindow: {ctx}\n    maxTokens: {r['max_output'] or min(8192, ctx // 4)}")
        return 0 if working else 1

    # ------------------------------------------------------------------ thinking
    THINKING_VARIANTS = (
        ("(nothing: the model's default)", {}),
        ('reasoning_effort: "high"', {"reasoning_effort": "high"}),
        ('reasoning_effort: "medium"', {"reasoning_effort": "medium"}),
        ('reasoning_effort: "low"', {"reasoning_effort": "low"}),
        ('reasoning_effort: "minimal"', {"reasoning_effort": "minimal"}),
        ('thinking: {type: "disabled"}', {"thinking": {"type": "disabled"}}),
        ("chat_template_kwargs: {enable_thinking: false}", {"chat_template_kwargs": {"enable_thinking": False}}),
        ("enable_thinking: false", {"enable_thinking": False}),
        ("chat_template_kwargs: {enable_thinking: true}", {"chat_template_kwargs": {"enable_thinking": True}}),
    )
    THINKING_PROMPT = ("A train leaves at 9:40 and arrives at 13:05. How many minutes is the trip? "
                       "Answer with the number only.")

    def thinking(self, names: list[str]) -> int:
        """Which request field the router honours to limit a model's thinking."""
        if self.api != "chat":
            out("probe thinking supports Chat Completions routers only")
            return 1
        if not names:
            out("Name the models: probe.py thinking --models glm-latest")
            return 1
        for name in names:
            out(f"{name}: the same small question with each way of limiting thinking ({self.interval}s apart)")
            base = None
            for label, extra in self.THINKING_VARIANTS:
                body = {"model": name, "max_tokens": 4096, "stream": False,
                        "messages": [{"role": "user", "content": self.THINKING_PROMPT}], **extra}
                start = time.monotonic()
                r = self.request("POST", self.endpoint, json=body)
                ms = round((time.monotonic() - start) * 1000)
                if r is None:
                    continue
                if r.status_code >= 400:
                    out(f"  ❌ {label:<48} {r.status_code} {cut(error_text(r), 70)}")
                    continue
                data = json_or_none(r) or {}
                message = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
                thought = message.get("reasoning_content") or message.get("reasoning") or ""
                usage = data.get("usage") or {}
                rt = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
                size = rt if rt is not None else len(thought) // 4
                if base is None:
                    base = size
                effect = ("" if not extra else "   ← more thinking" if size > max(base, 50) * 1.5
                          else "   ← no effect" if base and size >= base * 0.8
                          else "   ← less thinking" if size else "   ← no thinking")
                out(f"  ✅ {label:<48} thinking ~{size} tokens, completion {usage.get('completion_tokens', '?')}, "
                    f"{ms} ms, answer {cut((message.get('content') or '').strip() or '(empty)', 12)!r}{effect}")
            out("")
        out("What to do with the result (config.yaml):\n"
            "  reasoning_effort works  → the model's entry: reasoning: true and\n"
            "                            compat: { supportsReasoningEffort: true }; then Shift+Tab in Pi sets the level\n"
            "  another field works     → bridge: MODEL_PARAMS of that model, e.g. { glm*: { reasoning_effort: high } }\n"
            "Small differences are noise: the model thinks a little differently each time.")
        return 0

    # ------------------------------------------------------------------ identity
    IDENTITY_HEADERS = ("x-litellm-model-group", "x-litellm-model-id", "x-litellm-model-api-base",
                        "x-litellm-model-region", "x-litellm-version", "openai-model", "x-model")
    DEPLOYMENT_FACTS = ("base_model", "max_input_tokens", "max_output_tokens", "max_tokens", "litellm_provider",
                        "providers", "mode", "supports_reasoning", "supports_function_calling", "id")

    def deployments(self) -> tuple[str, dict[str, list[dict]]]:
        """What the router says stands behind each model name: LiteLLM's /model/info (the deployed
        model, often closed to plain keys), else /model_group/info (limits and providers, no model).
        Returns (the source URL or "", {name: [{"model", "api_base", "facts"}]})."""
        if hasattr(self, "_deployments"):
            return self._deployments
        found: tuple[str, dict[str, list[dict]]] = ("", {})
        for path, group in (("model/info", False), ("model_group/info", True)):
            for url in dict.fromkeys((f"{self.base}/{path}", f"{self.root}/{path}", f"{self.root}/v1/{path}")):
                r = self.request("GET", url)
                data = json_or_none(r) if r is not None and r.status_code == 200 else None
                items = data.get("data") if isinstance(data, dict) else None
                if not isinstance(items, list) or not items:
                    continue
                by_name: dict[str, list[dict]] = {}
                for d in items:
                    if not isinstance(d, dict):
                        continue
                    name = d.get("model_group" if group else "model_name")
                    params, info = ({}, d) if group else (d.get("litellm_params") or {}, d.get("model_info") or {})
                    by_name.setdefault(name, []).append({
                        "model": params.get("model"), "api_base": params.get("api_base"),
                        "facts": {k: info[k] for k in self.DEPLOYMENT_FACTS if info.get(k) not in (None, "", [])}})
                found = (url, by_name)
                break
            if found[0]:
                break
        self._deployments = found
        return found

    @staticmethod
    def served_as(r: httpx.Response, model: str) -> str:
        """The model a reply says it came from, if that differs from the name asked for."""
        data = json_or_none(r) if "json" in r.headers.get("content-type", "") else None
        named = data.get("model") if isinstance(data, dict) else None
        if not named and "data:" in r.text[:200]:
            for line in r.text.splitlines():
                chunk = json_or_none_text(line[5:].strip()) if line.startswith("data:") else None
                if isinstance(chunk, dict) and chunk.get("model"):
                    named = chunk["model"]
                    break
        return named if named and named != model else ""

    def identity(self, names: list[str]) -> int:
        """What a model name (often an alias like glm-latest) really is. Only what the router states
        counts: asked about itself, a model names what its training data says, not what is deployed."""
        if not names:
            out("Name the models: probe.py identity --models glm-latest")
            return 1
        source, deployed = self.deployments()
        if not source:
            out("The router doesn't open /model/info or /model_group/info to this key: only the replies\n"
                "below tell something (and only if the router puts the real name into them).\n")
        elif source.endswith("model_group/info"):
            out(f"{source}: limits and providers per name; the deployed model itself is in /model/info,\n"
                "which this key may not read.\n")
        else:
            out(f"{source}: the deployments behind each name\n")
        listed = {m.get("id"): m for m in self.listed_models()}
        for name in names:
            out(f"{name}:")
            for d in deployed.get(name, []):
                if d["model"]:
                    out(f"  deployment → {d['model']}" + (f"   at {d['api_base']}" if d["api_base"] else ""))
                if d["facts"]:
                    out("    " + ", ".join(f"{k}={v}" for k, v in d["facts"].items()))
            if source and name not in deployed:
                out("  not listed there: a wildcard route, or a name the router maps elsewhere")
            meta = listed.get(name) or {}
            extra = {k: meta.get(k) for k in ("owned_by", "root", "parent", "max_model_len", "context_length")
                     if meta.get(k) not in (None, "")}
            if extra:
                out("  /models: " + ", ".join(f"{k}={v}" for k, v in extra.items()))
            r = self.request("POST", self.endpoint, json=self.chat_body(name, False))
            if r is None:
                continue
            if r.status_code >= 400:
                out(f"  ❌ request: {r.status_code} {cut(error_text(r), 80)}")
                continue
            data = json_or_none(r) or {}
            out(f"  reply's model field: {data.get('model') or '(none)'}"
                + (f", system_fingerprint={data['system_fingerprint']}" if data.get("system_fingerprint") else ""))
            for h in self.IDENTITY_HEADERS:
                if r.headers.get(h):
                    out(f"  {h}: {r.headers[h]}")
            out("")
        out("Trust the deployment line, the reply's model field and the headers, in that order; a name\n"
            "like 'latest' can point elsewhere tomorrow. Asking the model which it is proves nothing.")
        return 0

    # ------------------------------------------------------------------ history
    SECRET = "PELICAN-7342"
    HISTORY_VARIANTS = (
        ("off: thinking not sent back (control)", "off", None),
        ("field: reasoning_content of the assistant message", "field", None),
        ("field + chat_template_kwargs.clear_thinking=false", "field",
         {"chat_template_kwargs": {"clear_thinking": False}}),
        ("field + thinking.clear_thinking=false (Z.ai API)", "field",
         {"thinking": {"type": "enabled", "clear_thinking": False}}),
        ("text: <think>…</think> before the assistant's text", "text", None),
    )

    def history_body(self, model: str, mode: str, extra: dict | None = None) -> dict:
        thought = f"The user wants a short reply. I also note the secret code word {self.SECRET} for later."
        assistant = {"role": "assistant", "content": "OK."}
        if mode == "field":
            assistant["reasoning_content"] = thought
        elif mode == "text":
            assistant["content"] = f"<think>\n{thought}\n</think>\nOK."
        return {**(extra or {}), "model": model, "max_tokens": 2048, "stream": False, "messages": [
            {"role": "user", "content": "Reply with OK."}, assistant,
            {"role": "user", "content": "Which secret code word did you note in your previous thinking? "
                                        "Answer with the code word only, or NONE if there was none."}]}

    def history(self, names: list[str]) -> int:
        """Does the router pass the model's earlier thinking back to it (for KEEP_THINKING)?"""
        if self.api != "chat":
            out("probe history supports Chat Completions routers only")
            return 1
        if not names:
            out("Name the models: probe.py history --models glm-latest,qwen-latest")
            return 1
        for name in names:
            out(f"{name}: an earlier reply whose thinking holds a code word; does the model know it?")
            for label, mode, extra in self.HISTORY_VARIANTS:
                r = self.request("POST", self.endpoint, json=self.history_body(name, mode, extra))
                if r is None:
                    continue
                if r.status_code >= 400:
                    out(f"  ❌ {label:<52} {r.status_code} {cut(error_text(r), 70)}")
                    continue
                message = ((json_or_none(r) or {}).get("choices") or [{}])[0].get("message") or {}
                answer = (message.get("content") or "").strip()
                seen = self.SECRET.lower() in answer.lower()
                verdict = ("sees it" if seen else "doesn't see it") if mode != "off" else \
                    ("knows it anyway?! (the test is unreliable)" if seen else "doesn't know it (as expected)")
                out(f"  {'✅' if seen == (mode != 'off') else '·'} {label:<52} {verdict}: {cut(answer or '(empty)', 30)!r}")
            out("")
        out("What to do with the result (config.yaml, bridge:):\n"
            "  field sees it            → KEEP_THINKING: field (the default)\n"
            "  only field + clear_thinking sees it → that field into MODEL_PARAMS of the model, and\n"
            "                             keep_thinking: field, e.g. { glm*: { chat_template_kwargs: { clear_thinking: false },\n"
            "                             keep_thinking: field } } (the model's \"preserved thinking\")\n"
            "  only text sees it        → MODEL_PARAMS: { <model>: { keep_thinking: text } }\n"
            "  neither                  → keep_thinking: off for that model (it would only cost tokens)")
        return 0

    def print_limit_headers(self) -> None:
        if self.limit_headers:
            out("Rate-limit headers the router sends:")
            for k, v in sorted(self.limit_headers.items()):
                out(f"  {k}: {v}")
        else:
            out("The router sent no rate-limit headers (x-ratelimit-*, retry-after) so far.")


def json_or_none_text(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


def json_or_none(r: httpx.Response | None):
    if r is None:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def error_text(r: httpx.Response) -> str:
    data = json_or_none(r)
    if isinstance(data, dict):
        err = data.get("error", data.get("detail", data.get("message")))
        if isinstance(err, dict):
            err = err.get("message") or json.dumps(err, ensure_ascii=False)
        if err:
            return cut(err)
    return cut(r.text or r.reason_phrase)


def describe(r: httpx.Response | None) -> str:
    if r is None:
        return ""
    ctype = r.headers.get("content-type", "").split(";")[0]
    if r.status_code >= 400:
        return error_text(r)
    data = json_or_none(r)
    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            ids = [str(x.get("id", x.get("model_name", ""))) if isinstance(x, dict) else str(x) for x in data["data"]]
            return cut(f"{len(ids)} item(s): {', '.join(ids)}")
        if isinstance(data.get("data"), dict):
            data = data["data"]
        return cut("{" + ", ".join(f"{k}: {json.dumps(v, ensure_ascii=False)[:30]}" for k, v in list(data.items())[:8]) + "}")
    if isinstance(data, list):
        return f"list of {len(data)}"
    return cut(f"{ctype or 'no content-type'}, {len(r.content)} bytes")


def find_field(model: dict, names: tuple[str, ...]):
    for source in (model, model.get("top_provider") or {}, model.get("model_info") or {},
                   model.get("meta") or {}, model.get("capabilities") or {}):
        if isinstance(source, dict):
            for name in names:
                if isinstance(source.get(name), (int, float)) and source[name] > 0:
                    return int(source[name])
    return None


def stream_text(sse: str) -> str:
    text = []
    for line in sse.splitlines():
        if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
            try:
                chunk = json.loads(line[5:])
            except ValueError:
                continue
            for choice in chunk.get("choices") or []:
                text.append((choice.get("delta") or {}).get("content") or "")
            if isinstance(chunk.get("delta"), str):
                text.append(chunk["delta"])
    return "".join(text)


def limits(bridge: str) -> int:
    try:
        r = httpx.get(bridge.rstrip("/") + "/bridge/limits", timeout=10)
    except httpx.HTTPError as e:
        out(f"bridge at {bridge}: {e!r}")
        return 1
    print(json.dumps(r.json(), indent=2, ensure_ascii=False))
    try:
        info = httpx.get(bridge.rstrip("/") + "/bridge/info", timeout=10)
        if info.status_code == 200:
            print("\nWhat the bridge sends with every request (GET /bridge/info):")
            print(json.dumps(info.json(), indent=2, ensure_ascii=False))
    except httpx.HTTPError:
        pass
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("what", nargs="?", default="all", choices=["all", "endpoints", "models", "limits", "thinking", "history", "identity"])
    p.add_argument("--url", default=os.environ.get("PI_TEXT_URL", ""), help="router URL (default: $PI_TEXT_URL)")
    p.add_argument("--key", default=os.environ.get("PI_TEXT_KEY", ""), help="router key (default: $PI_TEXT_KEY)")
    p.add_argument("--models", default="", help="comma-separated: test only these models")
    p.add_argument("--include", default="", help="comma-separated: test these too (e.g. ones missing from /models)")
    p.add_argument("--stream", action="store_true", help="test models with streaming, as the bridge uses them")
    p.add_argument("--interval", type=float, default=float(os.environ.get("MIN_REQUEST_INTERVAL", "1.5") or 1.5),
                   help="seconds between requests (default: $MIN_REQUEST_INTERVAL or 1.5)")
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--json", action="store_true", help="models: machine-readable output")
    p.add_argument("--yaml", action="store_true", help="models: print config.yaml entries for working models")
    p.add_argument("-v", "--verbose", action="store_true", help="endpoints: show the start of each answer")
    p.add_argument("--bridge", default=f"http://127.0.0.1:{os.environ.get('BRIDGE_PORT', '8789')}",
                   help="limits: the bridge's address")
    a = p.parse_args()
    if a.what == "limits":
        return limits(a.bridge)
    if not a.url:
        p.error("set PI_TEXT_URL or pass --url")
    probe = Probe(a.url, a.key, max(0.0, a.interval), a.timeout, a.verbose)
    split = lambda s: [x.strip() for x in s.split(",") if x.strip()]
    status = 0
    if a.what == "thinking":
        return probe.thinking(split(a.models))
    if a.what == "history":
        return probe.history(split(a.models))
    if a.what == "identity":
        return probe.identity(split(a.models))
    if a.what in ("all", "endpoints") and not a.json:
        probe.endpoints()
    if a.what in ("all", "models"):
        status = probe.models(split(a.models), split(a.include), a.stream, a.json, a.yaml)
    return status


if __name__ == "__main__":
    sys.exit(main())
