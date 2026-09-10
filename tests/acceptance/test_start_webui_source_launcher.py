from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CMD_LAUNCHER = ROOT / "start-webui.cmd"
PS_LAUNCHER = ROOT / "start-webui.ps1"


def _cmd_source() -> str:
    return CMD_LAUNCHER.read_text(encoding="utf-8-sig")


def _ps_source() -> str:
    return PS_LAUNCHER.read_text(encoding="utf-8-sig")


def _port_is_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def test_cmd_is_a_thin_fixed_mode_wrapper() -> None:
    source = _cmd_source()
    lowered = source.lower()

    assert len(source.splitlines()) < 35
    assert 'powershell.exe -noprofile -executionpolicy bypass -file "%~dp0start-webui.ps1"' in lowered
    assert '-mode "%plotpilot_mode%"' in lowered
    assert "--check" in lowered
    assert "--self-test-owned-cleanup" in lowered
    assert "--self-test-owned-cleanup-mismatch" in lowered
    assert "--self-test-owned-cleanup-exited" in lowered
    assert "--self-test-owned-capture-failure" in lowered
    assert "pause" in lowered
    assert "uvicorn" not in lowered
    assert "vite" not in lowered
    assert "win32_process" not in lowered


def test_source_contract_is_browser_only_and_uses_hidden_local_services() -> None:
    source = _ps_source()
    lowered = source.lower()
    launcher_source = source[source.index("function Invoke-Launcher {") :]

    assert '"interfaces.main:app"' in source
    assert '"--host", "127.0.0.1"' in source
    assert '"--port", "$backendPort"' in source
    assert '"--workers", "1"' in source
    assert launcher_source.count('"--workers"') == 1
    assert "--reload" not in lowered
    assert "pnpm.cmd exec vite --host 127.0.0.1 --port 3000 --strictport" in lowered
    assert 'start-process -filepath $browserurl' in lowered
    assert '-WindowStyle Hidden' in source
    assert 'WindowStyle            = "Hidden"' in source

    assert "PLOTPILOT_PROD_DATA_DIR" in source
    assert 'Join-Path $env:LOCALAPPDATA "PlotPilot\\data"' in source
    assert 'DISABLE_AUTO_DAEMON     = "1"' in source
    assert "VECTOR_STORE_ENABLED" in source
    assert 'PYTHONPATH              = $sourcePythonPath' in source
    assert 'Join-Path $root "backend"' in source
    assert "source SDK + migrations verified" in source

    forbidden = (
        "ta" + "uri",
        "t" + "k",
        "py" + "installer",
        "task" + "kill",
        "stop" + "-" + "process",
    )
    assert not any(token in lowered for token in forbidden)


def test_launcher_classifies_both_ports_before_building_a_plan_or_creating_data() -> None:
    source = _ps_source()

    assert 'function Get-PortServiceState' in source
    assert 'function Get-LauncherPlan' in source
    assert '"Available", "Reusable", "Occupied"' in source
    assert 'State     = "Reusable"' in source
    assert 'State     = "Occupied"' in source
    assert 'Test-ProcessSourceAssociation' in source
    assert 'Test-FrontendHttpContract' in source
    assert 'Test-BackendHttpContract' in source
    assert source.index('$backendState = Get-PortServiceState') < source.index(
        'CreateDirectory($dataDirectory)'
    )
    assert source.index('$frontendState = Get-PortServiceState') < source.index(
        'CreateDirectory($dataDirectory)'
    )
    assert source.index('Get-LauncherPlan') < source.index(
        'CreateDirectory($dataDirectory)'
    )


def test_reuse_requires_process_source_and_role_specific_http_contracts() -> None:
    source = _ps_source()
    lowered = source.lower()

    assert 'Get-ProcessSourceChain -ProcessId $ownerId' in source
    assert 'RequiredPath $FrontendDirectory' in source
    assert 'RequiredPath $Root' in source
    assert 'RequiredTokens @("vite", "--host", "127.0.0.1", "--port", "3000", "--strictport")' in source
    assert 'RequiredTokens @("uvicorn", "interfaces.main:app", "--app-dir", "--port", "8005", "--workers", "1")' in source
    assert '-RequiredPathArgument "--app-dir"' in source
    assert 'foreach ($record in @($ProcessRecords))' in source
    assert 'combined path and tokens from different process records' in source
    assert 'src="/src/main.ts"' in source
    assert 'id="app"' in source
    assert 'id="boot-splash"' in source
    assert 'status -eq "healthy"' in source
    assert 'build_id' in source
    assert 'StatusCode -ge 200' in source
    assert 'StatusCode -lt 300' in source
    assert 'title-only' in lowered
    assert 'StartBackend  = ($LauncherMode -eq "launch" -and $BackendState -eq "Available")' in source
    assert 'StartFrontend = ($LauncherMode -eq "launch" -and $FrontendState -eq "Available")' in source


def test_backend_app_dir_is_quoted_and_frontend_uses_supported_vite_arguments() -> None:
    source = _ps_source()

    assert '$backendAppDirectoryArgument = ConvertTo-QuotedWindowsArgument -Value $root' in source
    assert '"--app-dir", $backendAppDirectoryArgument' in source
    assert '"interfaces.main:app"' in source
    assert '"--port", "$backendPort"' in source
    assert '"pnpm.cmd exec vite --host 127.0.0.1 --port 3000 --strictPort"' in source
    assert '--root' not in source.lower()
    assert 'Reusable' in source
    assert '$owned' in source
    assert 'State     = "Reusable"' in source


def test_guard_retains_exact_identity_before_releasing_unique_sentinel() -> None:
    source = _ps_source()
    start = source.index("function Start-OwnedGuard")
    end = source.index("function Wait-ExactIdentityAbsent")
    guard_source = source[start:end]

    assert '"PlotPilot-{0}-{1}.gate"' in guard_source
    assert "Start-Process" in guard_source
    assert "-PassThru" in guard_source
    assert "Get-ProcessIdentity -ProcessId $guard.Id" in guard_source
    assert "OwnedIdentities.Add($guardIdentity)" in guard_source
    assert guard_source.index("OwnedIdentities.Add($guardIdentity)") < guard_source.index(
        "$gatePath,"
    )
    assert "Wait-IdentityRecord" in guard_source
    assert "OwnedIdentities.Add($childIdentity)" in guard_source
    assert "The sentinel is still closed" in guard_source
    assert "$guard.Kill()" in guard_source
    assert "ChildIdentityCreated" in guard_source


def test_cleanup_uses_one_immutable_snapshot_and_never_substitutes_a_reused_pid() -> None:
    source = _ps_source()
    start = source.index("function Stop-ExactOwnedProcessTrees")
    end = source.index("function ConvertTo-GuardPayload")
    cleanup = source[start:end]

    assert cleanup.count("Get-ImmutableProcessSnapshot") == 1
    assert "snapshotNode.StartTicks" in cleanup
    assert "expected.StartTicks" in cleanup
    assert "A reused PID is never substituted" in cleanup
    assert "Revalidate every immutable snapshot node immediately before termination" in cleanup
    assert "Get-ProcessIdentity -ProcessId ([int] $node.Id)" in cleanup
    assert "Invoke-CimMethod -InputObject $current.Raw -MethodName Terminate" in cleanup
    assert "Get-NetTCPConnection" not in cleanup
    assert ".Kill()" not in cleanup


def test_self_tests_cover_real_tree_pid_reuse_exited_root_and_capture_failure() -> None:
    source = _ps_source()
    start = source.index("function Invoke-OwnedCleanupSelfTest")
    end = source.index("function Invoke-LauncherPlanSelfTest")
    self_test = source[start:end]

    assert "Start-OwnedGuard" in self_test
    assert "Start-Sleep -Seconds 120" in self_test
    assert "StartTicks = [Int64] $started.RootIdentity.StartTicks + 1" in self_test
    assert "mismatch identity preserved" in self_test
    assert "ExitAfterChildStart" in self_test
    assert "exited root left a retained child" in self_test
    assert "InjectCaptureFailure" in self_test
    assert "capture failure kept the sentinel closed" in self_test
    assert "uvicorn" not in self_test.lower()
    assert "vite" not in self_test.lower()
    assert "http://" not in self_test.lower()


@pytest.mark.skipif(os.name != "nt", reason="PowerShell plan self-test requires Windows")
def test_controlled_plan_self_test_covers_reuse_and_adversarial_http_cases() -> None:
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PS_LAUNCHER),
            "-Mode",
            "self-test-plan",
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "service classification and pure launcher plans passed" in output


def test_check_branch_precedes_data_creation_and_owned_service_launches() -> None:
    source = _ps_source()
    check_branch = 'if ($LauncherMode -eq "check")'
    assert check_branch in source
    assert source.index(check_branch) < source.index("CreateDirectory($dataDirectory)")
    assert source.index(check_branch) < source.index('Write-Host ("[start] Backend')
    assert "without creating data, starting services, or opening a browser" in source


def test_launcher_files_have_the_required_encodings_and_no_trailing_whitespace() -> None:
    cmd_raw = CMD_LAUNCHER.read_bytes()
    assert cmd_raw.startswith(b"\xef\xbb\xbf")
    assert b"\n" not in cmd_raw.replace(b"\r\n", b"")
    assert all(not line.endswith((b" ", b"\t")) for line in cmd_raw.split(b"\r\n"))

    ps_raw = PS_LAUNCHER.read_bytes()
    assert not ps_raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in ps_raw
    assert ps_raw.endswith(b"\n")
    assert all(not line.endswith((b" ", b"\t")) for line in ps_raw.split(b"\n"))


@pytest.mark.skipif(os.name != "nt", reason="cmd launcher acceptance requires Windows")
def test_check_mode_is_side_effect_free_and_keeps_ports_unchanged(tmp_path: Path) -> None:
    ports = (8005, 3000)
    before = {port: _port_is_listening(port) for port in ports}
    if any(before.values()):
        pytest.skip("acceptance machine already has a service on a required port")

    data_dir = tmp_path / "check-data"
    env = os.environ.copy()
    env["PLOTPILOT_PROD_DATA_DIR"] = str(data_dir)

    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "start-webui.cmd", "--check"],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )

    after = {port: _port_is_listening(port) for port in ports}
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "--check" in output
    assert "8005" in output and "3000" in output
    assert before == after == {8005: False, 3000: False}
    assert not data_dir.exists(), "--check must not create the runtime data directory"


@pytest.mark.skipif(os.name != "nt", reason="cmd launcher acceptance requires Windows")
@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("--self-test-owned-cleanup", "exact owned guard and child tree exited"),
        ("--self-test-owned-cleanup-mismatch", "mismatch identity preserved"),
        ("--self-test-owned-cleanup-exited", "exited root left a retained child"),
        ("--self-test-owned-capture-failure", "capture failure kept the sentinel closed"),
    ],
)
def test_owned_process_rollback_self_tests_never_start_a_service(
    tmp_path: Path, argument: str, expected: str
) -> None:
    data_dir = tmp_path / "must-not-exist"
    env = os.environ.copy()
    env["PLOTPILOT_PROD_DATA_DIR"] = str(data_dir)
    before = {port: _port_is_listening(port) for port in (8005, 3000)}

    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "start-webui.cmd", argument],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "owned cleanup self-test simulated a later startup failure" in output
    assert expected in output
    assert before == {port: _port_is_listening(port) for port in (8005, 3000)}
    assert not data_dir.exists(), "self-tests must not create the runtime data directory"


def test_failure_path_preserves_logs_and_routes_only_to_exact_owned_cleanup() -> None:
    source = _ps_source()
    assert "Stop-ExactOwnedProcessTrees -ExpectedIdentities $owned.ToArray()" in source
    assert "See stdout $StandardOutput and stderr $StandardError" in source
    assert "Get-NetTCPConnection" in source
    launcher_catch = source[source.index("function Invoke-Launcher") :]
    cleanup = launcher_catch.index("$cleanupSucceeded = Stop-ExactOwnedProcessTrees")
    report = launcher_catch.index('[Console]::Error.WriteLine("[failed]')
    assert cleanup < report, "cleanup must run before reporting under ErrorActionPreference=Stop"
    assert "Write-Error" not in source
