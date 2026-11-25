"""Run the server in-process over a temporary workspace laid out like the image's."""

import json
import sys
import threading
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from server import PROTOCOL, Config, Sandbox, make_server


@dataclass
class Running:
    url: str
    config: Config
    sandbox: Sandbox

    def raw(self, body: Any, *, timeout: float = 60) -> dict[str, Any]:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        request = urllib.request.Request(f"{self.url}/invocations", data=data, method="POST")  # noqa: S310
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 -- loopback test server
            parsed: dict[str, Any] = json.loads(response.read())
        return parsed

    def call(self, op: str, args: dict[str, Any] | None = None, *, timeout_seconds: float = 30) -> dict[str, Any]:
        return self.raw({"protocol": PROTOCOL, "op": op, "args": args or {}, "timeout_seconds": timeout_seconds})

    def result(self, op: str, args: dict[str, Any] | None = None, *, timeout_seconds: float = 30) -> dict[str, Any]:
        response = self.call(op, args, timeout_seconds=timeout_seconds)
        assert response["ok"] is True, response
        result: dict[str, Any] = response["result"]
        return result

    def ping(self) -> dict[str, Any]:
        with urllib.request.urlopen(f"{self.url}/ping", timeout=10) as response:  # noqa: S310 -- loopback test server
            parsed: dict[str, Any] = json.loads(response.read())
        return parsed


def layout(root: Path) -> Config:
    workspace = root / "mnt" / "workspace"
    workspace.mkdir(parents=True)
    home = root / "home" / "user"
    home.parent.mkdir(parents=True)
    home.symlink_to(workspace)
    mountinfo = root / "mountinfo"
    mountinfo.write_text("22 1 0:21 / / rw,relatime - overlay overlay rw\n")
    return Config(
        home=home,
        workspace=workspace,
        python=sys.executable,
        host="127.0.0.1",
        port=0,
        mountinfo=mountinfo,
    )


@pytest.fixture
def start(tmp_path: Path) -> Iterator[Callable[..., Running]]:
    started: list[tuple[Any, Sandbox]] = []

    def _start(**overrides: Any) -> Running:
        config = replace(layout(tmp_path / f"s{len(started)}"), **overrides)
        server, sandbox = make_server(config)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append((server, sandbox))
        host, port = server.server_address[:2]
        return Running(f"http://{host!s}:{port}", config, sandbox)

    yield _start
    for server, sandbox in started:
        server.shutdown()
        server.server_close()
        sandbox.kernel.stop()


@pytest.fixture
def running(start: Callable[..., Running]) -> Running:
    return start()
