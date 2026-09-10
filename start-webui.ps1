#requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("launch", "check", "self-test-cleanup", "self-test-mismatch", "self-test-exited", "self-test-capture-failure")]
    [string] $Mode = "launch",

    [string] $GuardPayload
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

function Get-CreationTicks {
    param([Parameter(Mandatory = $true)] $Value)

    if ($Value -is [DateTime]) {
        return $Value.ToUniversalTime().Ticks
    }

    return [System.Management.ManagementDateTimeConverter]::ToDateTime(
        [string] $Value
    ).ToUniversalTime().Ticks
}

function Get-ProcessIdentity {
    param([Parameter(Mandatory = $true)] [int] $ProcessId)

    $record = Get-CimInstance -ClassName Win32_Process `
        -Filter ("ProcessId = {0}" -f $ProcessId) `
        -ErrorAction SilentlyContinue
    if ($null -eq $record) {
        return $null
    }

    try {
        return [pscustomobject]@{
            Id         = [int] $record.ProcessId
            ParentId   = [int] $record.ParentProcessId
            StartTicks = [Int64] (Get-CreationTicks $record.CreationDate)
            Raw        = $record
        }
    }
    catch {
        return $null
    }
}

function ConvertTo-OwnedIdentity {
    param([Parameter(Mandatory = $true)] $Identity)

    return ("{0}|{1}" -f [int] $Identity.Id, [Int64] $Identity.StartTicks)
}

function ConvertFrom-OwnedIdentity {
    param([Parameter(Mandatory = $true)] [string] $Value)

    $parts = $Value.Trim().Split("|", 2)
    if ($parts.Count -ne 2) {
        throw "Invalid owned-process identity."
    }

    [int] $processId = 0
    [Int64] $startTicks = 0
    if (
        -not [int]::TryParse($parts[0], [ref] $processId) -or
        -not [Int64]::TryParse($parts[1], [ref] $startTicks)
    ) {
        throw "Invalid owned-process identity."
    }

    return [pscustomobject]@{
        Id         = $processId
        StartTicks = $startTicks
    }
}

function Test-ExactProcessIdentity {
    param([Parameter(Mandatory = $true)] $Identity)

    $current = Get-ProcessIdentity -ProcessId ([int] $Identity.Id)
    return (
        $null -ne $current -and
        [Int64] $current.StartTicks -eq [Int64] $Identity.StartTicks
    )
}

function Get-ImmutableProcessSnapshot {
    $nodes = @{}
    $children = @{}

    foreach ($record in @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue)) {
        try {
            $node = [pscustomobject]@{
                Id         = [int] $record.ProcessId
                ParentId   = [int] $record.ParentProcessId
                StartTicks = [Int64] (Get-CreationTicks $record.CreationDate)
            }
            $nodes[$node.Id] = $node
            if (-not $children.ContainsKey($node.ParentId)) {
                $children[$node.ParentId] = New-Object System.Collections.ArrayList
            }
            [void] $children[$node.ParentId].Add($node.Id)
        }
        catch {
            # An unreadable process is not eligible to become an owned node.
        }
    }

    return [pscustomobject]@{
        Nodes    = $nodes
        Children = $children
    }
}

function Stop-ExactOwnedProcessTrees {
    param(
        [Parameter(Mandatory = $true)] [object[]] $ExpectedIdentities,
        [scriptblock] $AfterSnapshot
    )

    if (@($ExpectedIdentities).Count -eq 0) {
        return $true
    }

    # This is the single immutable discovery snapshot for one cleanup decision.
    $snapshot = Get-ImmutableProcessSnapshot
    $queue = New-Object "System.Collections.Generic.Queue[int]"

    foreach ($expected in @($ExpectedIdentities)) {
        $expectedId = [int] $expected.Id
        if (-not $snapshot.Nodes.ContainsKey($expectedId)) {
            continue
        }

        $snapshotNode = $snapshot.Nodes[$expectedId]
        if ([Int64] $snapshotNode.StartTicks -ne [Int64] $expected.StartTicks) {
            # A reused PID is never substituted for the retained identity.
            continue
        }
        $queue.Enqueue($expectedId)
    }

    $seen = @{}
    $tree = New-Object System.Collections.ArrayList
    while ($queue.Count -gt 0) {
        $processId = $queue.Dequeue()
        if ($seen.ContainsKey($processId) -or -not $snapshot.Nodes.ContainsKey($processId)) {
            continue
        }

        $seen[$processId] = $true
        $node = $snapshot.Nodes[$processId]
        [void] $tree.Add($node)

        if ($snapshot.Children.ContainsKey($processId)) {
            foreach ($childId in @($snapshot.Children[$processId])) {
                $child = $snapshot.Nodes[[int] $childId]
                if ([Int64] $child.StartTicks -ge [Int64] $node.StartTicks) {
                    $queue.Enqueue([int] $childId)
                }
            }
        }
    }

    if ($null -ne $AfterSnapshot) {
        & $AfterSnapshot $snapshot @($tree) | Out-Null
    }

    $ordered = @($tree)
    [array]::Reverse($ordered)
    foreach ($node in $ordered) {
        # Revalidate every immutable snapshot node immediately before termination.
        $current = Get-ProcessIdentity -ProcessId ([int] $node.Id)
        if (
            $null -ne $current -and
            [Int64] $current.StartTicks -eq [Int64] $node.StartTicks
        ) {
            try {
                Invoke-CimMethod -InputObject $current.Raw -MethodName Terminate `
                    -ErrorAction SilentlyContinue | Out-Null
            }
            catch {
                # The bounded wait below decides whether cleanup succeeded.
            }
        }
    }

    $deadline = (Get-Date).AddSeconds(10)
    do {
        $remaining = 0
        foreach ($node in $ordered) {
            $current = Get-ProcessIdentity -ProcessId ([int] $node.Id)
            if (
                $null -ne $current -and
                [Int64] $current.StartTicks -eq [Int64] $node.StartTicks
            ) {
                $remaining++
            }
        }

        if ($remaining -eq 0) {
            return $true
        }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $deadline)

    return $false
}

function ConvertTo-GuardPayload {
    param([Parameter(Mandatory = $true)] $Configuration)

    $json = $Configuration | ConvertTo-Json -Compress -Depth 8
    return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
}

function ConvertFrom-GuardPayload {
    param([Parameter(Mandatory = $true)] [string] $Payload)

    $json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Payload))
    return $json | ConvertFrom-Json
}

function Remove-PathQuietly {
    param([string] $Path)

    if (-not [string]::IsNullOrWhiteSpace($Path)) {
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-Guard {
    param([Parameter(Mandatory = $true)] [string] $Payload)

    $configuration = ConvertFrom-GuardPayload -Payload $Payload
    $gatePath = [string] $configuration.GatePath
    $identityPath = [string] $configuration.IdentityPath
    $child = $null

    try {
        $deadline = (Get-Date).AddSeconds(30)
        $released = $false
        do {
            if (Test-Path -LiteralPath $gatePath -PathType Leaf) {
                $released = ([IO.File]::ReadAllText($gatePath) -eq "go")
                if ($released) {
                    break
                }
            }
            Start-Sleep -Milliseconds 25
        } while ((Get-Date) -lt $deadline)

        if (-not $released) {
            throw "Guard release sentinel was not received."
        }
        Remove-PathQuietly -Path $gatePath

        if ($null -ne $configuration.Environment) {
            foreach ($property in $configuration.Environment.PSObject.Properties) {
                [Environment]::SetEnvironmentVariable(
                    $property.Name,
                    [string] $property.Value,
                    "Process"
                )
            }
        }

        $startParameters = @{
            FilePath               = [string] $configuration.FilePath
            ArgumentList           = [string[]] @($configuration.Arguments)
            WorkingDirectory       = [string] $configuration.WorkingDirectory
            WindowStyle            = "Hidden"
            RedirectStandardOutput = [string] $configuration.StandardOutput
            RedirectStandardError  = [string] $configuration.StandardError
            PassThru               = $true
        }
        $child = Start-Process @startParameters

        $childIdentity = $null
        $identityDeadline = (Get-Date).AddSeconds(5)
        do {
            $childIdentity = Get-ProcessIdentity -ProcessId $child.Id
            if ($null -eq $childIdentity) {
                Start-Sleep -Milliseconds 25
            }
        } while ($null -eq $childIdentity -and (Get-Date) -lt $identityDeadline)

        if ($null -eq $childIdentity) {
            throw "Guard could not capture its child identity."
        }

        $temporaryIdentityPath = $identityPath + ".tmp-" + [Guid]::NewGuid().ToString("N")
        [IO.File]::WriteAllText(
            $temporaryIdentityPath,
            (ConvertTo-OwnedIdentity -Identity $childIdentity),
            (New-Object Text.UTF8Encoding($false))
        )
        [IO.File]::Move($temporaryIdentityPath, $identityPath)

        if ([bool] $configuration.ExitAfterChildStart) {
            return 0
        }

        $child.WaitForExit()
        return [int] $child.ExitCode
    }
    catch {
        if ($null -ne $child) {
            try {
                if (-not $child.HasExited) {
                    $child.Kill()
                    [void] $child.WaitForExit(5000)
                }
            }
            catch {
                # This handle belongs to the exact child created in this guard.
            }
        }
        throw
    }
}

function Wait-IdentityRecord {
    param(
        [Parameter(Mandatory = $true)] [string] $Path,
        [Parameter(Mandatory = $true)] $GuardIdentity
    )

    $deadline = (Get-Date).AddSeconds(5)
    do {
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            try {
                return ConvertFrom-OwnedIdentity -Value ([IO.File]::ReadAllText($Path))
            }
            catch {
                # An atomic rename is used, but tolerate a transient reader race.
            }
        }

        if (-not (Test-ExactProcessIdentity -Identity $GuardIdentity)) {
            throw "Guard exited before publishing its child identity."
        }
        Start-Sleep -Milliseconds 25
    } while ((Get-Date) -lt $deadline)

    throw "Timed out waiting for the guard child identity."
}

function Start-OwnedGuard {
    param(
        [Parameter(Mandatory = $true)] [string] $Name,
        [Parameter(Mandatory = $true)] [string] $FilePath,
        [Parameter(Mandatory = $true)] [string[]] $Arguments,
        [Parameter(Mandatory = $true)] [string] $WorkingDirectory,
        [Parameter(Mandatory = $true)] [string] $StandardOutput,
        [Parameter(Mandatory = $true)] [string] $StandardError,
        [Parameter(Mandatory = $true)] $OwnedIdentities,
        [hashtable] $Environment = @{},
        [switch] $ExitAfterChildStart,
        [switch] $InjectCaptureFailure
    )

    $token = [Guid]::NewGuid().ToString("N")
    $temporaryRoot = [IO.Path]::GetTempPath()
    $gatePath = Join-Path $temporaryRoot ("PlotPilot-{0}-{1}.gate" -f $Name, $token)
    $identityPath = Join-Path $temporaryRoot ("PlotPilot-{0}-{1}.identity" -f $Name, $token)
    Remove-PathQuietly -Path $gatePath
    Remove-PathQuietly -Path $identityPath

    $configuration = [ordered]@{
        GatePath           = $gatePath
        IdentityPath       = $identityPath
        FilePath           = $FilePath
        Arguments          = @($Arguments)
        WorkingDirectory   = $WorkingDirectory
        StandardOutput     = $StandardOutput
        StandardError      = $StandardError
        Environment        = $Environment
        ExitAfterChildStart = [bool] $ExitAfterChildStart
    }
    $payload = ConvertTo-GuardPayload -Configuration $configuration
    $scriptLiteral = $PSCommandPath.Replace("'", "''")
    $guardCommand = "& '$scriptLiteral' -GuardPayload '$payload'"
    $encodedCommand = [Convert]::ToBase64String(
        [Text.Encoding]::Unicode.GetBytes($guardCommand)
    )
    $powerShellExecutable = Join-Path $PSHOME "powershell.exe"
    $guard = $null
    $guardIdentity = $null
    $released = $false

    try {
        $guard = Start-Process `
            -FilePath $powerShellExecutable `
            -ArgumentList @(
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-EncodedCommand", $encodedCommand
            ) `
            -WorkingDirectory $WorkingDirectory `
            -WindowStyle Hidden `
            -PassThru

        if ($InjectCaptureFailure) {
            throw "Injected guard identity capture failure."
        }

        $identityDeadline = (Get-Date).AddSeconds(5)
        do {
            $guardIdentity = Get-ProcessIdentity -ProcessId $guard.Id
            if ($null -eq $guardIdentity) {
                Start-Sleep -Milliseconds 25
            }
        } while ($null -eq $guardIdentity -and (Get-Date) -lt $identityDeadline)

        if ($null -eq $guardIdentity) {
            throw "Could not retain the $Name guard identity."
        }

        # Retain the exact guard identity before releasing its sentinel.
        [void] $OwnedIdentities.Add($guardIdentity)
        [IO.File]::WriteAllText(
            $gatePath,
            "go",
            (New-Object Text.UTF8Encoding($false))
        )
        $released = $true

        $childIdentity = Wait-IdentityRecord `
            -Path $identityPath `
            -GuardIdentity $guardIdentity
        [void] $OwnedIdentities.Add($childIdentity)

        return [pscustomobject]@{
            RootIdentity  = $guardIdentity
            ChildIdentity = $childIdentity
        }
    }
    catch {
        if ($null -eq $guardIdentity -and $null -ne $guard) {
            $guardExited = $false
            try {
                if (-not $guard.HasExited) {
                    # The sentinel is still closed, so this exact handle owns no payload.
                    $guard.Kill()
                    [void] $guard.WaitForExit(5000)
                }
                $guardExited = $guard.HasExited
            }
            catch {
                $guardExited = $false
            }

            if ($InjectCaptureFailure) {
                return [pscustomobject]@{
                    CaptureFailureProven = (
                        $guardExited -and
                        -not $released -and
                        -not (Test-Path -LiteralPath $identityPath)
                    )
                    GuardId              = $guard.Id
                    SentinelReleased     = $released
                    ChildIdentityCreated = (Test-Path -LiteralPath $identityPath)
                }
            }
        }
        throw
    }
    finally {
        Remove-PathQuietly -Path $gatePath
        Remove-PathQuietly -Path $identityPath
    }
}

function Wait-ExactIdentityAbsent {
    param(
        [Parameter(Mandatory = $true)] $Identity,
        [int] $Seconds = 5
    )

    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        if (-not (Test-ExactProcessIdentity -Identity $Identity)) {
            return $true
        }
        Start-Sleep -Milliseconds 50
    } while ((Get-Date) -lt $deadline)

    return $false
}

function Get-PowerShellExecutable {
    $candidate = Join-Path $PSHOME "powershell.exe"
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        return $candidate
    }
    return (Get-Process -Id $PID).Path
}

function Invoke-OwnedCleanupSelfTest {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("cleanup", "mismatch", "exited", "capture-failure")]
        [string] $TestMode
    )

    $owned = New-Object "System.Collections.Generic.List[object]"
    $powerShellExecutable = Get-PowerShellExecutable
    $marker = "PlotPilot-selftest-" + [Guid]::NewGuid().ToString("N")
    $arguments = @(
        "-NoProfile",
        "-Command",
        ("`$null='{0}'; Start-Sleep -Seconds 120" -f $marker)
    )
    $outputPath = Join-Path ([IO.Path]::GetTempPath()) ($marker + ".out.log")
    $errorPath = Join-Path ([IO.Path]::GetTempPath()) ($marker + ".err.log")

    try {
        if ($TestMode -eq "capture-failure") {
            $proof = Start-OwnedGuard `
                -Name "selftest-capture" `
                -FilePath $powerShellExecutable `
                -Arguments $arguments `
                -WorkingDirectory $PSScriptRoot `
                -StandardOutput $outputPath `
                -StandardError $errorPath `
                -OwnedIdentities $owned `
                -InjectCaptureFailure
            if (-not $proof.CaptureFailureProven) {
                throw "Capture-failure rollback was not proven."
            }
            Write-Host "[self-test] capture failure kept the sentinel closed and exact guard exited."
            Write-Host "owned cleanup self-test simulated a later startup failure."
            return 1
        }

        $started = Start-OwnedGuard `
            -Name ("selftest-" + $TestMode) `
            -FilePath $powerShellExecutable `
            -Arguments $arguments `
            -WorkingDirectory $PSScriptRoot `
            -StandardOutput $outputPath `
            -StandardError $errorPath `
            -OwnedIdentities $owned `
            -ExitAfterChildStart:($TestMode -eq "exited")

        if ($TestMode -eq "mismatch") {
            $wrongIdentity = [pscustomobject]@{
                Id         = $started.RootIdentity.Id
                StartTicks = [Int64] $started.RootIdentity.StartTicks + 1
            }
            [void] (Stop-ExactOwnedProcessTrees -ExpectedIdentities @($wrongIdentity))
            if (
                -not (Test-ExactProcessIdentity -Identity $started.RootIdentity) -or
                -not (Test-ExactProcessIdentity -Identity $started.ChildIdentity)
            ) {
                throw "A same-PID/different-CreationTicks identity was not preserved."
            }
            Write-Host "[self-test] mismatch identity preserved."
        }

        if ($TestMode -eq "exited") {
            if (-not (Wait-ExactIdentityAbsent -Identity $started.RootIdentity)) {
                throw "The exited-root self-test guard did not exit."
            }
            # The separately retained exact child still permits safe cleanup.
            if (-not (Stop-ExactOwnedProcessTrees -ExpectedIdentities @($started.ChildIdentity))) {
                throw "Exited-root child cleanup timed out."
            }
            Write-Host "[self-test] exited root left a retained child that was cleaned exactly."
        }
        else {
            if (-not (Stop-ExactOwnedProcessTrees -ExpectedIdentities $owned.ToArray())) {
                throw "Owned process-tree cleanup timed out."
            }
        }

        foreach ($identity in $owned.ToArray()) {
            if (Test-ExactProcessIdentity -Identity $identity) {
                throw "An exact owned self-test process is still running."
            }
        }
        Write-Host "[self-test] exact owned guard and child tree exited."
        Write-Host "owned cleanup self-test simulated a later startup failure."
        return 1
    }
    finally {
        if ($owned.Count -gt 0) {
            [void] (Stop-ExactOwnedProcessTrees -ExpectedIdentities $owned.ToArray())
        }
        Remove-PathQuietly -Path $outputPath
        Remove-PathQuietly -Path $errorPath
    }
}

function Resolve-SourceTools {
    param(
        [Parameter(Mandatory = $true)] [string] $Root,
        [Parameter(Mandatory = $true)] [string] $FrontendDirectory
    )

    if (-not (Test-Path -LiteralPath $FrontendDirectory -PathType Container)) {
        throw "Frontend directory not found: $FrontendDirectory"
    }

    $pythonCommand = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $pythonCommand) {
        throw "Python was not found on PATH."
    }

    $pnpmCommand = Get-Command pnpm.cmd -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $pnpmCommand) {
        throw "pnpm.cmd was not found on PATH."
    }

    return [pscustomobject]@{
        Python = $pythonCommand.Source
        Pnpm   = $pnpmCommand.Source
    }
}

function Test-SourceEnvironment {
    param(
        [Parameter(Mandatory = $true)] [string] $Root,
        [Parameter(Mandatory = $true)] [string] $FrontendDirectory,
        [Parameter(Mandatory = $true)] [string] $DataDirectory,
        [Parameter(Mandatory = $true)] $Tools
    )

    $backendEntry = Join-Path $Root "interfaces\main.py"
    if (-not (Test-Path -LiteralPath $backendEntry -PathType Leaf)) {
        throw "Backend entry not found: $backendEntry"
    }

    Write-Host ("[check] Python/uvicorn: {0}" -f $Tools.Python)
    & $Tools.Python -c "import sys, uvicorn; print('  Python ' + sys.version.split()[0] + ' / uvicorn ' + getattr(uvicorn, '__version__', 'imported'))"
    if ($LASTEXITCODE -ne 0) {
        throw "Python exists but uvicorn cannot be imported."
    }

    Write-Host ("[check] pnpm/Vite: {0}" -f $Tools.Pnpm)
    & $Tools.Pnpm --dir $FrontendDirectory exec vite --version
    if ($LASTEXITCODE -ne 0) {
        throw "pnpm exists but the frontend Vite executable is unavailable."
    }

    $resolvedData = [IO.Path]::GetFullPath($DataDirectory)
    $probe = $resolvedData
    while ($probe -and -not (Test-Path -LiteralPath $probe -PathType Container)) {
        $parent = Split-Path -Parent $probe
        if ($parent -eq $probe) {
            $probe = $null
        }
        else {
            $probe = $parent
        }
    }
    if (-not $probe) {
        throw "Data path has no existing parent: $resolvedData"
    }
    Write-Host ("[check] Data path: {0}" -f $resolvedData)
}

function Test-PortAvailable {
    param(
        [Parameter(Mandatory = $true)] [int] $Port,
        [Parameter(Mandatory = $true)] [string] $Label
    )

    $owners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
    if ($owners.Count -gt 0) {
        $ownerIds = ($owners | ForEach-Object { $_.OwningProcess }) -join ","
        throw "$Label port $Port is already listening (OwningProcess=$ownerIds); it will not be taken over."
    }
    Write-Host ("[check] {0} port {1} is available." -f $Label, $Port)
}

function Wait-HttpReady {
    param(
        [Parameter(Mandatory = $true)] [string] $Url,
        [Parameter(Mandatory = $true)] [string] $Label,
        [Parameter(Mandatory = $true)] [string] $StandardOutput,
        [Parameter(Mandatory = $true)] [string] $StandardError
    )

    $deadline = (Get-Date).AddSeconds(60)
    do {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 3
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                Write-Host ("[ready] {0}" -f $Label)
                return
            }
        }
        catch {
            # Readiness is bounded by the deadline below.
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    throw "$Label was not ready in 60 seconds: $Url. See stdout $StandardOutput and stderr $StandardError."
}

function Invoke-Launcher {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("launch", "check")]
        [string] $LauncherMode
    )

    $root = $PSScriptRoot
    $frontendDirectory = Join-Path $root "frontend"
    $backendPort = 8005
    $frontendPort = 3000
    if (-not [string]::IsNullOrWhiteSpace($env:PLOTPILOT_PROD_DATA_DIR)) {
        $dataDirectory = $env:PLOTPILOT_PROD_DATA_DIR
    }
    else {
        if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
            throw "LOCALAPPDATA is not set, so the default data directory cannot be resolved."
        }
        $dataDirectory = Join-Path $env:LOCALAPPDATA "PlotPilot\data"
    }

    $tools = Resolve-SourceTools -Root $root -FrontendDirectory $frontendDirectory
    Test-SourceEnvironment `
        -Root $root `
        -FrontendDirectory $frontendDirectory `
        -DataDirectory $dataDirectory `
        -Tools $tools
    Test-PortAvailable -Port $backendPort -Label "Backend"
    Test-PortAvailable -Port $frontendPort -Label "Frontend"

    if ($LauncherMode -eq "check") {
        Write-Host "[pass] --check completed without creating data, starting services, or opening a browser."
        return 0
    }

    if (-not (Test-Path -LiteralPath $dataDirectory -PathType Container)) {
        [void] [IO.Directory]::CreateDirectory($dataDirectory)
    }

    $temporaryRoot = [IO.Path]::GetTempPath()
    $backendOutput = Join-Path $temporaryRoot "PlotPilot-webui-backend.out.log"
    $backendError = Join-Path $temporaryRoot "PlotPilot-webui-backend.err.log"
    $frontendOutput = Join-Path $temporaryRoot "PlotPilot-webui-frontend.out.log"
    $frontendError = Join-Path $temporaryRoot "PlotPilot-webui-frontend.err.log"
    $owned = New-Object "System.Collections.Generic.List[object]"

    try {
        $backendEnvironment = @{
            PLOTPILOT_PROD_DATA_DIR = $dataDirectory
            DISABLE_AUTO_DAEMON     = "1"
            PYTHONIOENCODING        = "utf-8"
            PYTHONUNBUFFERED        = "1"
        }
        if ($null -eq [Environment]::GetEnvironmentVariable("VECTOR_STORE_ENABLED")) {
            $backendEnvironment["VECTOR_STORE_ENABLED"] = "false"
        }

        Write-Host ("[start] Backend: 127.0.0.1:{0}" -f $backendPort)
        [void] (Start-OwnedGuard `
            -Name "backend" `
            -FilePath $tools.Python `
            -Arguments @(
                "-m", "uvicorn", "interfaces.main:app",
                "--host", "127.0.0.1",
                "--port", "$backendPort",
                "--workers", "1",
                "--log-level", "info"
            ) `
            -WorkingDirectory $root `
            -StandardOutput $backendOutput `
            -StandardError $backendError `
            -OwnedIdentities $owned `
            -Environment $backendEnvironment)

        Write-Host ("[start] Frontend: 127.0.0.1:{0}" -f $frontendPort)
        [void] (Start-OwnedGuard `
            -Name "frontend" `
            -FilePath $env:ComSpec `
            -Arguments @(
                "/d", "/s", "/c",
                "pnpm.cmd exec vite --host 127.0.0.1 --port 3000 --strictPort"
            ) `
            -WorkingDirectory $frontendDirectory `
            -StandardOutput $frontendOutput `
            -StandardError $frontendError `
            -OwnedIdentities $owned)

        Wait-HttpReady `
            -Url "http://127.0.0.1:$backendPort/health" `
            -Label "Backend" `
            -StandardOutput $backendOutput `
            -StandardError $backendError
        Wait-HttpReady `
            -Url "http://127.0.0.1:$frontendPort/" `
            -Label "Frontend" `
            -StandardOutput $frontendOutput `
            -StandardError $frontendError

        $browserUrl = "http://127.0.0.1:$frontendPort/"
        Write-Host ("[ready] Opening the default browser: {0}" -f $browserUrl)
        Start-Process -FilePath $browserUrl | Out-Null
        Write-Host "[done] The browser WebUI is running; backend and frontend windows are hidden."
        return 0
    }
    catch {
        $failureMessage = $_.Exception.Message
        $cleanupSucceeded = Stop-ExactOwnedProcessTrees `
            -ExpectedIdentities $owned.ToArray()
        [Console]::Error.WriteLine("[failed] {0}", $failureMessage)
        if (-not $cleanupSucceeded) {
            [Console]::Error.WriteLine("Exact owned-process cleanup timed out.")
        }
        Write-Host "Existing processes on ports 8005 and 3000 were not taken over."
        return 1
    }
}

if (-not [string]::IsNullOrWhiteSpace($GuardPayload)) {
    try {
        exit (Invoke-Guard -Payload $GuardPayload)
    }
    catch {
        [Console]::Error.WriteLine($_.Exception.Message)
        exit 1
    }
}

try {
    switch ($Mode) {
        "self-test-cleanup" {
            exit (Invoke-OwnedCleanupSelfTest -TestMode "cleanup")
        }
        "self-test-mismatch" {
            exit (Invoke-OwnedCleanupSelfTest -TestMode "mismatch")
        }
        "self-test-exited" {
            exit (Invoke-OwnedCleanupSelfTest -TestMode "exited")
        }
        "self-test-capture-failure" {
            exit (Invoke-OwnedCleanupSelfTest -TestMode "capture-failure")
        }
        default {
            exit (Invoke-Launcher -LauncherMode $Mode)
        }
    }
}
catch {
    [Console]::Error.WriteLine("[failed] {0}", $_.Exception.Message)
    exit 1
}
