"""Loopback-only, read-only HTTP viewer. Does not call a model or alter a run."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from urllib.parse import parse_qs, urlsplit

from harness.frontends.trajectory_reader import TrajectoryReader


def make_handler(reader, token):
    prefix = f"/t/{token}/"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if host not in allowed or (origin and origin != f"http://{host}"):
                self.send_error(403)
                return
            url = urlsplit(self.path)
            if not url.path.startswith(prefix):
                self.send_error(404)
                return
            path = url.path[len(prefix):]
            try:
                if path == "":
                    content = Path(__file__).with_name("trajectory_viewer.html").read_bytes()
                    mime = "text/html; charset=utf-8"
                elif path == "api/summary":
                    content = json.dumps(reader.summary(), ensure_ascii=False).encode()
                    mime = "application/json; charset=utf-8"
                elif path == "api/round":
                    identifier = parse_qs(url.query).get("id", [""])[0]
                    content = json.dumps(reader.detail(identifier), ensure_ascii=False).encode()
                    mime = "application/json; charset=utf-8"
                elif path == "api/step":
                    identifier = parse_qs(url.query).get("id", [""])[0]
                    content = json.dumps(reader.step(identifier), ensure_ascii=False).encode()
                    mime = "application/json; charset=utf-8"
                else:
                    self.send_error(404)
                    return
            except KeyError:
                self.send_error(404)
                return
            except (OSError, ValueError) as exc:
                # Missing audit objects may still be in flight during a live mirror.
                content = json.dumps({"error": type(exc).__name__, "message": "审计文件尚未同步完整，请稍后刷新"}).encode()
                mime = "application/json; charset=utf-8"
                self.send_response(503)
                self.send_header("Retry-After", "2")
            else:
                self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(content)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trajectory", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--connection-file", type=Path)
    parser.add_argument("--container", help="Optional read-only live Docker source")
    parser.add_argument("--remote-session-dir", help="Remote absolute .harness session directory")
    args = parser.parse_args()
    if bool(args.container) != bool(args.remote_session_dir):
        parser.error("--container and --remote-session-dir must be supplied together")
    reader = TrajectoryReader(args.trajectory)
    if args.container:
        from harness.frontends.audit_mirror import AuditMirror
        reader.mirror = AuditMirror(args.container, args.remote_session_dir, args.trajectory)
        reader.mirror.start()
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(reader, token))
    connection = {"url": f"http://127.0.0.1:{server.server_port}/t/{token}/",
                  "trajectory": str(reader.trajectory)}
    if args.connection_file:
        from harness.executor import LocalExecutor
        executor = LocalExecutor(args.connection_file.resolve().parent)
        executor.write_file(args.connection_file.resolve(), json.dumps(connection, indent=2).encode())
    print(json.dumps(connection), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if reader.mirror is not None:
            reader.mirror.close()


if __name__ == "__main__":
    main()
