"""Isolated subprocess adapter with bounded writes and Windows tree containment."""
from __future__ import annotations

import ctypes
import math
import os
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from .models import ManagedProcess, ResolvedWorkerRoute

_BOOTSTRAP = Path(__file__).with_name("worker_bootstrap.py").resolve()
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_CREATE_SUSPENDED = 0x00000004


def isolated_environment(
    python_executable: Path,
    *,
    ambient: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a small deterministic environment without caller Python paths."""

    source = os.environ if ambient is None else ambient
    result: dict[str, str] = {}
    for key in ("SystemRoot", "WINDIR", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA"):
        value = source.get(key)
        if value:
            result[key] = value
    scripts = str(python_executable.parent)
    system_root = result.get("SystemRoot") or result.get("WINDIR")
    result["PATH"] = scripts + (os.pathsep + str(Path(system_root) / "System32") if system_root else "")
    result.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return result


class JobTree(Protocol):
    def assign_and_resume(self, process: subprocess.Popen[bytes]) -> None: ...
    def terminate(self, exit_code: int) -> None: ...
    def close(self) -> None: ...


class _WindowsJob:
    """One KILL_ON_JOB_CLOSE Job Object for one suspended worker process."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Job Objects are available only on Windows")
        from ctypes import wintypes

        ulong_ptr = ctypes.c_size_t

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ulong_ptr),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        self._ntdll.NtResumeProcess.restype = ctypes.c_long
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle: Any | None = handle
        info = ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign_and_resume(self, process: subprocess.Popen[bytes]) -> None:
        from ctypes import wintypes

        if self._handle is None or not hasattr(process, "_handle"):
            raise OSError("suspended process handle is unavailable")
        process_handle = wintypes.HANDLE(int(process._handle))
        if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
            raise ctypes.WinError(ctypes.get_last_error())
        status = self._ntdll.NtResumeProcess(process_handle)
        if status != 0:
            raise OSError(f"NtResumeProcess failed with NTSTATUS 0x{status & 0xFFFFFFFF:08x}")

    def terminate(self, exit_code: int) -> None:
        if self._handle is not None and not self._kernel32.TerminateJobObject(self._handle, exit_code):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            self._kernel32.CloseHandle(handle)


class SubprocessHandle(ManagedProcess):
    """Managed process whose write method only enqueues bounded work."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        job: JobTree | None,
        on_transport_error: Callable[[str], None],
        write_queue_frames: int,
        write_queue_bytes: int,
        write_timeout: float,
    ) -> None:
        self._process = process
        self._job = job
        self._on_transport_error = on_transport_error
        self._write_queue_frames = write_queue_frames
        self._write_queue_bytes = write_queue_bytes
        self._write_timeout = write_timeout
        self._condition = threading.Condition()
        self._writes: deque[tuple[bytes, Callable[[], None] | None]] = deque()
        self._queued_bytes = 0
        self._writer_closed = False
        self._transport_failed = False
        self._write_started_at: float | None = None
        self._writer = threading.Thread(target=self._write_loop, name=f"plugin-writer-{process.pid}", daemon=True)
        self._writer.start()
        self._writer_watchdog = threading.Thread(
            target=self._watch_write_deadline,
            name=f"plugin-writer-watchdog-{process.pid}",
            daemon=True,
        )
        self._writer_watchdog.start()

    def write(self, data: bytes, *, on_written: Callable[[], None] | None = None) -> None:
        frame = bytes(data)
        with self._condition:
            if self._writer_closed:
                raise BrokenPipeError("worker writer is closed")
            if (
                len(self._writes) >= self._write_queue_frames
                or len(frame) > self._write_queue_bytes
                or self._queued_bytes + len(frame) > self._write_queue_bytes
            ):
                raise BufferError("worker writer queue capacity exceeded")
            self._writes.append((frame, on_written))
            self._queued_bytes += len(frame)
            self._condition.notify()

    def _write_loop(self) -> None:
        while True:
            with self._condition:
                while not self._writes and not self._writer_closed:
                    self._condition.wait()
                if self._writer_closed:
                    return
                frame, on_written = self._writes[0]
                self._write_started_at = time.monotonic()
                self._condition.notify_all()
            try:
                if self._process.stdin is None:
                    raise BrokenPipeError("worker stdin is closed")
                self._process.stdin.write(frame)
                self._process.stdin.flush()
            except Exception as exc:  # noqa: BLE001
                with self._condition:
                    self._writer_closed = True
                    self._writes.clear()
                    self._queued_bytes = 0
                    self._write_started_at = None
                    notify = not self._transport_failed
                    self._transport_failed = True
                    self._condition.notify_all()
                if notify:
                    self._on_transport_error(f"worker stdin write failed: {exc}")
                return
            with self._condition:
                delivered = not self._writer_closed and not self._transport_failed
                if delivered and self._writes and self._writes[0][0] == frame:
                    self._writes.popleft()
                    self._queued_bytes -= len(frame)
                self._write_started_at = None
                self._condition.notify_all()
            if delivered and on_written is not None:
                try:
                    on_written()
                except Exception:  # noqa: BLE001, S110 -- delivery observer cannot break the writer
                    pass

    def _watch_write_deadline(self) -> None:
        while True:
            with self._condition:
                if self._writer_closed:
                    return
                started = self._write_started_at
                if started is None:
                    self._condition.wait()
                    continue
                remaining = self._write_timeout - (time.monotonic() - started)
                if remaining > 0:
                    self._condition.wait(remaining)
                    continue
                self._writer_closed = True
                self._writes.clear()
                self._queued_bytes = 0
                self._write_started_at = None
                notify = not self._transport_failed
                self._transport_failed = True
                self._condition.notify_all()
            if notify:
                self._on_transport_error("worker stdin write deadline exceeded")
            return

    def _close_input(self) -> None:
        with self._condition:
            self._writer_closed = True
            self._writes.clear()
            self._queued_bytes = 0
            self._write_started_at = None
            self._condition.notify_all()
        stream = self._process.stdin
        if stream is not None:
            try:
                os.close(stream.fileno())
            except OSError:
                pass

    def terminate(self) -> None:
        self._close_input()
        if self._process.poll() is None:
            if self._job is not None:
                self._job.terminate(1)
            else:
                self._process.terminate()

    def kill(self) -> None:
        self._close_input()
        if self._process.poll() is None:
            if self._job is not None:
                self._job.terminate(9)
            else:
                self._process.kill()

    def poll(self) -> int | None:
        return self._process.poll()

    def parent_exited(self) -> None:
        self._close_input()
        if self._job is not None:
            self._job.close()


class IsolatedVenvProcessFactory:
    """Spawn a contained worker and expose only bounded non-blocking writes."""

    def __init__(
        self,
        *,
        write_queue_frames: int = 64,
        write_queue_bytes: int = 16 * 1024 * 1024,
        write_timeout: float = 5.0,
        drain_timeout: float = 2.0,
        job_factory: Callable[[], JobTree] | None = None,
    ) -> None:
        if (
            not isinstance(write_queue_frames, int)
            or isinstance(write_queue_frames, bool)
            or write_queue_frames < 1
            or not isinstance(write_queue_bytes, int)
            or isinstance(write_queue_bytes, bool)
            or write_queue_bytes < 1
            or not isinstance(write_timeout, (int, float))
            or isinstance(write_timeout, bool)
            or not math.isfinite(write_timeout)
            or write_timeout <= 0
            or not isinstance(drain_timeout, (int, float))
            or isinstance(drain_timeout, bool)
            or not math.isfinite(drain_timeout)
            or drain_timeout <= 0
        ):
            raise ValueError("process queue and drain limits must be positive")
        self._write_queue_frames = write_queue_frames
        self._write_queue_bytes = write_queue_bytes
        self._write_timeout = write_timeout
        self._drain_timeout = drain_timeout
        self._job_factory = job_factory or _WindowsJob

    @staticmethod
    def _close_pipe(pipe: Any | None) -> None:
        if pipe is None:
            return
        try:
            os.close(pipe.fileno())
        except OSError:
            pass

    def start(
        self,
        route: ResolvedWorkerRoute,
        *,
        on_stdout: Callable[[bytes], None],
        on_stderr: Callable[[bytes], None],
        on_exit: Callable[[int], None],
        on_transport_error: Callable[[str], None],
    ) -> ManagedProcess:
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW | _CREATE_SUSPENDED
        process = subprocess.Popen(
            [
                str(route.python_executable),
                "-I",
                "-u",
                str(_BOOTSTRAP),
                route.route.entrypoint,
            ],
            cwd=str(route.working_root),
            env=isolated_environment(route.python_executable),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        job: JobTree | None = None
        try:
            if os.name == "nt":
                job = self._job_factory()
                job.assign_and_resume(process)
            handle = SubprocessHandle(
                process,
                job=job,
                on_transport_error=on_transport_error,
                write_queue_frames=self._write_queue_frames,
                write_queue_bytes=self._write_queue_bytes,
                write_timeout=self._write_timeout,
            )
        except Exception:
            try:
                process.kill()
                process.wait(timeout=2)
            finally:
                if job is not None:
                    job.close()
                self._close_pipe(process.stdin)
                self._close_pipe(process.stdout)
                self._close_pipe(process.stderr)
            raise

        def read_stdout() -> None:
            assert process.stdout is not None
            while True:
                try:
                    chunk = process.stdout.read1(64 * 1024)
                except (OSError, ValueError):
                    return
                if not chunk:
                    return
                try:
                    on_stdout(chunk)
                except Exception:  # noqa: BLE001, S112
                    continue

        def read_stderr() -> None:
            assert process.stderr is not None
            while True:
                try:
                    chunk = process.stderr.read1(16 * 1024)
                except (OSError, ValueError):
                    return
                if not chunk:
                    return
                try:
                    on_stderr(chunk)
                except Exception:  # noqa: BLE001, S112
                    continue

        stdout_thread = threading.Thread(target=read_stdout, name=f"plugin-stdout-{process.pid}", daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, name=f"plugin-stderr-{process.pid}", daemon=True)

        def wait_for_drained_exit() -> None:
            try:
                process.wait()
            finally:
                handle.parent_exited()
                stdout_thread.join(self._drain_timeout)
                stderr_thread.join(self._drain_timeout)
                if stdout_thread.is_alive():
                    self._close_pipe(process.stdout)
                if stderr_thread.is_alive():
                    self._close_pipe(process.stderr)
                stdout_thread.join(0.1)
                stderr_thread.join(0.1)
                on_exit(process.returncode if process.returncode is not None else -1)

        stdout_thread.start()
        stderr_thread.start()
        threading.Thread(target=wait_for_drained_exit, name=f"plugin-exit-{process.pid}", daemon=True).start()
        return handle


__all__ = ["IsolatedVenvProcessFactory", "SubprocessHandle", "isolated_environment"]
