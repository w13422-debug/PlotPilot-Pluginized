from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from plotpilot_core.supervisor import process as process_module
from plotpilot_core.supervisor.process import (
    IsolatedVenvProcessFactory,
    isolated_environment,
)


def test_isolated_environment_drops_python_and_secret_ambient_values() -> None:
    executable = Path("C:/venv/Scripts/python.exe")
    env = isolated_environment(
        executable,
        ambient={
            "SystemRoot": "C:/Windows",
            "TEMP": "C:/Temp",
            "PYTHONPATH": "C:/host-code",
            "PIP_INDEX_URL": "https://secret.invalid/token",
            "PLUGIN_SECRET": "secret",
        },
    )
    assert env["PATH"].startswith("C:\\venv\\Scripts") or env["PATH"].startswith("C:/venv/Scripts")
    assert env["PYTHONNOUSERSITE"] == "1"
    assert "PYTHONPATH" not in env
    assert "PIP_INDEX_URL" not in env
    assert "PLUGIN_SECRET" not in env


def test_process_exit_callback_waits_for_stdout_and_stderr_eof(monkeypatch, tmp_path: Path) -> None:
    class Pipe:
        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = iter(chunks)

        def read1(self, size: int) -> bytes:
            del size
            return next(self.chunks, b"")

        def fileno(self) -> int:
            return -1

    class Stdin:
        def write(self, data: bytes) -> None:
            del data

        def flush(self) -> None:
            return None

        def fileno(self) -> int:
            return -1

    class Popen:
        pid = 4321
        returncode = 7
        stdin = Stdin()
        stdout = Pipe([b"stdout-tail"])
        stderr = Pipe([b"traceback-tail"])

        def wait(self) -> int:
            return self.returncode

        def poll(self) -> int:
            return self.returncode

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    fake = Popen()
    popen_kwargs = {}

    def popen(*args, **kwargs):
        del args
        popen_kwargs.update(kwargs)
        return fake

    class Job:
        def assign_and_resume(self, process) -> None:
            assert process is fake

        def terminate(self, exit_code: int) -> None:
            del exit_code

        def close(self) -> None:
            return None

    monkeypatch.setattr(process_module.subprocess, "Popen", popen)
    events: list[tuple[str, bytes | int]] = []
    exited = threading.Event()
    route = SimpleNamespace(
        python_executable=tmp_path / "venv" / "Scripts" / "python.exe",
        package_root=tmp_path,
        working_root=tmp_path / "working",
        route=SimpleNamespace(entrypoint="plugin_worker:main"),
    )

    def observe_stdout(data: bytes) -> None:
        events.append(("stdout", data))
        raise RuntimeError("observer failed after recording protocol bytes")

    IsolatedVenvProcessFactory(job_factory=Job).start(
        route,
        on_stdout=observe_stdout,
        on_stderr=lambda data: events.append(("stderr", data)),
        on_exit=lambda code: (events.append(("exit", code)), exited.set()),
        on_transport_error=lambda message: events.append(("transport_error", message.encode())),
    )
    assert exited.wait(1)
    assert ("stdout", b"stdout-tail") in events
    assert ("stderr", b"traceback-tail") in events
    assert events[-1] == ("exit", 7)
    assert popen_kwargs["cwd"] == str(route.working_root)


def test_bounded_writer_queue_rejects_n_plus_one_without_blocking_caller() -> None:
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    class Stdin:
        def write(self, data: bytes) -> None:
            assert data == b"first"
            entered.set()
            assert release.wait(1)
            completed.set()

        def flush(self) -> None:
            return None

        def fileno(self) -> int:
            return -1

    class Process:
        pid = 9001
        stdin = Stdin()
        terminated = False

        def poll(self):
            return None

        def terminate(self) -> None:
            self.terminated = True

    process = Process()
    errors: list[str] = []
    handle = process_module.SubprocessHandle(
        process,
        job=None,
        on_transport_error=errors.append,
        write_queue_frames=1,
        write_queue_bytes=1024,
        write_timeout=1,
    )
    handle.write(b"first")
    assert entered.wait(1)
    with pytest.raises(BufferError):
        handle.write(b"second")
    release.set()
    assert completed.wait(1)
    handle.terminate()
    assert process.terminated
    assert errors == []


def test_writer_calls_delivery_callback_only_after_flush_commits_frame() -> None:
    allow_flush = threading.Event()
    flushed = threading.Event()
    delivered = threading.Event()
    order: list[str] = []

    class Stdin:
        def write(self, data: bytes) -> None:
            assert data == b"terminal-ack"
            order.append("write")

        def flush(self) -> None:
            assert allow_flush.wait(1)
            order.append("flush")
            flushed.set()

        def fileno(self) -> int:
            return -1

    class Process:
        pid = 9006
        stdin = Stdin()
        terminated = False

        def poll(self):
            return None

        def terminate(self) -> None:
            self.terminated = True

    process = Process()
    handle = process_module.SubprocessHandle(
        process,
        job=None,
        on_transport_error=lambda message: pytest.fail(message),
        write_queue_frames=2,
        write_queue_bytes=1024,
        write_timeout=1,
    )
    handle.write(
        b"terminal-ack",
        on_written=lambda: (order.append("delivered"), delivered.set()),
    )
    assert not delivered.wait(0.02)
    allow_flush.set()
    assert flushed.wait(1)
    assert delivered.wait(1)
    assert order == ["write", "flush", "delivered"]
    handle.terminate()
    assert process.terminated


def test_writer_failure_reports_transport_error_exactly_once() -> None:
    reported = threading.Event()
    errors: list[str] = []

    class Stdin:
        def write(self, data: bytes) -> None:
            del data
            raise BrokenPipeError("closed")

        def flush(self) -> None:
            raise AssertionError("flush must not follow failed write")

        def fileno(self) -> int:
            return -1

    class Process:
        pid = 9002
        stdin = Stdin()

        def poll(self):
            return 1

    def on_error(message: str) -> None:
        errors.append(message)
        reported.set()

    handle = process_module.SubprocessHandle(
        Process(),
        job=None,
        on_transport_error=on_error,
        write_queue_frames=2,
        write_queue_bytes=1024,
        write_timeout=1,
    )
    handle.write(b"frame")
    assert reported.wait(1)
    with pytest.raises(BrokenPipeError):
        handle.write(b"late")
    assert len(errors) == 1
    assert "closed" in errors[0]


def test_writer_deadline_reports_transport_failure_and_unblocks_exact_process() -> None:
    entered = threading.Event()
    release = threading.Event()
    reported = threading.Event()

    class Stdin:
        def write(self, data: bytes) -> None:
            del data
            entered.set()
            assert release.wait(1)

        def flush(self) -> None:
            return None

        def fileno(self) -> int:
            return -1

    class Process:
        pid = 9005
        stdin = Stdin()
        terminated = 0

        def poll(self):
            return None

        def terminate(self) -> None:
            self.terminated += 1
            release.set()

    process = Process()
    errors: list[str] = []
    written: list[bool] = []

    def on_error(message: str) -> None:
        errors.append(message)
        process.terminate()
        reported.set()

    handle = process_module.SubprocessHandle(
        process,
        job=None,
        on_transport_error=on_error,
        write_queue_frames=2,
        write_queue_bytes=1024,
        write_timeout=0.02,
    )
    handle.write(b"blocked", on_written=lambda: written.append(True))
    assert entered.wait(1)
    assert reported.wait(1)
    assert errors == ["worker stdin write deadline exceeded"]
    assert process.terminated == 1
    assert written == []
    with pytest.raises(BrokenPipeError):
        handle.write(b"late")


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended-spawn containment")
def test_windows_job_assignment_failure_kills_child_fail_closed(monkeypatch, tmp_path: Path) -> None:
    class Pipe:
        def fileno(self) -> int:
            return -1

    class Process:
        pid = 9003
        returncode = -9
        stdin = Pipe()
        stdout = Pipe()
        stderr = Pipe()
        killed = 0
        waited = 0

        def kill(self) -> None:
            self.killed += 1

        def wait(self, timeout=None) -> int:
            assert timeout == 2
            self.waited += 1
            return self.returncode

    class FailingJob:
        closed = 0

        def assign_and_resume(self, process) -> None:
            del process
            raise OSError("assignment failed")

        def terminate(self, exit_code: int) -> None:
            del exit_code

        def close(self) -> None:
            self.closed += 1

    process = Process()
    job = FailingJob()
    monkeypatch.setattr(process_module.subprocess, "Popen", lambda *args, **kwargs: process)
    route = SimpleNamespace(
        python_executable=tmp_path / "venv" / "Scripts" / "python.exe",
        working_root=tmp_path / "working",
        route=SimpleNamespace(entrypoint="plugin_worker:main"),
    )

    with pytest.raises(OSError, match="assignment failed"):
        IsolatedVenvProcessFactory(job_factory=lambda: job).start(
            route,
            on_stdout=lambda data: None,
            on_stderr=lambda data: None,
            on_exit=lambda code: None,
            on_transport_error=lambda message: None,
        )
    assert process.killed == 1
    assert process.waited == 1
    assert job.closed == 1


def test_pipe_drain_deadline_closes_inherited_pipes_and_reports_exit(monkeypatch, tmp_path: Path) -> None:
    class BlockingPipe:
        def __init__(self) -> None:
            self.closed = threading.Event()

        def read1(self, size: int) -> bytes:
            del size
            assert self.closed.wait(1)
            raise OSError("closed by drain deadline")

        def fileno(self) -> int:
            return -1

    class Stdin:
        def fileno(self) -> int:
            return -1

    class Process:
        pid = 9004
        returncode = 17
        stdin = Stdin()
        stdout = BlockingPipe()
        stderr = BlockingPipe()

        def wait(self) -> int:
            return self.returncode

        def poll(self) -> int:
            return self.returncode

    class Job:
        closed = 0

        def assign_and_resume(self, process) -> None:
            del process

        def terminate(self, exit_code: int) -> None:
            del exit_code

        def close(self) -> None:
            self.closed += 1

    process = Process()
    job = Job()
    monkeypatch.setattr(process_module.subprocess, "Popen", lambda *args, **kwargs: process)
    factory = IsolatedVenvProcessFactory(drain_timeout=0.01, job_factory=lambda: job)

    def close_pipe(pipe) -> None:
        if isinstance(pipe, BlockingPipe):
            pipe.closed.set()

    monkeypatch.setattr(factory, "_close_pipe", close_pipe)
    exited = threading.Event()
    exit_codes: list[int] = []
    route = SimpleNamespace(
        python_executable=tmp_path / "venv" / "Scripts" / "python.exe",
        working_root=tmp_path / "working",
        route=SimpleNamespace(entrypoint="plugin_worker:main"),
    )
    factory.start(
        route,
        on_stdout=lambda data: None,
        on_stderr=lambda data: None,
        on_exit=lambda code: (exit_codes.append(code), exited.set()),
        on_transport_error=lambda message: None,
    )
    assert exited.wait(1)
    assert process.stdout.closed.is_set()
    assert process.stderr.closed.is_set()
    assert exit_codes == [17]
    if os.name == "nt":
        assert job.closed == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object descendant containment")
def test_windows_job_close_terminates_real_descendant_and_unblocks_inherited_pipe() -> None:
    import ctypes
    from ctypes import wintypes

    parent_script = (
        "import subprocess,sys;"
        "child=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)'],"
        "stdout=sys.stdout,stderr=sys.stderr);"
        "print(child.pid,flush=True)"
    )
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", parent_script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW | 0x00000004,
    )
    job = process_module._WindowsJob()
    child_handle = None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        job.assign_and_resume(process)
        assert process.stdout is not None
        child_pid = int(process.stdout.readline().strip())
        assert process.wait(timeout=5) == 0
        child_handle = kernel32.OpenProcess(0x00100000, False, child_pid)
        assert child_handle
        assert kernel32.WaitForSingleObject(child_handle, 0) == 0x00000102
        job.close()
        assert kernel32.WaitForSingleObject(child_handle, 5000) == 0
        assert process.stdout.read() == b""
    finally:
        job.close()
        if child_handle:
            kernel32.CloseHandle(child_handle)
        if process.poll() is None:
            process.kill()
