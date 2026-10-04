import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import product
from pathlib import Path
from typing import Final
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import JsonValue, TypeAdapter

JSON_OBJECT: Final = TypeAdapter(dict[str, JsonValue])
REQUESTS: Final[queue.Queue[tuple[str, dict[str, str], dict[str, JsonValue]]]] = queue.Queue()
OUTPUT_FORMAT: Final[dict[str, JsonValue]] = {
    "type": "json_schema",
    "name": "answer",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    },
}


class Provider(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        body: Final = JSON_OBJECT.validate_json(self.rfile.read(int(self.headers["Content-Length"])))
        REQUESTS.put((self.path, {name.lower(): value for name, value in self.headers.items()}, body))
        if self.path.endswith("/alpha/search"):
            payload: Final = json.dumps({"results": [{"title": "fixture", "future": {"preserved": True}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("set-cookie", "provider-secret=fixture")
            self.send_header("x-codex-primary-used-percent", "4")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        response: Final = {
            "id": "resp_fixture",
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": "gpt-5.6-sol",
            "output": [
                {
                    "id": "msg_fixture",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "fixture", "annotations": []}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }
        event: Final = json.dumps({"type": "response.completed", "response": response})
        stream: Final = f"event: response.completed\ndata: {event}\n\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(stream)))
        self.end_headers()
        self.wfile.write(stream)


def request(
    port: int, path: str, body: dict[str, JsonValue], key: str = "sk-fixture"
) -> tuple[int, dict[str, str], bytes]:
    outgoing: Final = Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}", "originator": "codex_smoke"},
    )
    try:
        with urlopen(outgoing, timeout=20) as result:
            return result.status, dict(result.headers), result.read()
    except HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def verify(port: int) -> None:
    for path in ("/alpha/search", "/v1/alpha/search"):
        payload: Final[dict[str, JsonValue]] = {
            "model": "search-model",
            "id": "fixture-session",
            "commands": {"search_query": [{"q": "fixture"}]},
            "future": {"preserved": True},
        }
        status, headers, body = request(port, path, payload)
        assert status == 200, (status, body)
        assert json.loads(body) == {"results": [{"title": "fixture", "future": {"preserved": True}}]}
        assert "set-cookie" not in {name.lower() for name in headers}
        assert headers["x-codex-primary-used-percent"] == "4"
        upstream_path, upstream_headers, upstream_body = REQUESTS.get(timeout=5)
        assert upstream_path == "/backend-api/codex/alpha/search", upstream_path
        assert upstream_body == {**payload, "model": "gpt-5.6-sol"}, upstream_body
        assert upstream_headers["authorization"] == "Bearer fixture-provider-token"
        assert upstream_headers["originator"] == "codex_smoke"
        assert upstream_headers["session_id"] == "fixture-session"
    for instructions, cache_key in product(
        ("Caller-owned instructions.\nKeep whitespace. ", "", None),
        ("thread-one", "thread-one", "thread-two", "", None),
    ):
        payload: Final[dict[str, JsonValue]] = {
            "model": "search-model",
            "input": "fixture",
            "stream": True,
            "extra_headers": {"session_id": "fixture-fallback"},
            **({"instructions": instructions} if instructions is not None else {}),
            **({"prompt_cache_key": cache_key} if cache_key is not None else {}),
        }
        status, _, body = request(port, "/v1/responses", payload)
        assert status == 200, (status, body)
        assert b"response.completed" in body, body
        _, upstream_headers, upstream_body = REQUESTS.get(timeout=5)
        if instructions is None:
            assert "instructions" not in upstream_body, upstream_body
        else:
            assert upstream_body.get("instructions") == instructions, upstream_body
        if cache_key is None:
            assert "prompt_cache_key" not in upstream_body, upstream_body
        else:
            assert upstream_body.get("prompt_cache_key") == cache_key, upstream_body
        assert upstream_headers["session_id"] == (cache_key or "fixture-fallback"), upstream_headers
        assert "text" not in upstream_body, upstream_body
    verify_output_formats(port)
    assert 400 <= request(port, "/v1/alpha/search", {"model": "search-model"}, "sk-wrong")[0] < 500
    assert request(port, "/v1/alpha/search", {"model": ""})[0] == 400
    assert request(port, "/v1/alpha/search", {"model": "other-provider"})[0] == 400
    assert REQUESTS.empty(), "Rejected requests reached the provider"


def verify_output_formats(port: int) -> None:
    text_configs: Final[tuple[dict[str, JsonValue], ...]] = (
        {"format": OUTPUT_FORMAT, "verbosity": "low"},
        {"format": {"type": "json_object"}},
        {"format": {"type": "text"}, "verbosity": "high"},
        {"verbosity": "low"},
        {},
    )
    for text_config in text_configs:
        status, _, body = request(
            port,
            "/v1/responses",
            {
                "model": "search-model",
                "input": [{"role": "user", "content": "fixture"}],
                "stream": True,
                "text": text_config,
            },
        )
        assert status == 200, (status, body)
        assert b"response.completed" in body, body
        path, _, upstream_body = REQUESTS.get(timeout=5)
        assert path == "/backend-api/codex/responses", path
        assert upstream_body.get("text") == text_config, upstream_body

    status, _, body = request(
        port,
        "/v1/chat/completions",
        {
            "model": "search-model",
            "messages": [{"role": "user", "content": "fixture"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {key: value for key, value in OUTPUT_FORMAT.items() if key != "type"},
            },
        },
    )
    assert status == 200, (status, body)
    assert json.loads(body)["choices"][0]["message"]["content"] == "fixture", body
    path, _, upstream_body = REQUESTS.get(timeout=5)
    assert path == "/backend-api/codex/responses", path
    assert upstream_body.get("text") == {"format": OUTPUT_FORMAT}, upstream_body


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="litellm-image-smoke-") as directory:
        root: Final = Path(directory)
        (root / "auth.json").write_text(
            json.dumps(
                {"access_token": "fixture-provider-token", "account_id": "fixture", "expires_at": time.time() + 3600}
            )
        )
        with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as provider:
            threading.Thread(target=provider.serve_forever, daemon=True).start()
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port: Final = reservation.getsockname()[1]
            config: Final = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "model_list": [
                            {"model_name": "search-model", "litellm_params": {"model": "chatgpt/gpt-5.6-sol"}},
                            {
                                "model_name": "other-provider",
                                "litellm_params": {"model": "openai/gpt-5.6-sol", "api_key": "fixture"},
                            },
                        ],
                        "general_settings": {"master_key": "sk-fixture"},
                        "litellm_settings": {"telemetry": False},
                    }
                )
            )
            with (root / "proxy.log").open("w+") as log:
                process: Final = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "litellm.proxy.proxy_cli",
                        "--config",
                        str(config),
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--telemetry",
                        "False",
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env={
                        **os.environ,
                        "CHATGPT_TOKEN_DIR": str(root),
                        "CHATGPT_API_BASE": f"http://127.0.0.1:{provider.server_port}/backend-api/codex",
                        "CHATGPT_DEFAULT_INSTRUCTIONS": "Must not be injected",
                        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                        "LITELLM_MODE": "PRODUCTION",
                    },
                )
                try:
                    deadline: Final = time.monotonic() + 90
                    while True:
                        assert process.poll() is None, "Proxy exited during startup"
                        try:
                            with urlopen(f"http://127.0.0.1:{port}/health/liveliness", timeout=1) as health:
                                if health.status == 200:
                                    break
                        except (URLError, TimeoutError):
                            assert time.monotonic() < deadline, "Proxy startup timed out"
                            threading.Event().wait(0.2)
                    verify(port)
                except BaseException:
                    log.seek(0)
                    sys.stderr.write(log.read())
                    raise
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    provider.shutdown()
    sys.stdout.write(
        "PASS: real proxy HTTP, caller instructions, cache affinity, output formats, chat bridge, "
        "search routes, OAuth headers and rejected requests\n"
    )


if __name__ == "__main__":
    main()
