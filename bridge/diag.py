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
import queue
import re
import sys
import threading
import time
import unicodedata
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
# Only normal colours on the default background: "bright black" (90) and faint (2) are the
# background colour in many dark themes (Solarized Dark, …), so secondary text is left uncoloured.
COLORS = {"request": "1;36", "end_ok": "1;32", "end_bad": "1;31", "warn": "33", "err": "31", "dim": "",
          "info": "", "verdict": "35"}


TAIL_EVERY = 20  # seconds between "what the model is writing now" lines in the log


def cells(text: str) -> int:
    """Terminal columns `text` takes: wide (CJK, most emoji) characters take two."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 0 if unicodedata.combining(ch) else 1
               for ch in text)


def fit(text: str, width: int) -> str:
    """Cut `text` to at most `width` terminal columns (a wrapped status line can't be redrawn)."""
    if cells(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        w = cells(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def wrap(text: str, width: int) -> list[str]:
    """Split `text` into pieces of at most `width` terminal columns, at spaces where possible."""
    width = max(20, width - 4)
    pieces = []
    while cells(text) > width:
        cut, used = 0, 0
        for i, ch in enumerate(text):
            used += cells(ch)
            if used > width:
                break
            cut = i + 1
        space = text.rfind(" ", 0, cut)
        if space > cut // 2:
            cut = space
        pieces.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return pieces + [text]


def ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds}s" if seconds < 120 else f"{seconds // 60}m{seconds % 60:02d}s"


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
        self.max_tokens: dict[str, int] = {}
        self.last_tail: dict[str, float] = {}
        self.sent_tokens: dict[str, int] = {}
        self.active: dict[str, dict] = {}    # rid -> model, start, phase, since (epoch seconds)
        self.outcome: dict[str, dict] = {}   # rid -> what the request ended with (done/error)
        self.last: dict | None = None        # the last finished request

    def c(self, kind: str, text: str) -> str:
        return f"\033[{COLORS[kind]}m{text}\033[0m" if self.color and COLORS[kind] else text

    def line(self, event: dict, kind: str, text: str) -> None:
        stamp = time.strftime("%H:%M:%S", time.localtime(event.get("t", time.time())))
        rid = event.get("rid", "")
        self.clear_status()
        prefix = self.c("dim", f"{stamp} {rid:6}") + " "
        lines = text.splitlines() or [""]
        width = self.width()
        for n, line in enumerate(lines):
            # Wrap long lines ourselves: a line the terminal wraps confuses the status line redraw.
            pieces = wrap(line, width - 16) if width else [line]
            for m, piece in enumerate(pieces):
                lead = prefix if n == 0 and m == 0 else " " * (16 if m == 0 else 20)
                print(lead + self.c(kind, piece), flush=False)
        sys.stdout.flush()
        self.draw_status()

    def width(self) -> int:
        if not self.tty:
            return 0
        try:
            return max(40, os.get_terminal_size().columns)
        except OSError:
            return 0

    def clear_status(self) -> None:
        if self.tty and self.status:
            sys.stdout.write("\r\033[K")

    def draw_status(self) -> None:
        if self.tty and self.status:
            width = (self.width() or 202) - 2  # one column spare: never let the terminal wrap it
            sys.stdout.write(self.c("dim", fit(self.status, max(20, width))))
            sys.stdout.flush()

    def track(self, e: dict) -> None:
        """Keep the state of every request from its events, so the status line runs on its own clock."""
        rid, k, t = e.get("rid"), e.get("kind"), e.get("t", time.time())
        if not rid:
            return
        if k == "request":
            self.active[rid] = {"model": e.get("model"), "start": t, "phase": e.get("phase", "bridge"), "since": t}
        elif k == "end":
            self.active.pop(rid, None)
            self.last = {"t": t, "rid": rid, "outcome": e.get("outcome"), **self.outcome.pop(rid, {})}
        elif rid in self.active and e.get("phase") and e["phase"] != self.active[rid]["phase"]:
            self.active[rid].update(phase=e["phase"], since=t)
        if k == "done":
            self.outcome[rid] = {"finish": e.get("finish"), "calls": e.get("call_args") or e.get("calls") or []}
        elif k == "error":
            self.outcome[rid] = {"error": e.get("message", "")}

    def tick(self, event: dict) -> None:
        """The bridge's own view, once a second: corrects ours (e.g. when diag started mid-request)."""
        t = event.get("t", time.time())
        seen = set()
        for a in event.get("active") or []:
            seen.add(a["rid"])
            mine = self.active.setdefault(a["rid"], {"model": a.get("model"), "start": t - a["elapsed"]})
            if mine.get("phase") != a["phase"]:
                mine.update(phase=a["phase"], since=t - a["phase_for"])
        for rid in [r for r in self.active if r not in seen and t - self.active[r]["start"] > 3]:
            del self.active[rid]  # it ended while we were not connected

    def render(self) -> None:
        now = time.time()
        parts = []
        for rid, a in self.active.items():
            phase, phase_for = a.get("phase", "bridge"), now - a.get("since", now)
            more = ""
            w = self.writing.get(rid)
            if phase == "generating" and w:
                more = f", text {kchars(w.get('chars'))} + reasoning {kchars(w.get('reasoning_chars'))} chars"
                if w.get("tail"):
                    more += f" | {'thinks' if w.get('now_writing') == 'reasoning' else 'writes'}: …{w['tail'][-90:]}"
            parts.append(f"⏳ {rid} {WAITING.get(phase, phase)} {ago(phase_for)} "
                         f"(total {ago(now - a['start'])}, {a.get('model')}){more}")
            # Without a status line, remind every 15 s of a long wait.
            if not self.tty and phase_for >= 15 and now - self.last_note.get(rid, 0) >= 15:
                self.last_note[rid] = now
                self.line({"t": now, "rid": rid}, "dim", f"… still {WAITING.get(phase, phase)}: {ago(phase_for)}")
        status = "  ".join(parts) if parts else self.idle(now)
        if status != self.status or self.tty:
            self.clear_status()
            self.status = status
            self.draw_status()

    def idle(self, now: float) -> str:
        """No model request in flight: say what Pi is most likely doing."""
        last = self.last
        if last is None:
            return "idle: no request to the model yet"
        since = ago(now - last["t"])
        if last.get("error"):
            return (f"✗ the last request failed {since} ago ({last['error'][:80]}): Pi retries after its "
                    f"back-off, or has stopped")
        if last.get("outcome") == "client disconnected":
            return f"■ Pi aborted the last request {since} ago (Esc, or its own timeout)"
        if last.get("finish") == "tool_calls":
            calls = last.get("calls") or ["a tool"]
            more = f" (+{len(calls) - 1} more)" if len(calls) > 1 else ""
            return f"⚙ Pi is running {calls[0]}{more} — for {since}; the model is asked again when it is done"
        if last.get("finish") == "length":
            return f"⚠ the last reply was cut off at max_tokens {since} ago: Pi has probably stopped"
        return f"✓ the model answered {since} ago: Pi is waiting for you"

    def show(self, e: dict) -> None:
        k = e.get("kind")
        rid = e.get("rid", "")
        if k == "tick":
            return self.tick(e)
        self.track(e)
        replay = " (earlier)" if e.get("replay") else ""
        if k == "request":
            self.pings[rid] = 0
            if e.get("max_tokens"):
                self.max_tokens[rid] = e["max_tokens"]
            extra = []
            if e.get("idle") is not None:
                extra.append(f"{e['idle']:.1f}s after the previous request ended")
            if e.get("repeat_of_failed"):
                extra.append(f"same messages as failed request {e['repeat_of_failed']}: Pi is retrying "
                             f"(the gap above was Pi's own back-off)")
            text = (f"▶ request{replay} model={e.get('model')} {'stream' if e.get('stream') else 'no-stream'} "
                    f"messages={e.get('messages')} tools={e.get('tools')} ~{kchars(e.get('tokens'))} tokens"
                    + (f" max_tokens={e['max_tokens']}" if e.get("max_tokens") else ""))
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
            self.sent_tokens[rid] = e.get("tokens") or 0
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
            text = f"  ⚠ 429 #{e.get('attempt')}: next try in {e.get('delay')}s (reset {e.get('reset', '?')})"
            left = re.search(r"remaining\W{0,3}(\d+)", e.get("message") or "", re.I)
            if left and re.search(r"tokens", e.get("message") or "", re.I):
                prompt, out = self.sent_tokens.get(rid, 0), self.max_tokens.get(rid, 0)
                remaining = int(left.group(1))
                if prompt and remaining >= prompt and out and remaining < prompt + out:
                    text += (f"\n    the router says {kchars(remaining)} tokens remain: more than this prompt "
                             f"(~{kchars(prompt)}), less than prompt + max_tokens (~{kchars(prompt + out)}). It "
                             f"probably reserves max_tokens: a smaller maxTokens in config.yaml would have passed")
                elif prompt and remaining < prompt:
                    text += (f"\n    the router says {kchars(remaining)} tokens remain, less than this prompt "
                             f"(~{kchars(prompt)}): the key's limit is used up")
            self.line(e, "warn", text)
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
            # Every TAIL_EVERY seconds one line: how far the model is, and the end of what it writes now.
            if e["t"] - self.last_tail.setdefault(rid, e["t"] - TAIL_EVERY + 4) < TAIL_EVERY:
                return  # (the first one ~4 s after the first token; the status line shows the rest)
            self.last_tail[rid] = e["t"]
            what = "thinking" if e.get("now_writing") == "reasoning" else "writing"
            text = (f"  … {what} {e['seconds']:.0f}s: text {kchars(e.get('chars'))}, reasoning "
                    f"{kchars(e.get('reasoning_chars'))} chars (~{total / secs:.0f} chars/s)")
            if e.get("tail"):
                text += f"\n    …{e['tail']}"
            self.line(e, "dim", text)
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
            cut = e.get("finish") in ("length", None)
            if e.get("finish") == "length":
                limit = self.max_tokens.get(rid)
                text += (f"\n    ⚠ CUT OFF: the reply hit max_tokens{f' ({limit})' if limit else ''}. Thinking and "
                         f"text share this limit: raise maxTokens of the model in config.yaml, or lower its "
                         f"thinking level (Shift+Tab in Pi)")
            elif e.get("finish") == "thinking_budget":
                text += ("\n    ⏹ STOPPED by the bridge: only thinking, no answer yet (REASONING_BUDGET); "
                         "it asks the model again to act")
            elif e.get("finish") is None:
                text += ("\n    ⚠ CUT OFF: the router ended the stream without a finish_reason (its own timeout "
                         "or a dropped connection)")
            if e.get("empty"):
                text += "\n    ⚠ empty reply (no text, no tool call)"
            self.line(e, "warn" if e.get("empty") or cut else "info", text)
        elif k == "resend":
            self.line(e, "warn", f"  ↻ {e.get('reason')}: requesting the reply again "
                                 f"({e.get('attempt')}/{e.get('of')})")
        elif k == "retry":
            self.line(e, "warn", f"  ↻ asking the model again ({e.get('attempt')}/{e.get('of')}): {e.get('reason')}")
        elif k == "ping":
            self.pings[rid] = self.pings.get(rid, 0) + 1
        elif k == "done":
            calls = e.get("call_args") or e.get("calls") or []
            text = f"  → to Pi: finish={e.get('finish')} tool calls: {len(calls) or 'none'}"
            for call in calls:
                text += f"\n    {call}"
            self.line(e, "info", text)
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
        self.max_tokens.pop(rid, None)
        self.last_tail.pop(rid, None)
        self.sent_tokens.pop(rid, None)
        self.line(e, "end_ok" if ok else "end_bad", text)


def read_events(url: str, history: int, raw: bool, events: queue.Queue) -> None:
    """In a thread: the bridge's NDJSON events into `events` (reconnecting), so the main thread can
    redraw the status line every second whether events come or not."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via HTTP(S)_PROXY
    first = True
    while True:
        try:
            with opener.open(f"{url}/bridge/events?history={history if first else 0}", timeout=30) as response:
                if not first:
                    events.put(("note", "reconnected to the bridge"))
                first = False
                for line in response:
                    if not line.strip():
                        continue
                    if raw:
                        print(line.decode().rstrip(), flush=True)
                        continue
                    try:
                        events.put(("event", json.loads(line)))
                    except ValueError:
                        continue
        except Exception as e:  # noqa: BLE001  (bridge restarting, old bridge without /bridge/events, …)
            if getattr(e, "code", None) == 404:
                events.put(("fatal", "this bridge has no /bridge/events: update it (pibox init && pibox setup && pibox up)"))
                return
            events.put(("error", f"bridge not reachable at {url} ({e}); retrying in 3s"))
            time.sleep(3)


def follow(url: str, history: int, raw: bool, view: View) -> None:
    events: queue.Queue = queue.Queue()
    threading.Thread(target=read_events, args=(url, history, raw, events), daemon=True).start()
    while True:
        try:
            kind, item = events.get(timeout=1.0)
        except queue.Empty:
            if not raw:
                view.render()
            continue
        if kind == "fatal":
            sys.exit(item)
        if kind in ("note", "error"):
            view.line({}, "dim" if kind == "note" else "err", item)
            continue
        view.show(item)
        if not item.get("replay") and item.get("kind") != "tick":
            view.render()


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
