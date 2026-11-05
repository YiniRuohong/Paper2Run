"""Desktop backend entry point: ``python -I -B -X utf8 serve.py [--port N]``.

Started by the Electron main process (``apps/desktop/main.cjs``) with the
bundled python-build-standalone interpreter. Standard library + uvicorn (and
the installed ``p2r_api`` app) only.

Contract with the parent process:

* binds a loopback socket itself and prints exactly one JSON line
  ``{"event": "listening", "port": N}`` to stdout before serving. ``--port``
  is a *preference*: if it is taken the OS picks a free port (``:0``), so
  there is never a probe-then-bind race. A stable port keeps the UI origin
  (and therefore its localStorage) stable across app restarts;
* everything else (uvicorn/app logs) goes to stderr;
* stdin is a pipe held by the parent. A daemon thread reads it until EOF -
  which happens when the parent exits for any reason, including SIGKILL or
  a crash - then asks uvicorn to shut down and hard-exits after a short
  grace period, so the backend can never outlive the desktop app;
* SIGTERM/SIGINT (posix) trigger uvicorn's normal graceful shutdown.

Requests whose ``Host`` is not this loopback address, or whose ``Origin`` (when
sent) is not this origin, are rejected (421). The local API runs without
authentication or CSRF checks, so this blocks DNS-rebinding and cross-origin
pages in the user's browser from driving it.

Configuration comes from the environment the parent sets: ``P2R_DEMO_DIR``
(data root, also used as cwd so stray relative files stay inside it) and
``P2R_WEB_DIST`` (built web UI). ``-I`` ignores ``PYTHON*`` variables, which
is why ``-B`` (never write bytecode into the read-only app bundle) and
``-X utf8`` are passed as interpreter flags by the parent.

Keep module import free of side effects: the app is built only in ``main``.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time

GRACE_SECONDS = 5.0
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


def _bind(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            # Windows SO_REUSEADDR would let another process steal the port;
            # exclusive use is the safe equivalent.
            sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5), 1)
        elif sys.platform.startswith("linux"):
            # Allows an immediate re-bind of the remembered port while the
            # previous instance's connections sit in TIME_WAIT; on Linux this
            # never allows two simultaneous listeners. Not set on macOS/BSD,
            # where it lets 127.0.0.1:P bind over another app's *:P listener
            # and silently shadow it.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(False)
    return sock


def _bind_preferred(host: str, port: int, strict: bool) -> socket.socket:
    if port:
        try:
            return _bind(host, port)
        except OSError as exc:
            if strict:
                raise
            print(f"[serve] port {port} unavailable ({exc}); using a free port", file=sys.stderr, flush=True)
    return _bind(host, 0)


def host_guard(app, port: int):
    """ASGI wrapper: only serve requests addressed to this loopback origin.

    Also refuses cross-site requests from browsers (``Sec-Fetch-Site``) and
    forbids framing, so a web page cannot drive or clickjack the local UI.
    """
    allowed = {f"{host}:{port}".encode("ascii") for host in LOOPBACK_HOSTS}
    origins = {b"http://" + host for host in allowed}
    frame_headers = [(b"x-frame-options", b"DENY"),
                     (b"content-security-policy", b"frame-ancestors 'none'")]

    async def guarded(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = dict(scope.get("headers", ()))
            origin = headers.get(b"origin")
            fetch_site = headers.get(b"sec-fetch-site", b"").lower()
            if (headers.get(b"host", b"").lower() not in allowed
                    or (origin is not None and origin.lower() not in origins)
                    or fetch_site in (b"cross-site", b"same-site")):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                body = b'{"error":{"code":"MISDIRECTED_REQUEST","message":"unexpected Host or Origin header"}}'
                await send({"type": "http.response.start", "status": 421,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode("ascii"))]})
                await send({"type": "http.response.body", "body": body})
                return
        if scope["type"] == "http":
            async def framed_send(message):
                if message["type"] == "http.response.start":
                    message = {**message, "headers": [*message.get("headers", ()), *frame_headers]}
                await send(message)
            await app(scope, receive, framed_send)
            return
        await app(scope, receive, send)

    return guarded


def _watch_stdin(server) -> None:
    """Block on stdin until EOF (parent gone), then shut down for sure."""
    try:
        stream = sys.stdin.buffer
        while stream.read(4096):
            pass
    except (OSError, ValueError, AttributeError):
        pass
    server.should_exit = True
    time.sleep(GRACE_SECONDS)
    os._exit(0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paper2Run desktop backend")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1",),
                        help="loopback only")
    parser.add_argument("--port", type=int, default=0,
                        help="preferred port; falls back to a free one (default: OS picks)")
    parser.add_argument("--strict-port", action="store_true",
                        help="fail instead of falling back when --port is taken")
    parser.add_argument("--no-stdin-watchdog", action="store_true",
                        help="do not exit on stdin EOF (manual runs from a terminal)")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be 0-65535")

    data_dir = os.environ.get("P2R_DEMO_DIR")
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
        os.chdir(data_dir)

    import uvicorn
    from p2r_api.main import create_local_app

    sock = _bind_preferred(args.host, args.port, args.strict_port)
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        host_guard(create_local_app(), port),
        log_level="info",
        access_log=False,
        server_header=False,
        proxy_headers=False,
        timeout_graceful_shutdown=int(GRACE_SECONDS),
    )
    server = uvicorn.Server(config)

    if not args.no_stdin_watchdog:
        threading.Thread(target=_watch_stdin, args=(server,), name="stdin-watchdog", daemon=True).start()

    sys.stdout.write(json.dumps({"event": "listening", "port": port}) + "\n")
    sys.stdout.flush()
    server.run(sockets=[sock])
    return 0


if __name__ == "__main__":
    sys.exit(main())
