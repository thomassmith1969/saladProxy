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

STREAM_CHUNK_SIZE = 8192
DEFAULT_UPSTREAM_TIMEOUT_SECONDS = 3600.0


class ProxyConfig:
    def __init__(
        self,
        upstream: SplitResult,
        auth_key: str,
        upstream_timeout_seconds: float | None,
    ) -> None:
        self.upstream = upstream
        self.auth_key = auth_key
        self.upstream_timeout_seconds = upstream_timeout_seconds


class SaladProxyHandler(BaseHTTPRequestHandler):
    config: ProxyConfig

    protocol_version = "HTTP/1.1"

    def parse_request(self) -> bool:
        raw = getattr(self, 'raw_requestline', b'')
        if isinstance(raw, bytes) and raw.startswith(b'\x16\x03'):
            self.send_error(
                400,
                "TLS handshake received on HTTP listener. Use http://127.0.0.1:<port> for the local proxy.",
            )
            return False
        return super().parse_request()

    @staticmethod
    def _connection_header_tokens(headers) -> set[str]:
        raw = headers.get("Connection", "")
        return {token.strip().lower() for token in raw.split(",") if token.strip()}

    def _read_request_body(self) -> bytes:
        # Decode chunked uploads so we can forward a canonical body upstream.
        transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()
        if "chunked" in transfer_encoding:
            body = bytearray()
            while True:
                size_line = self.rfile.readline()
                if not size_line:
                    break

                size_token = size_line.split(b";", 1)[0].strip()
                if not size_token:
                    continue

                try:
                    chunk_size = int(size_token, 16)
                except ValueError:
                    raise ConnectionError("Invalid chunk size in request body")

                if chunk_size == 0:
                    # Read trailer section terminator and ignore trailer headers.
                    while True:
                        trailer_line = self.rfile.readline()
                        if trailer_line in (b"\r\n", b"\n", b""):
                            break
                    break

                chunk = self.rfile.read(chunk_size)
                if len(chunk) != chunk_size:
                    raise ConnectionError("Unexpected EOF while reading chunked body")
                body.extend(chunk)

                # Each chunk is followed by CRLF.
                chunk_terminator = self.rfile.read(2)
                if chunk_terminator != b"\r\n":
                    raise ConnectionError("Malformed chunked request terminator")

            return bytes(body)

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

    def _iter_request_body_chunks(self):
        transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()
        if "chunked" in transfer_encoding:
            while True:
                size_line = self.rfile.readline()
                if not size_line:
                    break

                size_token = size_line.split(b";", 1)[0].strip()
                if not size_token:
                    continue

                try:
                    chunk_size = int(size_token, 16)
                except ValueError as exc:
                    raise ConnectionError("Invalid chunk size in request body") from exc

                if chunk_size == 0:
                    while True:
                        trailer_line = self.rfile.readline()
                        if trailer_line in (b"\r\n", b"\n", b""):
                            break
                    break

                chunk = self.rfile.read(chunk_size)
                if len(chunk) != chunk_size:
                    raise ConnectionError("Unexpected EOF while reading chunked body")

                chunk_terminator = self.rfile.read(2)
                if chunk_terminator != b"\r\n":
                    raise ConnectionError("Malformed chunked request terminator")

                yield chunk
            return

        content_length = self.headers.get("Content-Length")
        if not content_length:
            return

        try:
            remaining = int(content_length)
        except ValueError:
            return

        if remaining <= 0:
            return

        while remaining > 0:
            chunk = self.rfile.read(min(STREAM_CHUNK_SIZE, remaining))
            if not chunk:
                raise ConnectionError("Unexpected EOF while reading request body")
            remaining -= len(chunk)
            yield chunk

    def _build_upstream_headers(self) -> dict[str, str]:
        upstream_headers: dict[str, str] = {}
        connection_tokens = self._connection_header_tokens(self.headers)

        for name, value in self.headers.items():
            lowered = name.lower()
            if (
                lowered in HOP_BY_HOP_HEADERS
                or lowered == "host"
                or lowered in connection_tokens
            ):
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
        try:
            upstream_headers = self._build_upstream_headers()
        except ConnectionError as exc:
            self.send_error(400, f"Bad request body: {exc}")
            return

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

        connection = connection_cls(
            host,
            port,
            timeout=self.config.upstream_timeout_seconds,
        )

        try:
            try:
                connection.putrequest(self.command, upstream_url, skip_host=True, skip_accept_encoding=True)

                if "Host" not in upstream_headers and "host" not in upstream_headers:
                    upstream_headers["Host"] = host if port in {80, 443} else f"{host}:{port}"

                body_chunks = self._iter_request_body_chunks()
                has_content_length = self.headers.get("Content-Length") is not None
                has_chunked_input = "chunked" in self.headers.get("Transfer-Encoding", "").lower()

                if has_chunked_input:
                    upstream_headers.pop("Content-Length", None)
                    upstream_headers.pop("content-length", None)

                if has_chunked_input and "Transfer-Encoding" not in upstream_headers and "transfer-encoding" not in {name.lower() for name in upstream_headers}:
                    upstream_headers["Transfer-Encoding"] = "chunked"

                for header_name, header_value in upstream_headers.items():
                    connection.putheader(header_name, header_value)

                connection.endheaders()

                try:
                    for chunk in body_chunks:
                        if has_chunked_input:
                            connection.send(f"{len(chunk):X}\r\n".encode("ascii"))
                            connection.send(chunk)
                            connection.send(b"\r\n")
                        else:
                            connection.send(chunk)

                    if has_chunked_input:
                        connection.send(b"0\r\n\r\n")
                except ConnectionError as exc:
                    try:
                        self.send_error(400, f"Bad request body: {exc}")
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return

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
            response_has_body = (
                self.command != "HEAD"
                and response.status not in {204, 304}
                and not (100 <= response.status < 200)
            )
            for header_name, header_value in response.getheaders():
                lowered = header_name.lower()
                if lowered in HOP_BY_HOP_HEADERS:
                    continue
                if lowered in {"server", "date"}:
                    continue
                if lowered == "content-length":
                    upstream_has_content_length = True
                self.send_header(header_name, header_value)

            # Preserve keep-alive by chunking streamed responses with unknown length.
            should_chunk_downstream = response_has_body and not upstream_has_content_length
            if should_chunk_downstream:
                self.send_header("Transfer-Encoding", "chunked")

            self.end_headers()
            self.wfile.flush()

            if not response_has_body:
                return

            try:
                while True:
                    if hasattr(response, "read1"):
                        chunk = response.read1(STREAM_CHUNK_SIZE)  # type: ignore[attr-defined]
                    else:
                        chunk = response.read(STREAM_CHUNK_SIZE)
                    if not chunk:
                        break

                    if should_chunk_downstream:
                        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                    else:
                        self.wfile.write(chunk)
                    self.wfile.flush()

                if should_chunk_downstream:
                    self.wfile.write(b"0\r\n\r\n")
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


def resolve_upstream_timeout_seconds() -> float | None:
    raw_timeout = os.getenv("SALAD_PROXY_UPSTREAM_TIMEOUT_SECONDS", "").strip()
    if not raw_timeout:
        return DEFAULT_UPSTREAM_TIMEOUT_SECONDS

    try:
        timeout_value = float(raw_timeout)
    except ValueError as exc:
        raise ValueError(
            "SALAD_PROXY_UPSTREAM_TIMEOUT_SECONDS must be a number (0 disables timeout)"
        ) from exc

    if timeout_value < 0:
        raise ValueError("SALAD_PROXY_UPSTREAM_TIMEOUT_SECONDS must be >= 0")

    if timeout_value == 0:
        return None

    return timeout_value


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

    upstream_timeout_seconds = resolve_upstream_timeout_seconds()
    proxy_config = ProxyConfig(
        upstream=upstream,
        auth_key=auth_key,
        upstream_timeout_seconds=upstream_timeout_seconds,
    )

    class ConfiguredHandler(SaladProxyHandler):
        pass

    ConfiguredHandler.config = proxy_config

    class ProxyServer(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 128

    server = ProxyServer(("0.0.0.0", local_port), ConfiguredHandler)

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
