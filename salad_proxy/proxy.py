from __future__ import annotations

import argparse
import http.client
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence
from urllib.parse import SplitResult, urlsplit

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

UPSTREAM_TIMEOUT_SECONDS = 300


class ProxyConfig:
    def __init__(self, upstream: SplitResult, auth_key: str) -> None:
        self.upstream = upstream
        self.auth_key = auth_key


class SaladProxyHandler(BaseHTTPRequestHandler):
    config: ProxyConfig

    protocol_version = "HTTP/1.1"

    def _read_request_body(self) -> bytes:
        content_length = self.headers.get("Content-Length")
        if not content_length:
            return b""

        try:
            length = int(content_length)
        except ValueError:
            return b""

        if length <= 0:
            return b""

        return self.rfile.read(length)

    def _build_upstream_headers(self) -> dict[str, str]:
        upstream_headers: dict[str, str] = {}

        for name, value in self.headers.items():
            lowered = name.lower()
            if lowered in HOP_BY_HOP_HEADERS or lowered == "host":
                continue
            upstream_headers[name] = value

        upstream_headers["Salad-Api-Key"] = self.config.auth_key
        return upstream_headers

    def _build_upstream_url(self) -> str:
        incoming = urlsplit(self.path)
        incoming_path = incoming.path or "/"
        if not incoming_path.startswith("/"):
            incoming_path = f"/{incoming_path}"

        base_path = self.config.upstream.path.rstrip("/")
        if base_path:
            upstream_path = f"{base_path}{incoming_path}"
        else:
            upstream_path = incoming_path

        if incoming.query:
            return f"{upstream_path}?{incoming.query}"

        return upstream_path

    def _proxy(self):
        request_body = self._read_request_body()
        upstream_headers = self._build_upstream_headers()
        upstream_url = self._build_upstream_url()

        connection_cls = (
            http.client.HTTPSConnection
            if self.config.upstream.scheme == "https"
            else http.client.HTTPConnection
        )
        port = self.config.upstream.port
        if port is None:
            port = 443 if self.config.upstream.scheme == "https" else 80

        host = self.config.upstream.hostname
        if not host:
            self.send_error(502, "Upstream host is missing")
            return

        connection = connection_cls(host, port, timeout=UPSTREAM_TIMEOUT_SECONDS)

        try:
            try:
                connection.request(
                    method=self.command,
                    url=upstream_url,
                    body=request_body if request_body else None,
                    headers=upstream_headers,
                )
                response = connection.getresponse()
            except (OSError, socket.timeout, http.client.HTTPException) as exc:
                # Return a gateway error instead of dropping the client socket.
                try:
                    self.send_error(502, f"Upstream request failed: {exc}")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

            self.send_response(response.status, response.reason)

            upstream_has_content_length = False
            for header_name, header_value in response.getheaders():
                lowered = header_name.lower()
                if lowered in HOP_BY_HOP_HEADERS:
                    continue
                if lowered in {"server", "date"}:
                    continue
                if lowered == "content-length":
                    upstream_has_content_length = True
                self.send_header(header_name, header_value)

            # For streamed responses without Content-Length, terminate by closing.
            if not upstream_has_content_length and self.command != "HEAD":
                self.send_header("Connection", "close")
                self.close_connection = True

            self.end_headers()
            self.wfile.flush()

            if self.command == "HEAD":
                return

            try:
                while True:
                    if hasattr(response, "read1"):
                        chunk = response.read1(8192)  # type: ignore[attr-defined]
                    else:
                        chunk = response.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # Client disconnected while streaming response.
                return
        finally:
            connection.close()

    def do_GET(self):  # noqa: N802
        self._proxy()

    def do_POST(self):  # noqa: N802
        self._proxy()

    def do_PUT(self):  # noqa: N802
        self._proxy()

    def do_PATCH(self):  # noqa: N802
        self._proxy()

    def do_DELETE(self):  # noqa: N802
        self._proxy()

    def do_OPTIONS(self):  # noqa: N802
        self._proxy()

    def do_HEAD(self):  # noqa: N802
        self._proxy()

    def do_CONNECT(self):  # noqa: N802
        self.send_error(405, "CONNECT is not supported by this reverse proxy")


def parse_upstream_endpoint(raw_target: str) -> SplitResult:
    candidate = raw_target.strip()
    if not candidate:
        raise ValueError("Endpoint URL must not be empty")

    if "://" not in candidate:
        candidate = f"http://{candidate}"

    parsed = urlsplit(candidate)

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Remote target must use http or https scheme")

    if not parsed.hostname:
        raise ValueError("Endpoint URL must include a host")

    if parsed.query or parsed.fragment:
        raise ValueError("Endpoint URL must not include query or fragment")

    if parsed.path and not parsed.path.startswith("/"):
        raise ValueError("Endpoint URL path must start with '/'")

    return parsed


def load_key_from_file(path: Path) -> str | None:
    if not path.exists():
        return None

    content = path.read_text(encoding="utf-8").strip()
    return content or None


def resolve_auth_key(cli_key: str | None) -> str:
    if cli_key and cli_key.strip():
        return cli_key.strip()

    env_key = os.getenv("SALAD_AUTH_KEY", "").strip()
    if env_key:
        return env_key

    file_key = load_key_from_file(Path.home() / ".SALAD_AUTH_KEY")
    if file_key:
        return file_key

    raise ValueError(
        "Missing Salad auth key. Provide as CLI arg, SALAD_AUTH_KEY env var, or ~/.SALAD_AUTH_KEY file"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m salad_proxy",
        description="Run a local reverse proxy that injects Salad-Api-Key for an upstream server",
    )
    parser.add_argument(
        "remote",
        help="Endpoint URL, e.g. https://your-endpoint.salad.cloud[/optional/base/path]",
    )
    parser.add_argument(
        "local_port",
        type=int,
        help="Local port to bind for incoming requests",
    )
    parser.add_argument(
        "salad_key",
        nargs="?",
        default=None,
        help="Optional Salad API key. Falls back to SALAD_AUTH_KEY or ~/.SALAD_AUTH_KEY",
    )
    return parser


def run_proxy(remote: str, local_port: int, salad_key: str | None) -> int:
    if not (1 <= local_port <= 65535):
        raise ValueError("Local port must be between 1 and 65535")

    upstream = parse_upstream_endpoint(remote)
    auth_key = resolve_auth_key(salad_key)

    proxy_config = ProxyConfig(upstream=upstream, auth_key=auth_key)

    class ConfiguredHandler(SaladProxyHandler):
        pass

    ConfiguredHandler.config = proxy_config

    server = ThreadingHTTPServer(("0.0.0.0", local_port), ConfiguredHandler)

    upstream_netloc = upstream.netloc
    upstream_path = upstream.path or ""

    print(
        f"Salad proxy listening on 0.0.0.0:{local_port} -> {upstream.scheme}://{upstream_netloc}{upstream_path}",
        flush=True,
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping proxy...", flush=True)
    finally:
        server.server_close()

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return run_proxy(args.remote, args.local_port, args.salad_key)
    except ValueError as exc:
        parser.error(str(exc))
        return 2


__all__ = ["main", "run_proxy"]
