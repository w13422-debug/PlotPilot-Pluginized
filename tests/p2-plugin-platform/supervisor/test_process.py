from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

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

    class Stdin:
        def write(self, data: bytes) -> None:
            del data

        def flush(self) -> None:
            return None

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
    monkeypatch.setattr(process_module.subprocess, "Popen", lambda *args, **kwargs: fake)
    events: list[tuple[str, bytes | int]] = []
    exited = threading.Event()
    route = SimpleNamespace(
        python_executable=tmp_path / "venv" / "Scripts" / "python.exe",
        package_root=tmp_path,
        route=SimpleNamespace(entrypoint="plugin_worker:main"),
    )

    def observe_stdout(data: bytes) -> None:
        events.append(("stdout", data))
        raise RuntimeError("observer failed after recording protocol bytes")

    IsolatedVenvProcessFactory().start(
        route,
        on_stdout=observe_stdout,
        on_stderr=lambda data: events.append(("stderr", data)),
        on_exit=lambda code: (events.append(("exit", code)), exited.set()),
    )
    assert exited.wait(1)
    assert ("stdout", b"stdout-tail") in events
    assert ("stderr", b"traceback-tail") in events
    assert events[-1] == ("exit", 7)
