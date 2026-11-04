#!/usr/bin/env python3
"""Live view of what the bridge is doing with every request: `pibox diag` in a second terminal.

For each request from Pi it shows when it arrived, how long the bridge waited for rate limits,
when the router answered (headers), when the first token came, how fast the model wrote, and at
the end where the time went:

    end ok in 159.0s: rate limit 0.8s · router headers 0.3s · first token 148.2s · writing 9.7s
    → most of it (93%) the router answered but the model sent nothing yet ...

    python3 diag.py                     # follow (reconnects when the bridge restarts)
    python3 diag.py --history 0         # only new events
    python3 diag.py --json              # raw NDJSON events

Stdlib only. Reads GET /bridge/events of the bridge (default http://127.0.0.1:$BRIDGE_PORT).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

PHASES = {  # phase -> (label in the summary, what it means when it dominates)
    "bridge": ("bridge", "the bridge itself (preparing/parsing) — unexpected, look at the bridge log"),
    "rate_limit": ("rate limit", "the bridge waited for the router's rate limit (429 or request pacing)"),
    "upstream_headers": ("router headers", "the router did not answer at all yet (its queue or overload; "
                                           "with non-streaming upstream this includes the whole generation)"),
    "first_token": ("first token", "the router answered, but the model sent nothing yet: a queue at the "
                                   "provider, a long prompt being processed, or hidden thinking"),
    "generating": ("writing", "the model was writing (see chars/s; reasoning = thinking tokens)"),
}
WAITING = {"bridge": "in the bridge", "rate_limit": "waiting for the rate limit",
           "upstream_headers": "waiting for the router to answer", "first_token": "waiting for the first token",
           "generating": "model is writing"}
COLORS = {"request": "1;36", "end_ok": "1;32", "end_bad": "1;31", "warn": "33", "err": "31", "dim": "90",
          "info": "0", "verdict": "35"}


def kchars(n) -> str:
    n = n or 0
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


class View:
    def __init__(self, color: bool, tty: bool):
        self.color, self.tty = color, tty
        self.status = ""
        self.pings: dict[str, int] = {}
        self.writing: dict[str, dict] = {}
        self.last_note: dict[str, float] = {}

    def c(self, kind: str, text: str) -> str:
        return f"\033[{COLORS[kind]}m{text}\033[0m" if self.color else text

    def line(self, event: dict, kind: str, text: str) -> None:
        stamp = time.strftime("%H:%M:%S", time.localtime(event.get("t", time.time())))
        rid = event.get("rid", "")
        self.clear_status()
        prefix = self.c("dim", f"{stamp} {rid:6}") + " "
        lines = text.splitlines() or [""]
        print(prefix + self.c(kind, lines[0]), flush=False)
        for more in lines[1:]:
            print(" " * 16 + self.c(kind, more), flush=False)
        sys.stdout.flush()
        self.draw_status()

    def clear_status(self) -> None:
        if self.tty and self.status:
            sys.stdout.write("\r\033[K")

    def draw_status(self) -> None:
        if self.tty and self.status:
            try:
                width = os.get_terminal_size().columns - 1
            except OSError:
                width = 200
            sys.stdout.write(self.c("dim", self.status[:max(20, width)]))
            sys.stdout.flush()

    def tick(self, event: dict) -> None:
        active = event.get("active") or []
        parts = []
        for a in active:
            more = ""
            w = self.writing.get(a["rid"])
            if a["phase"] == "generating" and w:
                more = f", text {kchars(w.get('chars'))} + reasoning {kchars(w.get('reasoning_chars'))} chars"
            parts.append(f"⏳ {a['rid']} {WAITING.get(a['phase'], a['phase'])} {a['phase_for']:.0f}s{more} "
                         f"(total {a['elapsed']:.0f}s, {a.get('model')})")
            # Without a status line, remind every 15 s of a long wait.
            if not self.tty and a["phase_for"] >= 15:
                last = self.last_note.get(a["rid"], 0)
                if event["t"] - last >= 15:
                    self.last_note[a["rid"]] = event["t"]
                    self.line({**event, "rid": a["rid"]}, "dim",
                              f"… still {WAITING.get(a['phase'], a['phase'])}: {a['phase_for']:.0f}s")
        self.clear_status()
        self.status = "  ".join(parts) if parts else "idle: no request in flight (Pi is running a tool, " \
                                                      "waiting for you, or backing off before a retry)"
        self.draw_status()

    def show(self, e: dict) -> None:
        k = e.get("kind")
        rid = e.get("rid", "")
        if k == "tick":
            return self.tick(e)
        replay = " (earlier)" if e.get("replay") else ""
        if k == "request":
            self.pings[rid] = 0
            extra = []
            if e.get("idle") is not None:
                extra.append(f"{e['idle']:.1f}s after the previous request ended")
            if e.get("repeat_of_failed"):
                extra.append(f"same messages as failed request {e['repeat_of_failed']}: Pi is retrying "
                             f"(the gap above was Pi's own back-off)")
            text = (f"▶ request{replay} model={e.get('model')} {'stream' if e.get('stream') else 'no-stream'} "
                    f"messages={e.get('messages')} tools={e.get('tools')} ~{kchars(e.get('tokens'))} tokens")
            if e.get("last"):
                text += f'\n  last input: "{e["last"]}"'
            if extra:
                text += "\n  " + "; ".join(extra)
            self.line(e, "request", text)
        elif k == "wait":
            if e.get("seconds", 0) >= 0.5:
                self.line(e, "warn", f"  ⏸ bridge waits {e['seconds']:.1f}s before sending ({e.get('reason')})")
        elif k == "wait_in_stream":
            self.line(e, "warn", f"  ⏸ rate limited for ~{e['seconds']:.0f}s ({e.get('reason')}): the bridge "
                                 f"already answered Pi and keeps it connected with pings while it waits")
        elif k == "send":
            self.line(e, "info", f"  → sent to the router: model={e.get('model')} ~{kchars(e.get('tokens'))} tokens "
                                 f"(+{e['elapsed']:.1f}s)")
        elif k == "headers":
            status = e.get("status", 0)
            if status < 400:
                limits = e.get("limits") or {}
                lim = " ".join(f"{a}={b}" for a, b in limits.items())
                what = "headers" if e.get("streaming") else "full answer (non-streaming)"
                slow = "  ← slow router" if e.get("seconds", 0) >= 10 else ""
                self.line(e, "info", f"  ← router {status}: {what} after {e['seconds']:.2f}s{slow}"
                                     + (f"\n    limits: {lim}" if lim else ""))
            else:
                self.line(e, "err", f"  ← router {status} after {e['seconds']:.2f}s: {e.get('message', '')}")
        elif k == "rate_limited":
            self.line(e, "warn", f"  ⚠ 429 ({e.get('attempt')}/{e.get('of')}): next try in {e.get('delay')}s, "
                                 f"budget left {e.get('budget_left')}s")
        elif k == "fallback":
            self.line(e, "warn", f"  ⚠ model {e.get('model')} refused ({e.get('status')}), trying {e.get('to')}")
        elif k == "upstream_error":
            self.line(e, "err", f"  ✗ transport error after {e.get('seconds')}s: {e.get('message')}")
        elif k == "first_token":
            slow = "  ← model was silent this long" if e.get("seconds", 0) >= 10 else ""
            self.line(e, "info", f"  ← first {e.get('token')} token {e['seconds']:.2f}s after the headers{slow}")
        elif k == "progress":
            secs = max(e.get("seconds") or 0, 0.1)
            total = (e.get("chars") or 0) + (e.get("reasoning_chars") or 0)
            self.writing[rid] = e
            if self.tty:
                return  # the status line already shows it; keep the log short
            self.line(e, "dim", f"  … writing {e['seconds']:.0f}s: text {kchars(e.get('chars'))}, "
                                f"reasoning {kchars(e.get('reasoning_chars'))} chars (~{total / secs:.0f} chars/s)")
        elif k == "reply":
            usage = e.get("usage") or {}
            u = " ".join(f"{a}={usage[a]}" for a in ("prompt_tokens", "completion_tokens") if a in usage)
            details = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
            if details:
                u += f" reasoning_tokens={details}"
            secs = max(e.get("seconds") or 0, 0.1)
            total = (e.get("chars") or 0) + (e.get("reasoning_chars") or 0)
            text = (f"  ✓ model done (finish={e.get('finish')}): text {kchars(e.get('chars'))}, reasoning "
                    f"{kchars(e.get('reasoning_chars'))} chars in {e.get('seconds')}s (~{total / secs:.0f} chars/s)")
            if u:
                text += f"\n    usage: {u}"
            if e.get("empty"):
                text += "\n    ⚠ empty reply (no text, no tool call)"
            self.line(e, "warn" if e.get("empty") else "info", text)
        elif k == "retry":
            self.line(e, "warn", f"  ↻ asking the model again ({e.get('attempt')}/{e.get('of')}): {e.get('reason')}")
        elif k == "ping":
            self.pings[rid] = self.pings.get(rid, 0) + 1
        elif k == "done":
            calls = ", ".join(e.get("calls") or []) or "none"
            self.line(e, "info", f"  → to Pi: finish={e.get('finish')} tool calls: {calls}")
        elif k == "error":
            self.line(e, "err", f"  ✗ error {e.get('status')} to Pi: {e.get('message')}")
        elif k == "end":
            self.summary(e)
        else:
            self.line(e, "dim", f"  {k}: {json.dumps(e, ensure_ascii=False)}")

    def summary(self, e: dict) -> None:
        rid = e.get("rid", "")
        spent = e.get("spent") or {}
        total = e.get("elapsed") or 0
        ok = e.get("outcome") == "ok"
        parts = " · ".join(f"{PHASES.get(p, (p,))[0]} {s:.1f}s" for p, s in spent.items())
        text = f"■ end {e.get('outcome')} in {total:.1f}s" + (f": {parts}" if parts else "")
        pings = self.pings.pop(rid, 0)
        if pings:
            text += f"\n  {pings} keep-alive ping(s) sent to Pi while it waited"
        if e.get("outcome") == "client disconnected":
            text += "\n  Pi closed the connection before the end (Esc, or its own timeout/abort)"
        if spent and total >= 3:
            phase, secs = max(spent.items(), key=lambda kv: kv[1])
            share = 100 * secs / total if total else 0
            text += f"\n  → most of it ({share:.0f}%, {secs:.1f}s): {PHASES.get(phase, (phase, phase))[1]}"
        self.writing.pop(rid, None)
        self.line(e, "end_ok" if ok else "end_bad", text)


def follow(url: str, history: int, raw: bool, view: View) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via HTTP(S)_PROXY
    first = True
    while True:
        try:
            with opener.open(f"{url}/bridge/events?history={history if first else 0}", timeout=30) as response:
                if not first:
                    view.line({}, "dim", "reconnected to the bridge")
                first = False
                for line in response:
                    if not line.strip():
                        continue
                    if raw:
                        print(line.decode().rstrip(), flush=True)
                        continue
                    try:
                        view.show(json.loads(line))
                    except ValueError:
                        continue
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001  (bridge restarting, old bridge without /bridge/events, …)
            if getattr(e, "code", None) == 404:
                sys.exit("this bridge has no /bridge/events: update it (pibox init && pibox setup && pibox up)")
            view.status = ""
            view.line({}, "err", f"bridge not reachable at {url} ({e}); retrying in 3s")
            time.sleep(3)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--bridge", default=os.environ.get("BRIDGE_URL")
                   or f"http://127.0.0.1:{os.environ.get('BRIDGE_PORT', '8789')}")
    p.add_argument("--history", type=int, default=40, help="recent events to show first (default 40)")
    p.add_argument("--json", action="store_true", help="print the raw NDJSON events")
    p.add_argument("--no-color", action="store_true")
    a = p.parse_args()
    tty = sys.stdout.isatty()
    view = View(color=tty and not a.no_color and not os.environ.get("NO_COLOR"), tty=tty)
    try:
        follow(a.bridge.rstrip("/"), a.history, a.json, view)
    except KeyboardInterrupt:
        view.clear_status()
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
