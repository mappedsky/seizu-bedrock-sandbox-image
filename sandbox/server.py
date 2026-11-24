"""A ``seizu-sandbox/1`` server for Amazon Bedrock AgentCore Runtime.

Serves ``POST /invocations`` (one protocol operation per request) and
``GET /ping`` on port 8080. The contract is the Seizu repository's
``docs/root/dev/sandbox-image-protocol.md``, vendored here as
``conformance/``.

Standard library only, and run as ``python3 -I`` so nothing the sandboxed code
writes (a user site directory, a module in the working directory, ``PYTHON*``
variables) can change the server itself.

Never log a request's arguments or a result: they carry the user's code, data
and output. Log the operation, its outcome and how long it took.
"""

import base64
import binascii
import json
import logging
import os
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROTOCOL = "seizu-sandbox/1"
PROTOCOL_VERSIONS = [1]
MARKER_FILENAME = ".seizu-sandbox"
KERNEL_PATH = Path(__file__).with_name("kernel.py")

_DEFAULT_TIMEOUT_SECONDS = 60
_DRAIN_GRACE_SECONDS = 1.0

logger = logging.getLogger("seizu_sandbox")


class OpError(Exception):
    """The operation itself failed; ``code`` is a protocol ``ErrorCode``."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message)
        self.code = code


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    return int(raw) if raw.strip() else default


@dataclass(frozen=True)
class Config:
    home: Path = Path("/home/user")
    workspace: Path = Path("/mnt/workspace")
    python: str = "/usr/local/bin/python3"
    host: str = "0.0.0.0"  # noqa: S104 -- AgentCore Runtime reaches the container on its own interface
    port: int = 8080
    max_output_bytes: int = 1 << 20
    max_file_bytes: int = 64 << 20
    max_request_bytes: int = 96 << 20
    storage_semantics: str = ""
    mountinfo: Path = Path("/proc/self/mountinfo")

    @classmethod
    def from_env(cls) -> "Config":
        base = cls()
        return cls(
            home=Path(os.environ.get("SEIZU_SANDBOX_HOME", str(base.home))),
            workspace=Path(os.environ.get("SEIZU_SANDBOX_WORKSPACE", str(base.workspace))),
            python=os.environ.get("SEIZU_SANDBOX_PYTHON", base.python),
            host=os.environ.get("SEIZU_SANDBOX_HOST", base.host),
            port=_env_int("SEIZU_SANDBOX_PORT", base.port),
            max_output_bytes=_env_int("SEIZU_SANDBOX_MAX_OUTPUT_BYTES", base.max_output_bytes),
            max_file_bytes=_env_int("SEIZU_SANDBOX_MAX_FILE_BYTES", base.max_file_bytes),
            max_request_bytes=_env_int("SEIZU_SANDBOX_MAX_REQUEST_BYTES", base.max_request_bytes),
            storage_semantics=os.environ.get("SEIZU_SANDBOX_STORAGE_SEMANTICS", "").strip().lower(),
        )

    def child_env(self) -> dict[str, str]:
        """The environment sandboxed code runs with: the server's, with ``$HOME`` fixed."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("SEIZU_SANDBOX_")}
        env["HOME"] = str(self.home)
        env["PYTHONUNBUFFERED"] = "1"
        return env


# -- storage semantics ---------------------------------------------------------


def _unescape_mountinfo(field: str) -> str:
    return field.encode().decode("unicode_escape") if "\\" in field else field


def storage_semantics(config: Config) -> str:
    """``object`` when the workspace is an S3 mount, ``posix`` otherwise.

    Read from the filesystem the workspace is mounted on, at the time of the
    call: AgentCore attaches the mount per invocation, never at start-up.
    ``SEIZU_SANDBOX_STORAGE_SEMANTICS`` overrides the detection.
    """
    if config.storage_semantics in {"posix", "object"}:
        return config.storage_semantics
    try:
        target = os.path.realpath(config.workspace)
        lines = config.mountinfo.read_text().splitlines()
    except OSError:
        return "posix"
    best: tuple[int, str] = (-1, "posix")
    for line in lines:
        head, _, tail = line.partition(" - ")
        fields, rest = head.split(), tail.split()
        if len(fields) < 5 or len(rest) < 2:
            continue
        mount_point = _unescape_mountinfo(fields[4])
        inside = target == mount_point or target.startswith(mount_point.rstrip("/") + "/")
        if not inside or len(mount_point) <= best[0]:
            continue
        fstype, source = rest[0].lower(), rest[1].lower()
        semantics = "object" if "s3" in fstype or "mountpoint-s3" in source else "posix"
        best = (len(mount_point), semantics)
    return best[1]


# -- bounded output capture ------------------------------------------------------


class _Capture(threading.Thread):
    """Drain a pipe to EOF, keeping the first ``cap`` bytes."""

    def __init__(self, stream: Any, cap: int) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.cap = cap
        self.kept = bytearray()
        self.dropped = 0
        self._lock = threading.Lock()

    def run(self) -> None:
        try:
            while chunk := self.stream.read1(65536):
                with self._lock:
                    room = max(self.cap - len(self.kept), 0)
                    self.kept += chunk[:room]
                    self.dropped += len(chunk) - min(room, len(chunk))
        except (OSError, ValueError):
            pass

    def text(self) -> str:
        with self._lock:
            out = self.kept.decode("utf-8", errors="replace")
            if self.dropped:
                out += f"\n[... {self.dropped} more bytes not shown]\n"
            return out


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


# -- the interpreter ---------------------------------------------------------------


class PythonKernel:
    """One persistent interpreter per sandbox; calls are serialized."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._commands = -1
        self._replies = -1
        self._buffer = b""

    def _start(self) -> None:
        command_r, command_w = os.pipe()
        reply_r, reply_w = os.pipe()
        try:
            self._proc = subprocess.Popen(  # noqa: S603 -- fixed argv, no shell
                [self.config.python, str(KERNEL_PATH), str(command_r), str(reply_w), str(self.config.max_output_bytes)],
                pass_fds=(command_r, reply_w),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=self.config.home,
                env=self.config.child_env(),
                start_new_session=True,
            )
        except BaseException:
            for fd in (command_r, command_w, reply_r, reply_w):
                os.close(fd)
            raise
        os.close(command_r)
        os.close(reply_w)
        self._commands, self._replies, self._buffer = command_w, reply_r, b""

    def stop(self) -> None:
        """Kill the interpreter and everything it started; the next call starts a fresh one."""
        if self._proc is not None:
            _kill_group(self._proc)
            self._proc = None
        for fd in (self._commands, self._replies):
            if fd >= 0:
                os.close(fd)
        self._commands = self._replies = -1
        self._buffer = b""

    def _read_reply(self, deadline: float) -> bytes | None:
        """One reply line; ``None`` on timeout, ``b""`` if the interpreter died."""
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([self._replies], [], [], remaining)
            if not ready:
                return None
            chunk = os.read(self._replies, 1 << 20)
            if not chunk:
                return b""
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line

    def run(self, code: str, deadline: float) -> dict[str, Any]:
        if not self._lock.acquire(timeout=max(deadline - time.monotonic(), 0)):
            raise OpError("timeout", "the Python interpreter was busy with another call for the whole timeout")
        try:
            if self._proc is None or self._proc.poll() is not None:
                self.stop()
                self._start()
            try:
                os.write(self._commands, json.dumps({"code": code}).encode() + b"\n")
                line = self._read_reply(deadline)
            except BrokenPipeError:
                line = b""
            if line is None:
                self.stop()
                return {"restarted": True}
            if not line:
                self.stop()
                return {
                    "restarted": True,
                    "error": {"name": "KernelDied", "value": "the Python interpreter exited", "traceback": ""},
                }
            return dict(json.loads(line))
        finally:
            self._lock.release()


# -- operations ------------------------------------------------------------------------


def _arg(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str):
        raise OpError("invalid_args", f"{name!r} must be a string")
    return value


def _optional_arg(args: dict[str, Any], name: str) -> str | None:
    return None if args.get(name) is None else _arg(args, name)


@dataclass
class Sandbox:
    config: Config
    kernel: PythonKernel = field(init=False)

    def __post_init__(self) -> None:
        self.kernel = PythonKernel(self.config)

    def path(self, raw: str) -> Path:
        path = Path(raw)
        return path if path.is_absolute() else self.config.home / path

    # Each handler takes (args, deadline) and returns the op's result object.

    def hello(self, args: dict[str, Any], deadline: float) -> dict[str, Any]:
        marker_path = self.config.home / MARKER_FILENAME
        create = args.get("create_marker")
        if create is not None:
            if not isinstance(create, dict) or not all(
                isinstance(create.get(k), str) for k in ("deployment", "created_at")
            ):
                raise OpError("invalid_args", "create_marker needs string 'deployment' and 'created_at'")
            try:
                fd = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(json.dumps(create).encode())
        marker: dict[str, Any] | None = None
        try:
            loaded = json.loads(marker_path.read_bytes())
            marker = loaded if isinstance(loaded, dict) else None
        except FileNotFoundError:
            pass
        except ValueError:
            logger.warning("the sandbox marker is not valid JSON; reporting no marker")
        return {
            "protocol_versions": PROTOCOL_VERSIONS,
            "capabilities": [],
            "home": str(self.config.home),
            "storage_semantics": storage_semantics(self.config),
            "marker": marker,
        }

    def run_python(self, args: dict[str, Any], deadline: float) -> dict[str, Any]:
        code = _arg(args, "code")
        return self.kernel.run(code, deadline)

    def run_bash(self, args: dict[str, Any], deadline: float) -> dict[str, Any]:
        command = _arg(args, "command")
        cwd_arg = _optional_arg(args, "cwd")
        cwd = self.path(cwd_arg) if cwd_arg else self.config.home
        if not cwd.is_dir():
            raise OpError("not_found", f"{cwd} is not a directory")
        proc = subprocess.Popen(  # noqa: S603 -- running the caller's command is the point
            ["/bin/bash", "-c", command],
            cwd=cwd,
            env=self.config.child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        captures = (
            _Capture(proc.stdout, self.config.max_output_bytes),
            _Capture(proc.stderr, self.config.max_output_bytes),
        )
        for capture in captures:
            capture.start()
        timed_out = False
        try:
            proc.wait(timeout=max(deadline - time.monotonic(), 0))
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
        # A background job may keep the pipes open; report what has arrived.
        for capture in captures:
            capture.join(_DRAIN_GRACE_SECONDS)
        return {
            "stdout": captures[0].text(),
            "stderr": captures[1].text(),
            "exit_code": -1 if timed_out else proc.returncode,
            "timed_out": timed_out,
        }

    def read_file(self, args: dict[str, Any], deadline: float) -> dict[str, Any]:
        raw = _arg(args, "path")
        path = self.path(raw)
        if not path.is_file():
            raise OpError("not_found", raw)
        size = path.stat().st_size
        if size > self.config.max_file_bytes:
            raise OpError("invalid_args", f"{raw} is {size} bytes; the limit is {self.config.max_file_bytes}")
        data = path.read_bytes()
        return {"content_b64": base64.b64encode(data).decode(), "size": len(data)}

    def write_file(self, args: dict[str, Any], deadline: float) -> dict[str, Any]:
        raw = _arg(args, "path")
        encoded = _arg(args, "content_b64")
        try:
            data = base64.b64decode(encoded, validate=True)
        except binascii.Error as exc:
            raise OpError("invalid_args", f"content_b64 is not base64: {exc}") from None
        path = self.path(raw)
        if path.is_dir():
            raise OpError("invalid_args", f"{raw} is a directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written in place, in one pass: an object-store workspace cannot rename.
        with path.open("wb") as handle:
            handle.write(data)
        return {"size": len(data)}

    def list_files(self, args: dict[str, Any], deadline: float) -> dict[str, Any]:
        raw = _arg(args, "path")
        path = self.path(raw)
        if not path.is_dir():
            raise OpError("not_found", raw)
        entries = [
            {"name": entry.name, "type": "dir" if entry.is_dir() else "file"}
            for entry in sorted(os.scandir(path), key=lambda e: e.name)
        ]
        return {"entries": entries}

    def wipe(self, args: dict[str, Any], deadline: float) -> dict[str, Any]:
        removed = 0
        failures: list[str] = []
        try:
            entries = list(os.scandir(self.config.workspace))
        except FileNotFoundError:
            return {"removed": 0}
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path)
                else:
                    os.unlink(entry.path)
                removed += 1
            except OSError as exc:
                failures.append(f"{entry.name}: {exc.strerror or exc}")
        if failures:
            raise OpError("internal", "could not remove " + "; ".join(failures))
        return {"removed": removed}

    def handlers(self) -> dict[str, Callable[[dict[str, Any], float], dict[str, Any]]]:
        return {
            "hello": self.hello,
            "run_python": self.run_python,
            "run_bash": self.run_bash,
            "read_file": self.read_file,
            "write_file": self.write_file,
            "list_files": self.list_files,
            "wipe": self.wipe,
        }

    def dispatch(self, body: Any) -> dict[str, Any]:
        """Run one request envelope; always returns a response envelope."""
        started = time.monotonic()
        op = body.get("op") if isinstance(body, dict) else None
        try:
            if not isinstance(body, dict):
                raise OpError("invalid_args", "the request must be a JSON object")
            if body.get("protocol") != PROTOCOL:
                raise OpError("unsupported_protocol", f"this image speaks {PROTOCOL}")
            handler = self.handlers().get(op) if isinstance(op, str) else None
            if handler is None:
                raise OpError("unsupported_op", f"unknown op {op!r}"[:200])
            args = body.get("args")
            if args is None:
                args = {}
            if not isinstance(args, dict):
                raise OpError("invalid_args", "args must be an object")
            timeout = body.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
            if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
                raise OpError("invalid_args", "timeout_seconds must be a positive number")
            response: dict[str, Any] = {"ok": True, "result": handler(args, started + timeout)}
            outcome = "ok"
        except OpError as exc:
            response = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
            outcome = exc.code
        except Exception as exc:
            logger.exception("op %s failed", op if isinstance(op, str) else "?")
            response = {"ok": False, "error": {"code": "internal", "message": f"{type(exc).__name__}: {exc}"}}
            outcome = "internal"
        logger.info(
            "op=%s outcome=%s seconds=%.3f",
            op if isinstance(op, str) and op in self.handlers() else "?",
            outcome,
            time.monotonic() - started,
        )
        return response


# -- HTTP ----------------------------------------------------------------------------------


class Health:
    """``/ping`` state: busy while any invocation is in flight."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = 0
        self._changed = int(time.time())

    def enter(self) -> None:
        with self._lock:
            self._busy += 1
            if self._busy == 1:
                self._changed = int(time.time())

    def leave(self) -> None:
        with self._lock:
            self._busy -= 1
            if self._busy == 0:
                self._changed = int(time.time())

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"status": "HealthyBusy" if self._busy else "Healthy", "time_of_last_update": self._changed}


def make_handler(sandbox: Sandbox, health: Health) -> type[BaseHTTPRequestHandler]:
    max_request = sandbox.config.max_request_bytes

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "seizu-sandbox"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 -- the base class's signature
            pass

        def _send(self, status: int, payload: dict[str, Any], *, close: bool = False) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 -- http.server's naming
            if self.path.split("?", 1)[0] == "/ping":
                self._send(HTTPStatus.OK, health.status())
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 -- http.server's naming
            if self.path.split("?", 1)[0] != "/invocations":
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"}, close=True)
                return
            try:
                length = int(self.headers.get("Content-Length") or "")
            except ValueError:
                length = -1
            if length < 0 or length > max_request:
                message = "a Content-Length is required" if length < 0 else f"request exceeds {max_request} bytes"
                self._send(
                    HTTPStatus.OK, {"ok": False, "error": {"code": "invalid_args", "message": message}}, close=True
                )
                return
            raw = self.rfile.read(length)
            health.enter()
            try:
                try:
                    body = json.loads(raw)
                except ValueError:
                    response = {"ok": False, "error": {"code": "invalid_args", "message": "the body is not JSON"}}
                else:
                    response = sandbox.dispatch(body)
                self._send(HTTPStatus.OK, response)
            finally:
                health.leave()

    return Handler


def make_server(config: Config) -> tuple[ThreadingHTTPServer, Sandbox]:
    sandbox = Sandbox(config)
    server = ThreadingHTTPServer((config.host, config.port), make_handler(sandbox, Health()))
    server.daemon_threads = True
    return server, sandbox


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stderr)
    config = Config.from_env()
    server, sandbox = make_server(config)

    def _stop(signum: int, frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    logger.info("serving %s on %s:%d", PROTOCOL, config.host, config.port)
    try:
        server.serve_forever()
    finally:
        sandbox.kernel.stop()
        server.server_close()


if __name__ == "__main__":
    main()
