#!/usr/bin/env python3
"""Readable timeline of a Pi session: prompts, model thinking/text, the tool calls Pi
actually executed with their results, errors and durations.

    python3 pi-trace.py                 # latest session of the current directory
    python3 pi-trace.py -f              # ... and keep following it (live)
    python3 pi-trace.py SESSION.jsonl   # a specific session file
    python3 pi-trace.py --max 0         # do not truncate long texts

Stdlib only. Pi stores sessions in ~/.pi/agent/sessions/<cwd>/*.jsonl
(or $PI_CODING_AGENT_DIR/sessions).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

COLORS = {"user": "36", "assistant": "32", "thinking": "3", "call": "33", "ok": "34", "err": "31", "meta": "35"}


def sessions_dir() -> Path:
    base = os.environ.get("PI_CODING_AGENT_DIR") or str(Path.home() / ".pi" / "agent")
    return Path(base) / "sessions"


def latest_session(cwd: str) -> Path:
    folder = sessions_dir() / ("--" + cwd.strip("/").replace("/", "-") + "--")
    files = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not files:
        sys.exit(f"no Pi sessions in {folder}")
    return files[-1]


class Printer:
    def __init__(self, max_chars: int, color: bool):
        self.max, self.color = max_chars, color

    def cut(self, text: str) -> str:
        if self.max and len(text) > self.max:
            return text[:self.max] + f"\n… [{len(text) - self.max} more chars]"
        return text

    def out(self, kind: str, title: str, body: str = "") -> None:
        head = f"\033[{COLORS[kind]}m{title}\033[0m" if self.color else title
        print(head)
        if body:
            print("\n".join("    " + line for line in self.cut(body.rstrip()).splitlines()))
        sys.stdout.flush()

    def entry(self, obj: dict) -> None:
        ts = (obj.get("timestamp") or "")[11:19]
        kind = obj.get("type")
        if kind == "model_change":
            return self.out("meta", f"{ts} model: {obj.get('provider')}/{obj.get('modelId')}")
        if kind == "thinking_level_change":
            return self.out("meta", f"{ts} thinking level: {obj.get('thinkingLevel')}")
        if kind != "message":
            return
        msg = obj.get("message") or {}
        role = msg.get("role")
        content = msg.get("content")
        parts = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        if role == "user":
            text = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")
            return self.out("user", f"{ts} USER", text)
        if role == "assistant":
            for p in parts:
                if p.get("type") == "thinking" and p.get("thinking"):
                    self.out("thinking", f"{ts} thinking", p["thinking"])
                elif p.get("type") == "text" and p.get("text", "").strip():
                    self.out("assistant", f"{ts} ASSISTANT", p["text"])
                elif p.get("type") == "toolCall":
                    args = json.dumps(p.get("arguments"), ensure_ascii=False, indent=2)
                    self.out("call", f"{ts} CALL {p.get('name')} ({p.get('id')})", args)
            usage = msg.get("usage") or {}
            extra = f" error={msg.get('errorMessage')}" if msg.get("errorMessage") else ""
            if usage or extra:
                self.out("meta", f"{ts}   stop={msg.get('stopReason')} in={usage.get('input')} "
                                 f"out={usage.get('output')} cacheRead={usage.get('cacheRead')} "
                                 f"reasoning={usage.get('reasoning')}{extra}")
            return
        if role == "toolResult":
            text = "\n".join(p.get("text", "") if p.get("type") == "text" else f"[{p.get('type')}]" for p in parts)
            failed = msg.get("isError")
            self.out("err" if failed else "ok",
                     f"{ts} {'FAILED' if failed else 'RESULT'} {msg.get('toolName')} "
                     f"({msg.get('toolCallId')}, {msg.get('durationMs')} ms)", text)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session", nargs="?", help="session .jsonl (default: latest for the current directory)")
    ap.add_argument("-f", "--follow", action="store_true", help="keep printing new entries")
    ap.add_argument("--cwd", default=os.getcwd(), help="project directory to pick the latest session for")
    ap.add_argument("--max", type=int, default=3000, help="truncate texts to N chars (0 = never)")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    path = Path(args.session) if args.session else latest_session(os.path.abspath(args.cwd))
    printer = Printer(args.max, color=sys.stdout.isatty() and not args.no_color)
    print(f"# {path}")
    with open(path, encoding="utf-8") as f:
        buf = ""
        while True:
            line = f.readline()
            if not line:
                if not args.follow:
                    break
                time.sleep(0.5)
                continue
            buf += line
            if not buf.endswith("\n"):
                continue  # partial line being written
            try:
                printer.entry(json.loads(buf))
            except ValueError:
                pass
            buf = ""


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:  # e.g. piped into `head`
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
