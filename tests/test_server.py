"""Behaviour of the server beyond what the conformance checker covers."""

import base64
import concurrent.futures
import io
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import sandbox_image_conformance
from conftest import Running
from server import MARKER_FILENAME, PROTOCOL, Config, Sandbox, storage_semantics

CONFORMANCE_DIR = Path(__file__).resolve().parent.parent / "conformance"


def test_passes_the_seizu_conformance_checker(running: Running) -> None:
    out = io.StringIO()
    passed = sandbox_image_conformance.run(
        running.url,
        home=str(running.config.home),
        workspace=os.path.realpath(running.config.workspace),
        out=out,
    )
    assert passed, out.getvalue()


def test_implements_exactly_the_vendored_protocol() -> None:
    schema = json.loads((CONFORMANCE_DIR / "sandbox-protocol-v1.json").read_text())
    defaults = Config()
    assert set(schema["x-seizu-ops"]) == set(Sandbox(defaults).handlers())
    assert schema["title"] == PROTOCOL
    assert schema["x-seizu-marker-file"] == MARKER_FILENAME
    assert schema["x-seizu-home"] == str(defaults.home)
    assert schema["x-seizu-workspace-mount"] == str(defaults.workspace)


# -- run_python -------------------------------------------------------------------------


def test_python_captures_subprocess_and_c_level_output(running: Running) -> None:
    out = running.result("run_python", {"code": "import os\nos.system('echo from-child')\nos.write(1, b'raw\\n')"})
    assert "from-child\n" in out["stdout"]
    assert "raw\n" in out["stdout"]


def test_python_output_is_bounded(start: Callable[..., Running]) -> None:
    running = start(max_output_bytes=1000)
    out = running.result("run_python", {"code": "print('x' * 50_000)\n'y' * 50_000"})
    assert len(out["stdout"]) < 1100 and "more bytes not shown" in out["stdout"]
    assert len(out["text"]) < 1100 and "more characters not shown" in out["text"]


def test_python_top_level_await(running: Running) -> None:
    out = running.result("run_python", {"code": "import asyncio\nawait asyncio.sleep(0)\n40 + 2"})
    assert out["text"] == "42", out


def test_python_traceback_starts_in_the_cell(running: Running) -> None:
    out = running.result("run_python", {"code": "def f():\n    raise ValueError('boom')\nf()"})
    assert out["error"]["name"] == "ValueError" and out["error"]["value"] == "boom"
    assert "kernel.py" not in out["error"]["traceback"]
    assert "raise ValueError('boom')" in out["error"]["traceback"]


def test_python_syntax_error_is_a_result(running: Running) -> None:
    out = running.result("run_python", {"code": "def ("})
    assert out["error"]["name"] == "SyntaxError"


def test_python_replaced_stdout_is_restored(running: Running) -> None:
    running.result("run_python", {"code": "import io, sys\nsys.stdout = io.StringIO()"})
    assert running.result("run_python", {"code": "print('visible')"})["stdout"] == "visible\n"


def test_interpreter_exit_restarts_it(running: Running) -> None:
    running.result("run_python", {"code": "kept = 1"})
    out = running.result("run_python", {"code": "import os\nos._exit(3)"})
    assert out["restarted"] is True and out["error"]["name"] == "KernelDied"
    assert running.result("run_python", {"code": "kept"})["error"]["name"] == "NameError"


def test_python_calls_are_serialized(running: Running) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(running.call, "run_python", {"code": "import time\ntime.sleep(3)"}, timeout_seconds=20)
        time.sleep(0.5)
        blocked = running.call("run_python", {"code": "1"}, timeout_seconds=1)
        assert slow.result()["ok"] is True
    assert blocked == {"ok": False, "error": {"code": "timeout", "message": blocked["error"]["message"]}}


def test_python_runs_in_home(running: Running) -> None:
    out = running.result("run_python", {"code": "import os\nos.getcwd()"})
    assert out["text"] == repr(os.path.realpath(running.config.home))


# -- run_bash ---------------------------------------------------------------------------------


def test_bash_background_job_does_not_hold_the_call(running: Running) -> None:
    started = time.monotonic()
    out = running.result("run_bash", {"command": "sleep 30 & echo started"})
    assert out["stdout"] == "started\n" and out["exit_code"] == 0
    assert time.monotonic() - started < 10


def test_bash_timeout_kills_the_whole_group(running: Running, tmp_path: Path) -> None:
    flag = tmp_path / "survived"
    out = running.result("run_bash", {"command": f"(sleep 3; touch {flag}) & sleep 30"}, timeout_seconds=1)
    assert out["timed_out"] is True
    time.sleep(4)
    assert not flag.exists()


def test_bash_output_is_bounded(start: Callable[..., Running]) -> None:
    running = start(max_output_bytes=1000)
    out = running.result("run_bash", {"command": "head -c 100000 /dev/zero | tr '\\0' x"})
    assert len(out["stdout"]) < 1100 and "more bytes not shown" in out["stdout"]


def test_bash_missing_cwd_is_not_found(running: Running) -> None:
    response = running.call("run_bash", {"command": "true", "cwd": "/does/not/exist"})
    assert response["error"]["code"] == "not_found"


def test_bash_sees_home_and_not_server_settings(running: Running, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEIZU_SANDBOX_PORT", "1234")
    out = running.result("run_bash", {"command": 'echo "$HOME"; env | grep -c ^SEIZU_SANDBOX_ || true'})
    assert out["stdout"].splitlines() == [str(running.config.home), "0"]


# -- files ------------------------------------------------------------------------------------


def test_relative_paths_are_under_home(running: Running) -> None:
    running.result("write_file", {"path": "rel/a.txt", "content_b64": base64.b64encode(b"a").decode()})
    assert (running.config.workspace / "rel" / "a.txt").read_bytes() == b"a"


def test_oversized_read_is_refused(start: Callable[..., Running]) -> None:
    running = start(max_file_bytes=10)
    running.result("write_file", {"path": "big", "content_b64": base64.b64encode(b"x" * 11).decode()})
    assert running.call("read_file", {"path": "big"})["error"]["code"] == "invalid_args"


def test_corrupt_marker_reads_as_none(running: Running) -> None:
    (running.config.workspace / MARKER_FILENAME).write_text("{not json")
    assert running.result("hello")["marker"] is None


def test_wipe_does_not_follow_symlinks(running: Running, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    (running.config.workspace / "link").symlink_to(outside)
    assert running.result("wipe")["removed"] == 1
    assert (outside / "keep").exists()


# -- envelope ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"protocol": PROTOCOL, "op": "run_python", "args": {}}, "invalid_args"),
        ({"protocol": PROTOCOL, "op": "run_python", "args": {"code": 1}}, "invalid_args"),
        ({"protocol": PROTOCOL, "op": "write_file", "args": {"path": "a", "content_b64": "!!"}}, "invalid_args"),
        ({"protocol": PROTOCOL, "op": "hello", "timeout_seconds": 0}, "invalid_args"),
        ({"protocol": PROTOCOL, "op": "hello", "timeout_seconds": True}, "invalid_args"),
        ({"protocol": PROTOCOL, "op": "hello", "args": []}, "invalid_args"),
        ({"protocol": PROTOCOL, "op": "hello", "args": {"create_marker": {"deployment": 1}}}, "invalid_args"),
        (["not", "an", "object"], "invalid_args"),
    ],
)
def test_malformed_requests(running: Running, body: Any, code: str) -> None:
    assert running.raw(body)["error"]["code"] == code


def test_unknown_request_fields_are_ignored(running: Running) -> None:
    response = running.raw({"protocol": PROTOCOL, "op": "hello", "timeout_seconds": 5, "future": {"x": 1}})
    assert response["ok"] is True


def test_non_json_body(running: Running) -> None:
    assert running.raw(b"{nope")["error"]["code"] == "invalid_args"


def test_oversized_request(start: Callable[..., Running]) -> None:
    running = start(max_request_bytes=100)
    body = {"protocol": PROTOCOL, "op": "run_python", "args": {"code": "#" * 200}, "timeout_seconds": 5}
    assert running.raw(body)["error"]["code"] == "invalid_args"


def test_logs_never_carry_arguments_or_output(running: Running, caplog: pytest.LogCaptureFixture) -> None:
    sentinel = "s3ntinel-value-in-code"
    caplog.set_level("DEBUG")
    running.result("run_python", {"code": f"print({sentinel!r})"})
    running.result("write_file", {"path": "f", "content_b64": base64.b64encode(sentinel.encode()).decode()})
    running.call("no_such_op", {"x": sentinel})
    assert "op=run_python outcome=ok" in caplog.text
    assert sentinel not in caplog.text


def test_ping_timestamp_moves_only_with_the_status(running: Running) -> None:
    first = running.ping()
    time.sleep(1.1)
    assert running.ping() == first
    running.result("run_bash", {"command": "true"})
    assert running.ping()["status"] == "Healthy"


# -- storage semantics ---------------------------------------------------------------------------


def _mountinfo(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "mountinfo"
    path.write_text("".join(f"{line}\n" for line in lines))
    return path


ROOT = "22 1 0:21 / / rw,relatime - overlay overlay rw"


@pytest.mark.parametrize(
    ("mount", "expected"),
    [
        ("40 22 0:50 / {ws} rw,nosuid - fuse.mountpoint-s3 mountpoint-s3 rw,user_id=1000", "object"),
        ("40 22 0:50 / {ws} rw,nosuid - fuse mountpoint-s3 rw,user_id=1000", "object"),
        ("40 22 253:1 / {ws} rw,relatime - ext4 /dev/vdb rw", "posix"),
        ("40 22 0:50 / {parent} rw - fuse.mountpoint-s3 mountpoint-s3 rw", "object"),
        ("40 22 0:50 / {sibling} rw - fuse.mountpoint-s3 mountpoint-s3 rw", "posix"),
    ],
)
def test_storage_semantics_follows_the_workspace_mount(tmp_path: Path, mount: str, expected: str) -> None:
    ws = tmp_path / "mnt" / "workspace"
    ws.mkdir(parents=True)
    real = os.path.realpath(ws)
    line = mount.format(ws=real, parent=os.path.dirname(real), sibling=real + "2")
    config = Config(workspace=ws, mountinfo=_mountinfo(tmp_path, ROOT, line))
    assert storage_semantics(config) == expected


def test_a_nested_posix_mount_wins_over_an_outer_s3_one(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    real = os.path.realpath(ws)
    mounts = _mountinfo(
        tmp_path,
        ROOT,
        f"40 22 0:50 / {os.path.dirname(real)} rw - fuse.mountpoint-s3 mountpoint-s3 rw",
        f"41 40 253:1 / {real} rw - ext4 /dev/vdb rw",
    )
    assert storage_semantics(Config(workspace=ws, mountinfo=mounts)) == "posix"


def test_storage_semantics_override_and_unreadable_mountinfo(tmp_path: Path) -> None:
    assert storage_semantics(Config(storage_semantics="object", mountinfo=tmp_path / "missing")) == "object"
    assert storage_semantics(Config(mountinfo=tmp_path / "missing")) == "posix"


def test_hello_reports_detected_semantics(start: Callable[..., Running], tmp_path: Path) -> None:
    running = start()
    real = os.path.realpath(running.config.workspace)
    running.config.mountinfo.write_text(f"{ROOT}\n40 22 0:50 / {real} rw - fuse.mountpoint-s3 mountpoint-s3 rw\n")
    assert running.result("hello")["storage_semantics"] == "object"
