"""Egress policy for the agent sandbox (mitmproxy addon).

Clients send plain HTTP proxy requests ("GET http://host/path"), so the method is visible without
decrypting anything. Allowed: GET/HEAD to public hosts on the allowed ports, without a request body
or protocol upgrade. Encrypted tunnels (CONNECT) are refused, the proxy couldn't see what goes
through them, except to the hosts in EGRESS_TUNNEL: your artifact repositories, which answer HTTPS
only. Those tunnels pass through untouched (no decryption, the client sees the real certificate). Everything refused is answered by the proxy itself and never leaves it. Allowed
requests are paused for approval in the mitmproxy console, except for hosts in EGRESS_NO_APPROVAL,
then forwarded: to the upstream proxy when mitmproxy runs with --mode upstream:URL, or directly for
hosts in UPSTREAM_NO_PROXY (an internal artifact repository, for example).

Environment (all optional; lists are comma-separated domains, subdomains included):
  EGRESS_ALLOW_ONLY     only these domains
  EGRESS_DENY           always blocked
  EGRESS_ALLOW_PRIVATE  allowed although they resolve to private addresses (an internal package mirror)
  EGRESS_NO_APPROVAL    allowed without pausing (package registries)
  EGRESS_PORTS          allowed ports, default 80,443
  EGRESS_TUNNEL         encrypted tunnels (CONNECT) allowed to these, without approval
  UPSTREAM_PROXY        the upstream proxy of --mode upstream (needed to know where the rest goes)
  UPSTREAM_NO_PROXY     domains reached directly, not through the upstream proxy
  EGRESS_LOG            journal of every decision, default ~/.mitmproxy/egress.log
"""
import ipaddress
import logging
import os
import re
import socket
import time

from mitmproxy import ctx, http, tcp, udp
from mitmproxy.net import server_spec


def _domains(name: str) -> set:
    return {d.strip().lower().lstrip(".") for d in os.environ.get(name, "").split(",") if d.strip()}


ALLOWED_METHODS = {"GET", "HEAD"}
ALLOWED_PORTS = {int(p) for p in os.environ.get("EGRESS_PORTS", "80,443").split(",") if p.strip()}
ALLOW_ONLY = _domains("EGRESS_ALLOW_ONLY")
DENY = _domains("EGRESS_DENY")
ALLOW_PRIVATE = _domains("EGRESS_ALLOW_PRIVATE")
NO_APPROVAL = _domains("EGRESS_NO_APPROVAL")
TUNNEL = _domains("EGRESS_TUNNEL")
NO_PROXY = _domains("UPSTREAM_NO_PROXY")
UPSTREAM = server_spec.parse(os.environ["UPSTREAM_PROXY"], "http") if os.environ.get("UPSTREAM_PROXY") else None
LOG_FILE = os.environ.get("EGRESS_LOG") or os.path.expanduser("~/.mitmproxy/egress.log")
log = logging.getLogger("egress")


def _audit(level: int, message: str) -> None:
    log.log(level, message)                       # shown in the console's event log
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
    except OSError:
        pass


def _matches(host: str, domains: set) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def _private(host: str) -> bool:
    """True if the host is or resolves to a non-public address (LAN, loopback, metadata, Docker)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False                      # unresolvable here: only the upstream proxy can reach it
    return any(not ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in infos)


def _violation(host: str, port: int) -> str | None:
    host = host.lower().rstrip(".")
    if port not in ALLOWED_PORTS:
        return f"port {port} not allowed"
    if _matches(host, DENY):
        return f"{host} is denied"
    if _matches(host, ALLOW_PRIVATE):
        return None
    if ALLOW_ONLY and not _matches(host, ALLOW_ONLY):
        return f"{host} is not in EGRESS_ALLOW_ONLY"
    if _private(host):
        return f"{host} is a private/internal address"
    return None


def _block(flow: http.HTTPFlow, status: int, reason: str) -> None:
    flow.metadata["egress"] = "blocked"
    _audit(logging.WARNING, f"BLOCKED {flow.request.method} {flow.request.pretty_url}: {reason}")
    flow.response = http.Response.make(status, f"egress policy: {reason}\n".encode(),
                                       {"Content-Type": "text/plain"})


def running() -> None:
    # Tunnels to EGRESS_TUNNEL hosts pass through as they are: mitmproxy doesn't decrypt them.
    if TUNNEL:
        ctx.options.update(ignore_hosts=[rf"^(.+\.)?{re.escape(d)}:\d+$" for d in sorted(TUNNEL)])


def _route(flow, host: str) -> None:
    if UPSTREAM:                          # decided per request; nothing is connected yet (lazy)
        flow.server_conn.via = None if _matches(host, NO_PROXY) else UPSTREAM


def http_connect(flow: http.HTTPFlow) -> None:
    req = flow.request
    if not _matches(req.host, TUNNEL):
        return _block(flow, 403, "encrypted tunnels (CONNECT) are not allowed; request http:// URLs through this proxy")
    if req.port not in ALLOWED_PORTS:
        return _block(flow, 403, f"port {req.port} not allowed")
    if _matches(req.host, DENY):
        return _block(flow, 403, f"{req.host} is denied")
    flow.metadata["egress"] = "tunnel"
    _route(flow, req.host)
    _audit(logging.INFO, f"TUNNEL {req.host}:{req.port}")


def requestheaders(flow: http.HTTPFlow) -> None:
    req = flow.request
    if req.method.upper() not in ALLOWED_METHODS:
        return _block(flow, 405, f"method {req.method} not allowed (GET/HEAD only)")
    if req.headers.get("content-length", "0") not in ("", "0") or "transfer-encoding" in req.headers:
        return _block(flow, 400, "request bodies are not allowed")
    if "upgrade" in req.headers:
        return _block(flow, 400, "protocol upgrades (WebSocket) are not allowed")
    reason = _violation(req.host, req.port)
    if reason:
        return _block(flow, 403, reason)
    _route(flow, req.host)
    if UPSTREAM and _matches(req.host, NO_PROXY):
        req.authority = ""                    # direct: send "GET /path", not the proxy form
    if _matches(req.host, NO_APPROVAL):
        _audit(logging.INFO, f"ALLOW {req.method} {req.pretty_url}")
    else:
        _audit(logging.INFO, f"ASK {req.method} {req.pretty_url}")
        flow.intercept()                  # waits in the console until you resume (a) or kill (x) it


def response(flow: http.HTTPFlow) -> None:
    if flow.metadata.get("egress") not in ("blocked", "tunnel"):
        _audit(logging.INFO, f"DONE {flow.response.status_code} {flow.request.method} {flow.request.pretty_url}")


def error(flow: http.HTTPFlow) -> None:
    _audit(logging.INFO, f"FAILED {flow.request.method} {flow.request.pretty_url}: {flow.error}")


def _how(server) -> str:
    via = getattr(getattr(server, "via", None), "address", None)
    return f"via the upstream proxy {via[0]}:{via[1]}" if via else "directly"


def _hostport(server) -> tuple[str, int]:
    return tuple(server.address) if server.address else ("", 0)


# Tunnels are opaque, so the journal says how each went: "TUNNEL host" (asked), then CONNECTED
# (directly or via the upstream proxy, and how long that took) or CONNECT FAILED, and CLOSED with how
# long it was open. "TUNNEL" without either, or a CLOSED right when the client gave up, is a
# connection that hung behind the proxy (VPN, route, upstream proxy).
def server_connected(data) -> None:
    host, port = _hostport(data.server)
    if _matches(host, TUNNEL):
        took = (data.server.timestamp_tcp_setup or 0) - (data.server.timestamp_start or 0)
        _audit(logging.INFO, f"TUNNEL CONNECTED {host}:{port} {_how(data.server)} in {max(took, 0):.1f}s")


def server_connect_error(data) -> None:
    host, port = _hostport(data.server)
    _audit(logging.WARNING, f"CONNECT FAILED {host}:{port} {_how(data.server)}: {data.server.error}")


def server_disconnected(data) -> None:
    host, port = _hostport(data.server)
    if _matches(host, TUNNEL) and data.server.timestamp_start:
        took = (data.server.timestamp_end or time.time()) - data.server.timestamp_start
        _audit(logging.INFO, f"TUNNEL CLOSED {host}:{port} after {took:.1f}s")


def tcp_start(flow: tcp.TCPFlow) -> None:
    host, port = flow.server_conn.address or ("", 0)
    if _matches(host, TUNNEL) and port in ALLOWED_PORTS:   # the inside of an allowed tunnel
        return
    _audit(logging.WARNING, f"BLOCKED raw TCP to {flow.server_conn.address}")
    flow.kill()


def udp_start(flow: udp.UDPFlow) -> None:
    _audit(logging.WARNING, f"BLOCKED UDP to {flow.server_conn.address}")
    flow.kill()
