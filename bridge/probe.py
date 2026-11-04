#!/usr/bin/env python3
"""Find out what an OpenAI-compatible router really offers, whatever its documentation says.

    probe.py                 endpoints + models (the default)
    probe.py endpoints       which discovery/management endpoints answer (models, key, credits, health,
                             LiteLLM /model/info, /openapi.json with the full list of routes, …)
    probe.py models          send a tiny chat request to every model: which ones really work
    probe.py limits          what the running bridge knows about the rate limits (GET /bridge/limits)

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
        for name in names:
            result = self.test_model(name, stream)
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

    def print_limit_headers(self) -> None:
        if self.limit_headers:
            out("Rate-limit headers the router sends:")
            for k, v in sorted(self.limit_headers.items()):
                out(f"  {k}: {v}")
        else:
            out("The router sent no rate-limit headers (x-ratelimit-*, retry-after) so far.")


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
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("what", nargs="?", default="all", choices=["all", "endpoints", "models", "limits"])
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
    if a.what in ("all", "endpoints") and not a.json:
        probe.endpoints()
    if a.what in ("all", "models"):
        status = probe.models(split(a.models), split(a.include), a.stream, a.json, a.yaml)
    return status


if __name__ == "__main__":
    sys.exit(main())
