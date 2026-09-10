from __future__ import annotations

import os
import socket
import subprocess
import sys
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


def test_cmd_remains_a_thin_fixed_mode_wrapper() -> None:
    source = _cmd_source().lower()

    assert len(source.splitlines()) < 35
    assert 'powershell.exe -noprofile -executionpolicy bypass -file "%~dp0start-webui.ps1"' in source
    assert '-mode "%plotpilot_mode%"' in source
    assert "uvicorn" not in source
    assert "vite" not in source


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
    assert "start-process -filepath $browserurl" in lowered
    assert "-WindowStyle Hidden" in source
    assert 'WindowStyle            = "Hidden"' in source

    assert "PLOTPILOT_PROD_DATA_DIR" in source
    assert 'Join-Path $env:LOCALAPPDATA "PlotPilot\\data"' in source
    assert 'DISABLE_AUTO_DAEMON     = "1"' in source
    assert "VECTOR_STORE_ENABLED" in source
    assert 'PYTHONPATH              = $sourcePythonPath' in source
    assert "$sourcePythonPath = $root" in launcher_source
    assert 'Join-Path $root "backend"' not in launcher_source
    assert "source SDK identity and migrations verified" in source

    forbidden = (
        "ta" + "uri",
        "t" + "k",
        "py" + "installer",
        "task" + "kill",
        "stop" + "-" + "process",
    )
    assert not any(token in lowered for token in forbidden)


def test_launcher_is_one_observe_plan_start_wait_reobserve_open_flow() -> None:
    source = _ps_source()
    launcher = source[source.index("function Invoke-Launcher {") :]

    assert source.count("Start-Process -FilePath $browserUrl") == 1
    assert "Get-ProcessSourceChain" not in source
    assert "Test-ProcessSourceAssociation" not in source
    assert "Single flow: observe both roles, plan, validate/start only missing roles, wait, then reobserve both." in source

    initial_backend = launcher.index('$backendState = Get-PortServiceState')
    initial_frontend = launcher.index('$frontendState = Get-PortServiceState')
    plan = launcher.index('$plan = Get-LauncherPlan')
    validate = launcher.index('$tools = Resolve-SourceTools')
    final_backend = launcher.index('$finalBackend = Get-PortServiceState')
    final_frontend = launcher.index('$finalFrontend = Get-PortServiceState')
    browser = launcher.index("Start-Process -FilePath $browserUrl")
    assert initial_backend < initial_frontend < plan < validate < final_backend < final_frontend < browser
    assert launcher.count('Get-PortServiceState -Role "Backend"') == 2
    assert launcher.count('Get-PortServiceState -Role "Frontend"') == 2


def test_reuse_uses_the_direct_listener_owner_with_bound_arguments() -> None:
    source = _ps_source()
    classifier = source[source.index("function Get-LoopbackListenerOwner") : source.index("function Get-LauncherPlan")]

    assert "Get-NetTCPConnection -State Listen -LocalPort $Port" in classifier
    assert "Get-ProcessIdentity -ProcessId $ownerIds[0]" in classifier
    assert "StartTicks" in classifier
    assert "Test-OptionValuePair" in classifier
    assert '"--app-dir"' in classifier
    assert '"--host"' in classifier
    assert '"--port"' in classifier
    assert '"--workers"' in classifier
    assert "Test-ViteScriptUnderFrontendRoot" in classifier
    assert '"vite.js"' in source
    assert "ParentProcessId" not in classifier


def test_http_reuse_contract_rejects_redirects_and_nonfinal_loopback_uris() -> None:
    source = _ps_source()

    assert "-MaximumRedirection 0" in source
    assert "Response.BaseResponse.ResponseUri.AbsoluteUri" in source
    assert "Test-ExactLoopbackResponse" in source
    assert "Test-RoleEvidence" in source
    assert 'id="app"' in source
    assert 'id="boot-splash"' in source
    assert 'src="/src/main.ts"' in source
    assert 'payload.status -eq "healthy"' in source
    assert "Direct owner, identity, or exact HTTP marker did not match" in source


def test_missing_role_tools_and_check_probe_are_scoped_and_bytecode_safe() -> None:
    source = _ps_source()
    launcher = source[source.index("function Invoke-Launcher {") :]

    assert 'ValidateBackend  = ($BackendState -eq "Available")' in source
    assert 'ValidateFrontend = ($FrontendState -eq "Available")' in source
    assert "if ($NeedBackend)" in source
    assert "if ($NeedFrontend)" in source
    assert "Resolve-SourceTools -NeedBackend $plan.ValidateBackend -NeedFrontend $plan.ValidateFrontend" in source
    assert "$sourcePythonPath = $root" in launcher
    assert 'Join-Path $root "backend"' not in launcher
    assert "& $Tools.Python -B -c $sdkProbe" in source
    assert 'PYTHONDONTWRITEBYTECODE = "1"' in source
    assert "without creating data, starting services, opening a browser, or writing bytecode" in source
    assert launcher.index('if ($LauncherMode -eq "check")') < launcher.index("Start-Process -FilePath $browserUrl")


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
    end = source.index("function ConvertFrom-WindowsCommandLine")
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


def test_check_branch_precedes_data_creation_and_owned_service_launches() -> None:
    source = _ps_source()
    launcher = source[source.index("function Invoke-Launcher {") :]
    check_branch = 'if ($LauncherMode -eq "check")'

    assert check_branch in launcher
    assert launcher.index(check_branch) < launcher.index("CreateDirectory($dataDirectory)")
    assert launcher.index(check_branch) < launcher.index(
        'Start-OwnedGuard -Name "backend"'
    )
    assert launcher.index(check_branch) < launcher.index(
        'Start-OwnedGuard -Name "frontend"'
    )
    assert "without creating data, starting services, opening a browser, or writing bytecode" in source


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


def test_sdk_canonical_and_top_level_imports_are_one_module_and_one_exception_class(
    tmp_path: Path,
) -> None:
    probe = """
import sys
import backend.plotpilot_plugin_sdk as backend_sdk
import plotpilot_plugin_sdk as public_sdk
from backend.plotpilot_plugin_sdk import ContractError as backend_error
from plotpilot_plugin_sdk import ContractError as public_error
from backend.plotpilot_plugin_sdk.errors import ContractError as backend_error_module
from plotpilot_plugin_sdk.errors import ContractError as public_error_module
assert public_sdk is backend_sdk
assert sys.modules['plotpilot_plugin_sdk'] is backend_sdk
assert public_error is backend_error
assert public_error_module is backend_error_module is backend_error
print('sdk identity ok')
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sdk identity ok" in result.stdout

    legacy_probe = """
import sys
from pathlib import Path
root = Path.cwd()
sys.path.insert(0, str(root / "backend"))
import plotpilot_plugin_sdk as public_sdk
import backend.plotpilot_plugin_sdk as backend_sdk
from plotpilot_plugin_sdk import ContractError as public_error
from backend.plotpilot_plugin_sdk import ContractError as backend_error
assert public_sdk is backend_sdk
assert public_error is backend_error
print('sdk top-level redirect ok')
"""
    legacy = subprocess.run(
        [sys.executable, "-B", "-c", legacy_probe],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    assert legacy.returncode == 0, legacy.stdout + legacy.stderr
    assert "sdk top-level redirect ok" in legacy.stdout

    unrelated_root = tmp_path / "unrelated"
    unrelated_backend = unrelated_root / "backend"
    unrelated_backend.mkdir(parents=True)
    (unrelated_backend / "__init__.py").write_text("", encoding="utf-8")
    unrelated_backend_probe = """
import sys
from pathlib import Path
unrelated_root = Path(sys.argv[1])
product_backend = Path(sys.argv[2])
sys.path.insert(0, str(product_backend))
sys.path.insert(0, str(unrelated_root))
assert 'backend' not in sys.modules
import plotpilot_plugin_sdk as public_sdk
from plotpilot_plugin_sdk import ContractError as public_error
from plotpilot_plugin_sdk.errors import ContractError as public_error_module
assert public_sdk.__name__ == 'plotpilot_plugin_sdk'
assert sys.modules['plotpilot_plugin_sdk'] is public_sdk
assert public_sdk.ContractError is public_error is public_error_module
assert 'backend' not in sys.modules
print('sdk unrelated backend namespace fallback ok')
"""
    unrelated_backend_result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            unrelated_backend_probe,
            str(unrelated_root),
            str(ROOT / "backend"),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    assert unrelated_backend_result.returncode == 0, (
        unrelated_backend_result.stdout + unrelated_backend_result.stderr
    )
    assert "sdk unrelated backend namespace fallback ok" in (
        unrelated_backend_result.stdout
    )

    unrelated_missing_probe = """
import importlib.abc
import sys
from pathlib import Path
root = Path.cwd()
import backend
class UnrelatedMissingFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'backend.plotpilot_plugin_sdk':
            raise ModuleNotFoundError(
                "No module named 'sdk_internal_dependency'",
                name='sdk_internal_dependency',
            )
        return None
sys.meta_path.insert(0, UnrelatedMissingFinder())
sys.path.insert(0, str(root / 'backend'))
try:
    import plotpilot_plugin_sdk
except ModuleNotFoundError as exc:
    assert exc.name == 'sdk_internal_dependency'
else:
    raise AssertionError('an unrelated backend import failure was swallowed')
print('sdk unrelated import failure preserved')
"""
    unrelated_missing = subprocess.run(
        [sys.executable, "-B", "-c", unrelated_missing_probe],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    assert unrelated_missing.returncode == 0, (
        unrelated_missing.stdout + unrelated_missing.stderr
    )
    assert "sdk unrelated import failure preserved" in unrelated_missing.stdout


@pytest.mark.skipif(os.name != "nt", reason="PowerShell launcher logic is Windows-specific")
def test_pure_proof_matrix_covers_plans_impersonation_redirect_and_owner_swap() -> None:
    escaped_path = str(PS_LAUNCHER).replace("'", "''")
    program = rf'''
$path = '{escaped_path}'
$source = [IO.File]::ReadAllText($path)
$definitions = $source.Substring(0, $source.IndexOf('if (-not [string]::IsNullOrWhiteSpace($GuardPayload))'))
. ([scriptblock]::Create($definitions))

$bothAvailable = Get-LauncherPlan -BackendState Available -FrontendState Available -LauncherMode launch
if (-not ($bothAvailable.StartBackend -and $bothAvailable.StartFrontend -and $bothAvailable.ValidateBackend -and $bothAvailable.ValidateFrontend)) {{ throw 'both Available plan failed' }}
$backendOnly = Get-LauncherPlan -BackendState Available -FrontendState Reusable -LauncherMode launch
if (-not ($backendOnly.StartBackend -and -not $backendOnly.StartFrontend -and $backendOnly.ValidateBackend -and -not $backendOnly.ValidateFrontend)) {{ throw 'frontend Reusable/backend Available plan failed' }}
$frontendOnly = Get-LauncherPlan -BackendState Reusable -FrontendState Available -LauncherMode launch
if (-not (-not $frontendOnly.StartBackend -and $frontendOnly.StartFrontend -and -not $frontendOnly.ValidateBackend -and $frontendOnly.ValidateFrontend)) {{ throw 'backend Reusable/frontend Available plan failed' }}
$bothReusable = Get-LauncherPlan -BackendState Reusable -FrontendState Reusable -LauncherMode launch
if ($bothReusable.StartBackend -or $bothReusable.StartFrontend -or $bothReusable.ValidateBackend -or $bothReusable.ValidateFrontend) {{ throw 'both Reusable plan failed' }}
$checkAvailable = Get-LauncherPlan -BackendState Available -FrontendState Reusable -LauncherMode check
if ($checkAvailable.StartBackend -or $checkAvailable.StartFrontend -or -not $checkAvailable.ValidateBackend -or $checkAvailable.ValidateFrontend) {{ throw 'check plan failed' }}
$occupiedRejected = $false
try {{ [void] (Get-LauncherPlan -BackendState Occupied -FrontendState Available -LauncherMode launch) }} catch {{ $occupiedRejected = $true }}
if (-not $occupiedRejected) {{ throw 'foreign owner was accepted' }}

$front = [pscustomobject]@{{
    StatusCode = 200
    Content = '<title>PlotPilot</title><div id="app"></div><div id="boot-splash"></div><script src="/src/main.ts"></script>'
    BaseResponse = [pscustomobject]@{{ ResponseUri = [Uri] 'http://127.0.0.1:3000/' }}
}}
if (-not (Test-ExactLoopbackResponse -Response $front -ExpectedUri 'http://127.0.0.1:3000/' -Validator {{ param($response) Test-FrontendHttpContract -Response $response }})) {{ throw 'exact frontend marker rejected' }}
$front.BaseResponse.ResponseUri = [Uri] 'http://127.0.0.1:3000/redirect'
if (Test-ExactLoopbackResponse -Response $front -ExpectedUri 'http://127.0.0.1:3000/' -Validator {{ param($response) Test-FrontendHttpContract -Response $response }}) {{ throw 'redirect accepted' }}
if (Test-RoleEvidence -OwnerMatches $true -HttpMatches $false) {{ throw 'path-only impersonation accepted' }}
if (Test-RoleEvidence -OwnerMatches $false -HttpMatches $true) {{ throw 'HTTP-only impersonation accepted' }}
if (Test-OptionValuePair -Tokens @('--host', 'wrong', '127.0.0.1', '--port', '3000') -Option '--host' -ExpectedValue '127.0.0.1') {{ throw 'wrong option/value adjacency accepted' }}
$initial = [pscustomobject]@{{ Role = 'Frontend'; State = 'Reusable'; Owner = [pscustomobject]@{{ Id = 7; StartTicks = 11 }} }}
$swapped = [pscustomobject]@{{ Role = 'Frontend'; State = 'Reusable'; Owner = [pscustomobject]@{{ Id = 7; StartTicks = 12 }} }}
$swapRejected = $false
try {{ Assert-FinalServiceState -Initial $initial -Final $swapped -Started $false }} catch {{ $swapRejected = $true }}
if (-not $swapRejected) {{ throw 'owner swap accepted' }}
'proof matrix ok'
'''
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", program],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "proof matrix ok" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="PowerShell launcher logic is Windows-specific")
def test_port_service_state_combines_direct_owner_argv_http_and_creation_ticks() -> None:
    escaped_path = str(PS_LAUNCHER).replace("'", "''")
    escaped_root = str(ROOT).replace("'", "''")
    escaped_frontend = str(ROOT / "frontend").replace("'", "''")
    program = rf'''
$path = '{escaped_path}'
$root = '{escaped_root}'
$frontend = '{escaped_frontend}'
$source = [IO.File]::ReadAllText($path)
$definitions = $source.Substring(0, $source.IndexOf('if (-not [string]::IsNullOrWhiteSpace($GuardPayload))'))
. ([scriptblock]::Create($definitions))

$script:MockCommandLine = ''
$script:MockHttpMatches = $false
$script:MockTicks = @([Int64] 101, [Int64] 101)
$script:IdentityCalls = 0

function Get-NetTCPConnection {{
    [CmdletBinding()]
    param([string] $State, [int] $LocalPort)
    return [pscustomobject]@{{ LocalAddress = '127.0.0.1'; OwningProcess = 4242 }}
}}

function Get-ProcessIdentity {{
    param([int] $ProcessId)
    $index = [Math]::Min($script:IdentityCalls, $script:MockTicks.Count - 1)
    $ticks = [Int64] $script:MockTicks[$index]
    $script:IdentityCalls += 1
    return [pscustomobject]@{{
        Id = 4242
        StartTicks = $ticks
        Raw = [pscustomobject]@{{ CommandLine = $script:MockCommandLine }}
    }}
}}

function Invoke-ExactLoopbackRequest {{
    param([string] $Url, [scriptblock] $Validator)
    if ($script:MockHttpMatches) {{
        return [pscustomobject]@{{ Url = $Url; Marker = 'exact' }}
    }}
    return $null
}}

function Assert-MockedState {{
    param(
        [string] $Label,
        [string] $Role,
        [int] $Port,
        [string] $CommandLine,
        [bool] $HttpMatches,
        [Int64[]] $Ticks,
        [string] $Expected
    )
    $script:MockCommandLine = $CommandLine
    $script:MockHttpMatches = $HttpMatches
    $script:MockTicks = @($Ticks)
    $script:IdentityCalls = 0
    $actual = Get-PortServiceState -Role $Role -Port $Port -Root $root -FrontendDirectory $frontend
    if ($actual.State -ne $Expected) {{
        throw "$Label expected $Expected but got $($actual.State): $($actual.Reason)"
    }}
}}

$viteScript = Join-Path $frontend 'node_modules\vite\bin\vite.js'
$backendExact = 'python.exe -B -m uvicorn interfaces.main:app --app-dir "' + $root + '" --host 127.0.0.1 --port 8005 --workers 1 --log-level info'
$frontendExact = 'node.exe "' + $viteScript + '" --host 127.0.0.1 --port 3000 --strictPort'
$wrongAdjacency = 'node.exe "' + $viteScript + '" --host wrong 127.0.0.1 --port 3000 --strictPort'
$httpOnly = 'node.exe C:\unrelated\vite.js --host 127.0.0.1 --port 3000 --strictPort'

Assert-MockedState -Label 'backend exact owner plus marker' -Role Backend -Port 8005 -CommandLine $backendExact -HttpMatches $true -Ticks @(101, 101) -Expected Reusable
Assert-MockedState -Label 'frontend exact owner plus marker' -Role Frontend -Port 3000 -CommandLine $frontendExact -HttpMatches $true -Ticks @(101, 101) -Expected Reusable
Assert-MockedState -Label 'wrong option adjacency' -Role Frontend -Port 3000 -CommandLine $wrongAdjacency -HttpMatches $true -Ticks @(101, 101) -Expected Occupied
Assert-MockedState -Label 'HTTP only' -Role Frontend -Port 3000 -CommandLine $httpOnly -HttpMatches $true -Ticks @(101, 101) -Expected Occupied
Assert-MockedState -Label 'path only' -Role Frontend -Port 3000 -CommandLine $frontendExact -HttpMatches $false -Ticks @(101, 101) -Expected Occupied
Assert-MockedState -Label 'same PID creation ticks changed' -Role Frontend -Port 3000 -CommandLine $frontendExact -HttpMatches $true -Ticks @(101, 102) -Expected Occupied
'Get-PortServiceState mock matrix ok'
'''
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", program],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Get-PortServiceState mock matrix ok" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="PowerShell parser is Windows-specific")
def test_launcher_parses_without_execution() -> None:
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "[void][scriptblock]::Create([IO.File]::ReadAllText('"
            + str(PS_LAUNCHER).replace("'", "''")
            + "')); 'parser ok'",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "parser ok" in result.stdout


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
