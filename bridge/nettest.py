#!/usr/bin/env python3
"""Where does a request to URL hang? Run in the bridge container (`pibox nettest URL`), which has
the same way out as the sandbox's proxy: directly (UPSTREAM_NO_PROXY hosts) or through the proxy
on your Mac (UPSTREAM_PROXY). Each step has its own timeout, so nothing here hangs."""

from __future__ import annotations

import os
import socket
import ssl
import sys
import time
from urllib.parse import urlsplit

import httpx


def step(title: str, fn) -> None:
    started = time.monotonic()
    try:
        result = fn()
        print(f"  ✅ {title:<44} {time.monotonic() - started:5.1f}s  {result}")
    except Exception as e:  # noqa: BLE001
        kind = "TIMEOUT" if "timed out" in str(e).lower() or isinstance(e, (TimeoutError, httpx.TimeoutException)) else "FAILED"
        print(f"  ❌ {title:<44} {time.monotonic() - started:5.1f}s  {kind}: {e!r}"[:300])


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: nettest.py https://host/path")
        return 2
    url = sys.argv[1]
    u = urlsplit(url)
    host, port = u.hostname or "", u.port or (443 if u.scheme == "https" else 80)
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
    no_proxy = os.environ.get("NO_PROXY", "")
    listed = any(host == d or host.endswith("." + d.lstrip(".")) for d in no_proxy.split(",") if d.strip())
    print(f"{url}\n  upstream proxy (network.mac_proxy_port): {proxy or '(none)'}\n"
          f"  upstream_no_proxy: {no_proxy or '(none)'} → this host goes "
          f"{'DIRECTLY' if listed or not proxy else 'through the upstream proxy'}\n")
    print("From the VM, directly (no proxy):")
    step("DNS", lambda: ", ".join(sorted({i[4][0] for i in socket.getaddrinfo(host, port)})))
    step(f"TCP connect {host}:{port}", lambda: socket.create_connection((host, port), 10).close() or "connected")
    if u.scheme == "https":
        def tls():
            with socket.create_connection((host, port), 10) as s:
                with ssl.create_default_context().wrap_socket(s, server_hostname=host) as t:
                    return f"TLS ok ({t.version()})"
        step("TLS handshake", tls)
    timeout = httpx.Timeout(20.0, connect=10.0)
    step("GET (no proxy)", lambda: _get(httpx.Client(trust_env=False, timeout=timeout, verify=False), url))
    if proxy:
        print(f"\nThrough the upstream proxy {proxy}:")
        step("GET via upstream proxy", lambda: _get(httpx.Client(proxy=proxy, trust_env=False, timeout=timeout,
                                                                  verify=False), url))
    return 0


def _get(client: httpx.Client, url: str) -> str:
    with client:
        r = client.get(url)
        return f"HTTP {r.status_code}, {len(r.content)} bytes"


if __name__ == "__main__":
    sys.exit(main())
