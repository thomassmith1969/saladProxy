# salad_proxy

Local reverse proxy that forwards requests to a fixed upstream and injects `Salad-Api-Key` on every request.

## Quick Start

Install from GitHub:

```bash
pip install "git+https://github.com/thomassmith1969/saladProxy.git"
```

Run local proxy to a Salad endpoint:

```bash
salad-proxy https://currant-delicata-koevuxe7nrww04ng.salad.cloud 11434
```

Send traffic through localhost:

```bash
curl -N -i http://127.0.0.1:11434/api/chat -X POST -H 'Content-Type: application/json' -d '{"model":"qwen3.5:9b","messages":[{"role":"user","content":"What is deep learning?"}],"stream":true}'
```

> **Important:** The local proxy listens on plain HTTP. Do not call `https://127.0.0.1:11434`; that will trigger SSL negotiation errors such as `SSL routines::wrong version number` or `Bad request version`.

> **Troubleshooting:** If you see `Bad request version` or `TLS handshake received on HTTP listener`, you're sending HTTPS traffic to the local proxy. Use `http://127.0.0.1:<port>` for local requests.

## Install

Python package install:

```bash
pip install .
```

Or editable install while developing:

```bash
pip install -e .
```

Install directly from Git (public repo):

```bash
pip install "git+https://github.com/thomassmith1969/saladProxy.git"
```

## Usage (Python)

```bash
python -m salad_proxy <endpoint-url> <local-port> [salad-key]
```

Console script after pip install:

```bash
salad_proxy <endpoint-url> <local-port> [salad-key]
```

You can also use the hyphenated alias:

```bash
salad-proxy <endpoint-url> <local-port> [salad-key]
```

## Usage (uvx)

From the public Git repo:

```bash
uvx --from "git+https://github.com/thomassmith1969/saladProxy.git" salad-proxy <endpoint-url> <local-port> [salad-key]
```

From a local checkout:

```bash
uvx --from /absolute/path/to/saladProxy salad-proxy <endpoint-url> <local-port> [salad-key]
```

If published to an index, run by package name:

```bash
uvx salad-proxy <endpoint-url> <local-port> [salad-key]
```

## Usage (pipx run)

From the public Git repo:

```bash
pipx run --spec "git+https://github.com/thomassmith1969/saladProxy.git" salad-proxy <endpoint-url> <local-port> [salad-key]
```

From a local checkout:

```bash
pipx run --spec /absolute/path/to/saladProxy salad-proxy <endpoint-url> <local-port> [salad-key]
```

If published to an index, run by package name:

```bash
pipx run --spec salad-proxy salad-proxy <endpoint-url> <local-port> [salad-key]
```

Examples:

```bash
python -m salad_proxy https://araza-bokchoy-j19ktqyrbmbb1t43.salad.cloud 9000
python -m salad_proxy https://my-upstream.example.com:443/base/path 9000 sk_live_xxx
```

Then send traffic to your local proxy, for example:

```bash
curl http://127.0.0.1:9000/health
```

## Key Resolution Priority

1. CLI positional arg: `[salad-key]`
2. `SALAD_AUTH_KEY` environment variable
3. `~/.SALAD_AUTH_KEY` file contents

If no key is found, startup fails with an error.

## Notes

- Python module names cannot contain `-`, so use `python -m salad_proxy` (underscore) rather than `python -m salad-proxy`.
- This is a reverse proxy (fixed upstream), not a general forward proxy.
- Supports HTTP and HTTPS endpoint URLs.
- If the endpoint includes a base path, local request paths are appended to that base path.
- `CONNECT` tunneling is intentionally rejected.

## License

This project is licensed under the MIT License. See `LICENSE` for details.

## Exact Method Behavior

- `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`, `HEAD`: proxied upstream with method preserved.
- `CONNECT`: rejected locally with `405` and message `CONNECT is not supported by this reverse proxy`.
- Any other HTTP method (for example `TRACE`): not implemented by handler, so Python BaseHTTPRequestHandler returns `501 Unsupported method`.

Method behavior is the same for all paths. There are no route-specific handlers; every path is forwarded to the configured upstream path mapping.
