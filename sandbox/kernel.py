"""The persistent Python interpreter behind ``run_python``.

Started by the server as ``python kernel.py <command fd> <reply fd> <output cap>``.
Each line read from the command descriptor is ``{"code": "..."}``; each line
written to the reply descriptor is a ``RunPythonResult`` object. One namespace
lives for the life of the process, so variables and imports survive between
cells as they do in a notebook.

A cell's output is captured at the file-descriptor level, so ``print``, C
extensions and subprocesses the cell starts all land in it. Outside a cell,
descriptors 0-2 are ``/dev/null``.
"""

import ast
import asyncio
import inspect
import json
import linecache
import os
import sys
import threading
import traceback
from types import CodeType, TracebackType
from typing import Any

_DRAIN_GRACE_SECONDS = 0.5


class _Drain(threading.Thread):
    """Read one pipe to EOF, keeping the first ``cap`` bytes and counting the rest."""

    def __init__(self, fd: int, cap: int) -> None:
        super().__init__(daemon=True)
        self.fd = fd
        self.cap = cap
        self.kept = bytearray()
        self.dropped = 0
        self._lock = threading.Lock()

    def run(self) -> None:
        try:
            while chunk := os.read(self.fd, 65536):
                with self._lock:
                    room = self.cap - len(self.kept)
                    if room > 0:
                        self.kept += chunk[:room]
                    self.dropped += max(0, len(chunk) - max(room, 0))
        finally:
            os.close(self.fd)

    def text(self) -> str:
        with self._lock:
            out = self.kept.decode("utf-8", errors="replace")
            if self.dropped:
                out += f"\n[... {self.dropped} more bytes not shown]\n"
            return out


def _cap_text(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n[... {len(text) - cap} more characters not shown]"


def _format_error(exc: BaseException, tb: TracebackType | None) -> dict[str, str]:
    return {
        "name": type(exc).__name__,
        "value": str(exc),
        "traceback": "".join(traceback.format_exception(type(exc), exc, tb)),
    }


def _from_first_cell_frame(tb: TracebackType | None) -> TracebackType | None:
    """Drop the kernel's own frames so a traceback starts in the user's cell."""
    cursor = tb
    while cursor is not None:
        if cursor.tb_frame.f_code.co_filename.startswith("<cell-"):
            return cursor
        cursor = cursor.tb_next
    return tb


class Kernel:
    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.namespace: dict[str, Any] = {"__name__": "__main__", "__builtins__": __builtins__}
        self.cells = 0
        self.loop = asyncio.new_event_loop()

    def _compile(self, code: str) -> tuple[CodeType, CodeType | None]:
        self.cells += 1
        filename = f"<cell-{self.cells}>"
        linecache.cache[filename] = (len(code), None, code.splitlines(keepends=True), filename)
        tree = ast.parse(code, filename=filename, mode="exec")
        last: ast.Expression | None = None
        final = tree.body[-1] if tree.body else None
        if isinstance(final, ast.Expr):
            tree.body.pop()
            last = ast.Expression(final.value)
        flags = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
        body = compile(tree, filename, "exec", flags=flags, dont_inherit=True)
        tail = compile(last, filename, "eval", flags=flags, dont_inherit=True) if last is not None else None
        return body, tail

    def _evaluate(self, code: CodeType) -> Any:
        result = eval(code, self.namespace)  # noqa: S307 -- running the caller's code is the point
        if code.co_flags & inspect.CO_COROUTINE:
            result = self.loop.run_until_complete(result)
        return result

    def run_cell(self, code: str) -> dict[str, Any]:
        text = ""
        error: dict[str, str] | None = None
        saved_streams = sys.stdout, sys.stderr
        saved_fds = os.dup(1), os.dup(2)
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        drains = _Drain(out_r, self.cap), _Drain(err_r, self.cap)
        for drain in drains:
            drain.start()
        os.dup2(out_w, 1)
        os.dup2(err_w, 2)
        os.close(out_w)
        os.close(err_w)
        try:
            try:
                body, tail = self._compile(code)
            except SyntaxError as exc:
                error = _format_error(exc, None)
            else:
                try:
                    self._evaluate(body)
                    if tail is not None:
                        value = self._evaluate(tail)
                        if value is not None:
                            text = _cap_text(repr(value), self.cap)
                except BaseException as exc:  # noqa: BLE001 -- a cell's exception is its result
                    error = _format_error(exc, _from_first_cell_frame(exc.__traceback__))
        finally:
            for stream in (sys.stdout, sys.stderr, *saved_streams):
                try:
                    stream.flush()
                except Exception:  # noqa: BLE001, S110 -- a broken replacement stream must not lose the reply
                    pass
            sys.stdout, sys.stderr = saved_streams
            os.dup2(saved_fds[0], 1)
            os.dup2(saved_fds[1], 2)
            os.close(saved_fds[0])
            os.close(saved_fds[1])
        # A background process the cell started may still hold the pipe open;
        # return what has arrived rather than waiting for it to exit.
        for drain in drains:
            drain.join(_DRAIN_GRACE_SECONDS)
        return {
            "stdout": drains[0].text(),
            "stderr": drains[1].text(),
            "text": text,
            "error": error,
            "restarted": False,
        }


def main() -> None:
    command_fd, reply_fd, cap = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    # Notebook semantics: the working directory is importable, this file's is not.
    sys.path[0] = ""
    sys.argv = [""]
    kernel = Kernel(cap)
    with os.fdopen(command_fd, "rb") as commands, os.fdopen(reply_fd, "wb", buffering=0) as replies:
        for line in commands:
            try:
                code = json.loads(line)["code"]
            except (ValueError, KeyError, TypeError):
                reply: dict[str, Any] = {"error": {"name": "ProtocolError", "value": "malformed cell", "traceback": ""}}
            else:
                reply = kernel.run_cell(code)
            replies.write(json.dumps(reply).encode() + b"\n")


if __name__ == "__main__":
    main()
