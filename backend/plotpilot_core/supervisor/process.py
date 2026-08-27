"""Standard-library isolated subprocess adapter for immutable release venvs."""
from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

from .models import ManagedProcess, ResolvedWorkerRoute

_BOOTSTRAP = Path(__file__).with_name("worker_bootstrap.py").resolve()


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


class SubprocessHandle(ManagedProcess):
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process
        self._write_lock = threading.Lock()

    def write(self, data: bytes) -> None:
        if self._process.stdin is None:
            raise BrokenPipeError("worker stdin is closed")
        with self._write_lock:
            self._process.stdin.write(data)
            self._process.stdin.flush()

    def terminate(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()

    def kill(self) -> None:
        if self._process.poll() is None:
            self._process.kill()

    def poll(self) -> int | None:
        return self._process.poll()


class IsolatedVenvProcessFactory:
    """Spawn argv directly; no shell, plugin command, or ambient PYTHONPATH."""

    def start(
        self,
        route: ResolvedWorkerRoute,
        *,
        on_stdout: Callable[[bytes], None],
        on_stderr: Callable[[bytes], None],
        on_exit: Callable[[int], None],
    ) -> ManagedProcess:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            [
                str(route.python_executable),
                "-I",
                "-u",
                str(_BOOTSTRAP),
                route.route.entrypoint,
            ],
            cwd=str(route.package_root),
            env=isolated_environment(route.python_executable),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            creationflags=creationflags,
        )
        handle = SubprocessHandle(process)

        def read_stdout() -> None:
            assert process.stdout is not None
            while True:
                # ``BufferedReader.read(n)`` may wait for the full request on a
                # live pipe. ``read1`` returns currently available bytes.
                chunk = process.stdout.read1(64 * 1024)
                if not chunk:
                    return
                try:
                    on_stdout(chunk)
                except Exception:  # noqa: BLE001, S112 -- callback is an injected isolation boundary
                    # Observer failure must not stop draining the protocol pipe.
                    continue

        def read_stderr() -> None:
            assert process.stderr is not None
            while True:
                chunk = process.stderr.read1(16 * 1024)
                if not chunk:
                    return
                try:
                    on_stderr(chunk)
                except Exception:  # noqa: BLE001, S112 -- keep draining after observer failure
                    continue

        stdout_thread = threading.Thread(target=read_stdout, name=f"plugin-stdout-{process.pid}", daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, name=f"plugin-stderr-{process.pid}", daemon=True)

        def wait_for_drained_exit() -> None:
            # A process exit is observable only after both pipes reached EOF.
            # This makes stderr crash capture and RPC EOF finalization one
            # ordered event instead of three racing callbacks.
            try:
                process.wait()
            finally:
                stdout_thread.join()
                stderr_thread.join()
                on_exit(process.returncode if process.returncode is not None else -1)

        stdout_thread.start()
        stderr_thread.start()
        threading.Thread(target=wait_for_drained_exit, name=f"plugin-exit-{process.pid}", daemon=True).start()
        return handle


__all__ = ["IsolatedVenvProcessFactory", "SubprocessHandle", "isolated_environment"]
