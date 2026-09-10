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
            # Exit-after-start can publish atomically between the path probe and identity check.
            if (Test-Path -LiteralPath $Path -PathType Leaf) {
                try {
                    return ConvertFrom-OwnedIdentity -Value ([IO.File]::ReadAllText($Path))
                }
                catch {
                    # A malformed final record is still a failed guard publication.
                }
            }
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

function ConvertFrom-WindowsCommandLine {
    param([AllowNull()] [string] $Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return @()
    }

    $tokens = New-Object System.Collections.ArrayList
    foreach ($match in [regex]::Matches($Value, '"([^"]*)"|(\S+)')) {
        if ($match.Groups[1].Success) {
            [void] $tokens.Add($match.Groups[1].Value)
        }
        else {
            [void] $tokens.Add($match.Groups[2].Value)
        }
    }
    return @($tokens)
}

function Test-ExactToken {
    param(
        [Parameter(Mandatory = $true)] [string[]] $Tokens,
        [Parameter(Mandatory = $true)] [string] $Expected
    )

    return @($Tokens | Where-Object {
        [string]::Equals($_, $Expected, [StringComparison]::OrdinalIgnoreCase)
    }).Count -gt 0
}

function Test-OptionValuePair {
    param(
        [Parameter(Mandatory = $true)] [string[]] $Tokens,
        [Parameter(Mandatory = $true)] [string] $Option,
        [Parameter(Mandatory = $true)] [string] $ExpectedValue
    )

    for ($index = 0; $index -lt ($Tokens.Count - 1); $index++) {
        if (
            [string]::Equals($Tokens[$index], $Option, [StringComparison]::OrdinalIgnoreCase) -and
            [string]::Equals($Tokens[$index + 1], $ExpectedValue, [StringComparison]::OrdinalIgnoreCase)
        ) {
            return $true
        }
    }
    return $false
}

function Test-ViteScriptUnderFrontendRoot {
    param(
        [Parameter(Mandatory = $true)] [string[]] $Tokens,
        [Parameter(Mandatory = $true)] [string] $FrontendDirectory
    )

    $frontendRoot = [IO.Path]::GetFullPath($FrontendDirectory).TrimEnd("\")
    foreach ($token in @($Tokens)) {
        try {
            $candidate = [IO.Path]::GetFullPath($token.Replace("/", "\"))
        }
        catch {
            continue
        }
        if (
            $candidate.StartsWith($frontendRoot + "\", [StringComparison]::OrdinalIgnoreCase) -and
            [string]::Equals([IO.Path]::GetFileName($candidate), "vite.js", [StringComparison]::OrdinalIgnoreCase)
        ) {
            return $true
        }
    }
    return $false
}

function Test-HttpSuccess {
    param([AllowNull()] $Response)

    return $null -ne $Response -and $Response.StatusCode -ge 200 -and $Response.StatusCode -lt 300
}

function Test-ExactLoopbackResponse {
    param(
        [AllowNull()] $Response,
        [Parameter(Mandatory = $true)] [string] $ExpectedUri,
        [Parameter(Mandatory = $true)] [scriptblock] $Validator
    )

    if (-not (Test-HttpSuccess -Response $Response)) {
        return $false
    }
    try {
        $finalUri = [string] $Response.BaseResponse.ResponseUri.AbsoluteUri
        $expected = ([Uri] $ExpectedUri).AbsoluteUri
    }
    catch {
        return $false
    }
    return (
        [string]::Equals($finalUri, $expected, [StringComparison]::OrdinalIgnoreCase) -and
        [bool] (& $Validator $Response)
    )
}

function Test-FrontendHttpContract {
    param([AllowNull()] $Response)

    if (-not (Test-HttpSuccess -Response $Response)) {
        return $false
    }
    $content = [string] $Response.Content
    foreach ($marker in @('PlotPilot', 'id="app"', 'id="boot-splash"', 'src="/src/main.ts"')) {
        if ($content.IndexOf($marker, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
            return $false
        }
    }
    return $true
}

function Test-BackendHttpContract {
    param([AllowNull()] $Response)

    if (-not (Test-HttpSuccess -Response $Response)) {
        return $false
    }
    try {
        $payload = ([string] $Response.Content) | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        return $false
    }
    return (
        [string] $payload.status -eq "healthy" -and
        -not [string]::IsNullOrWhiteSpace([string] $payload.version) -and
        -not [string]::IsNullOrWhiteSpace([string] $payload.build_id)
    )
}

function Invoke-ExactLoopbackRequest {
    param(
        [Parameter(Mandatory = $true)] [string] $Url,
        [Parameter(Mandatory = $true)] [scriptblock] $Validator
    )

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -MaximumRedirection 0 -TimeoutSec 3 -ErrorAction Stop
        if (Test-ExactLoopbackResponse -Response $response -ExpectedUri $Url -Validator $Validator) {
            return $response
        }
    }
    catch {
        # Redirects, non-loopback finals, and marker failures are not reusable.
    }
    return $null
}

function Test-RoleEvidence {
    param([bool] $OwnerMatches, [bool] $HttpMatches)

    return $OwnerMatches -and $HttpMatches
}

function Get-LoopbackListenerOwner {
    param([Parameter(Mandatory = $true)] [int] $Port)

    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
    if ($listeners.Count -eq 0) {
        return [pscustomobject]@{ State = "Available"; Owner = $null; Reason = "No listener" }
    }
    $loopback = @($listeners | Where-Object { $_.LocalAddress -eq "127.0.0.1" })
    if ($loopback.Count -eq 0) {
        return [pscustomobject]@{ State = "Occupied"; Owner = $null; Reason = "Listener is not bound to 127.0.0.1" }
    }
    $ownerIds = @($loopback | ForEach-Object { [int] $_.OwningProcess } | Sort-Object -Unique)
    if ($ownerIds.Count -ne 1) {
        return [pscustomobject]@{ State = "Occupied"; Owner = $null; Reason = "Multiple loopback listener owners" }
    }
    $owner = Get-ProcessIdentity -ProcessId $ownerIds[0]
    if ($null -eq $owner) {
        return [pscustomobject]@{ State = "Occupied"; Owner = $null; Reason = "Direct listener owner is unreadable" }
    }
    return [pscustomobject]@{ State = "Candidate"; Owner = $owner; Reason = "Direct loopback listener owner" }
}

function Test-BackendOwner {
    param([Parameter(Mandatory = $true)] $Owner, [Parameter(Mandatory = $true)] [string] $Root)

    $tokens = @(ConvertFrom-WindowsCommandLine -Value ([string] $Owner.Raw.CommandLine))
    return (
        (Test-ExactToken -Tokens $tokens -Expected "uvicorn") -and
        (Test-ExactToken -Tokens $tokens -Expected "interfaces.main:app") -and
        (Test-OptionValuePair -Tokens $tokens -Option "--app-dir" -ExpectedValue ([IO.Path]::GetFullPath($Root))) -and
        (Test-OptionValuePair -Tokens $tokens -Option "--host" -ExpectedValue "127.0.0.1") -and
        (Test-OptionValuePair -Tokens $tokens -Option "--port" -ExpectedValue "8005") -and
        (Test-OptionValuePair -Tokens $tokens -Option "--workers" -ExpectedValue "1")
    )
}

function Test-FrontendOwner {
    param([Parameter(Mandatory = $true)] $Owner, [Parameter(Mandatory = $true)] [string] $FrontendDirectory)

    $tokens = @(ConvertFrom-WindowsCommandLine -Value ([string] $Owner.Raw.CommandLine))
    return (
        (Test-ViteScriptUnderFrontendRoot -Tokens $tokens -FrontendDirectory $FrontendDirectory) -and
        (Test-OptionValuePair -Tokens $tokens -Option "--host" -ExpectedValue "127.0.0.1") -and
        (Test-OptionValuePair -Tokens $tokens -Option "--port" -ExpectedValue "3000") -and
        (Test-ExactToken -Tokens $tokens -Expected "--strictPort")
    )
}

function Get-PortServiceState {
    param(
        [Parameter(Mandatory = $true)] [ValidateSet("Backend", "Frontend")] [string] $Role,
        [Parameter(Mandatory = $true)] [int] $Port,
        [Parameter(Mandatory = $true)] [string] $Root,
        [Parameter(Mandatory = $true)] [string] $FrontendDirectory
    )

    $candidate = Get-LoopbackListenerOwner -Port $Port
    if ($candidate.State -ne "Candidate") {
        return [pscustomobject]@{ Role = $Role; Port = $Port; State = $candidate.State; Owner = $null; Reason = $candidate.Reason }
    }
    $ownerMatches = if ($Role -eq "Backend") {
        Test-BackendOwner -Owner $candidate.Owner -Root $Root
    }
    else {
        Test-FrontendOwner -Owner $candidate.Owner -FrontendDirectory $FrontendDirectory
    }
    $url = if ($Role -eq "Backend") { "http://127.0.0.1:$Port/health" } else { "http://127.0.0.1:$Port/" }
    $validator = if ($Role -eq "Backend") { { param($response) Test-BackendHttpContract -Response $response } } else { { param($response) Test-FrontendHttpContract -Response $response } }
    $httpMatches = $null -ne (Invoke-ExactLoopbackRequest -Url $url -Validator $validator)
    $finalCandidate = Get-LoopbackListenerOwner -Port $Port
    $stableOwner = (
        $finalCandidate.State -eq "Candidate" -and
        $finalCandidate.Owner.Id -eq $candidate.Owner.Id -and
        $finalCandidate.Owner.StartTicks -eq $candidate.Owner.StartTicks
    )
    if (-not (Test-RoleEvidence -OwnerMatches ($ownerMatches -and $stableOwner) -HttpMatches $httpMatches)) {
        return [pscustomobject]@{ Role = $Role; Port = $Port; State = "Occupied"; Owner = $null; Reason = "Direct owner, identity, or exact HTTP marker did not match" }
    }
    return [pscustomobject]@{ Role = $Role; Port = $Port; State = "Reusable"; Owner = $candidate.Owner; Reason = "Direct owner and exact loopback HTTP marker matched" }
}

function Get-LauncherPlan {
    param(
        [Parameter(Mandatory = $true)] [ValidateSet("Available", "Reusable", "Occupied")] [string] $BackendState,
        [Parameter(Mandatory = $true)] [ValidateSet("Available", "Reusable", "Occupied")] [string] $FrontendState,
        [Parameter(Mandatory = $true)] [ValidateSet("launch", "check")] [string] $LauncherMode
    )

    if ($BackendState -eq "Occupied" -or $FrontendState -eq "Occupied") {
        throw "A required WebUI port is occupied by an unrelated, ambiguous, or unhealthy process."
    }
    return [pscustomobject]@{
        StartBackend     = ($LauncherMode -eq "launch" -and $BackendState -eq "Available")
        StartFrontend    = ($LauncherMode -eq "launch" -and $FrontendState -eq "Available")
        ValidateBackend  = ($BackendState -eq "Available")
        ValidateFrontend = ($FrontendState -eq "Available")
    }
}

function Resolve-SourceTools {
    param([bool] $NeedBackend, [bool] $NeedFrontend, [string] $FrontendDirectory)

    $tools = [pscustomobject]@{ Python = $null; Pnpm = $null }
    if ($NeedBackend) {
        $tools.Python = (Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
        if ([string]::IsNullOrWhiteSpace($tools.Python)) {
            throw "Python was not found on PATH."
        }
    }
    if ($NeedFrontend) {
        if (-not (Test-Path -LiteralPath $FrontendDirectory -PathType Container)) {
            throw "Frontend directory not found: $FrontendDirectory"
        }
        $tools.Pnpm = (Get-Command pnpm.cmd -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
        if ([string]::IsNullOrWhiteSpace($tools.Pnpm)) {
            throw "pnpm.cmd was not found on PATH."
        }
    }
    return $tools
}

function Test-SourceEnvironment {
    param(
        [bool] $NeedBackend,
        [bool] $NeedFrontend,
        [Parameter(Mandatory = $true)] [string] $Root,
        [Parameter(Mandatory = $true)] [string] $FrontendDirectory,
        [Parameter(Mandatory = $true)] [string] $DataDirectory,
        [Parameter(Mandatory = $true)] $Tools,
        [Parameter(Mandatory = $true)] [string] $SourcePythonPath
    )

    if ($NeedBackend) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root "interfaces\main.py") -PathType Leaf)) {
            throw "Backend entry not found."
        }
        $previousPath = [Environment]::GetEnvironmentVariable("PYTHONPATH", "Process")
        $previousNoBytecode = [Environment]::GetEnvironmentVariable("PYTHONDONTWRITEBYTECODE", "Process")
        try {
            [Environment]::SetEnvironmentVariable("PYTHONPATH", $SourcePythonPath, "Process")
            [Environment]::SetEnvironmentVariable("PYTHONDONTWRITEBYTECODE", "1", "Process")
            $sdkProbe = @(
                'import sys',
                'import uvicorn',
                'import backend.plotpilot_plugin_sdk as backend_sdk',
                'import plotpilot_plugin_sdk as public_sdk',
                'from backend.plotpilot_plugin_sdk import ContractError as backend_error',
                'from plotpilot_plugin_sdk import ContractError as public_error',
                'from backend.plotpilot_core.repositories.authority import P3_JOB_MIGRATIONS',
                'assert public_sdk is backend_sdk',
                'assert public_error is backend_error',
                'assert sys.modules["plotpilot_plugin_sdk"] is backend_sdk',
                'assert P3_JOB_MIGRATIONS',
                'print("source SDK identity and migrations verified")'
            ) -join [Environment]::NewLine
            & $Tools.Python -B -c $sdkProbe
            if ($LASTEXITCODE -ne 0) {
                throw "Python source runtime probe failed."
            }
        }
        finally {
            [Environment]::SetEnvironmentVariable("PYTHONPATH", $previousPath, "Process")
            [Environment]::SetEnvironmentVariable("PYTHONDONTWRITEBYTECODE", $previousNoBytecode, "Process")
        }
        $probe = [IO.Path]::GetFullPath($DataDirectory)
        while ($probe -and -not (Test-Path -LiteralPath $probe -PathType Container)) {
            $probe = Split-Path -Parent $probe
        }
        if (-not $probe) {
            throw "Data path has no existing parent: $DataDirectory"
        }
    }
    if ($NeedFrontend) {
        & $Tools.Pnpm --dir $FrontendDirectory exec vite --version
        if ($LASTEXITCODE -ne 0) {
            throw "pnpm exists but the frontend Vite executable is unavailable."
        }
    }
}

function Wait-HttpReady {
    param(
        [Parameter(Mandatory = $true)] [string] $Url,
        [Parameter(Mandatory = $true)] [string] $Label,
        [Parameter(Mandatory = $true)] [string] $StandardOutput,
        [Parameter(Mandatory = $true)] [string] $StandardError,
        [Parameter(Mandatory = $true)] [scriptblock] $Validator
    )

    $deadline = (Get-Date).AddSeconds(60)
    do {
        if ($null -ne (Invoke-ExactLoopbackRequest -Url $Url -Validator $Validator)) {
            Write-Host ("[ready] {0}" -f $Label)
            return
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "$Label was not ready in 60 seconds: $Url. See stdout $StandardOutput and stderr $StandardError."
}

function Assert-FinalServiceState {
    param(
        [Parameter(Mandatory = $true)] $Initial,
        [Parameter(Mandatory = $true)] $Final,
        [bool] $Started
    )

    if ($Started) {
        if ($Final.State -ne "Reusable") {
            throw "$($Initial.Role) did not become reusable after start."
        }
        return
    }
    if ($Initial.State -eq "Reusable") {
        if (
            $Final.State -ne "Reusable" -or
            $Final.Owner.Id -ne $Initial.Owner.Id -or
            $Final.Owner.StartTicks -ne $Initial.Owner.StartTicks
        ) {
            throw "$($Initial.Role) direct listener owner changed before final observation."
        }
        return
    }
    if ($Final.State -ne "Available") {
        throw "$($Initial.Role) changed while check mode was observing it."
    }
}

function Write-ServiceState {
    param([Parameter(Mandatory = $true)] $State, [string] $Phase = "check")

    $identity = if ($null -eq $State.Owner) { "" } else { "; PID $($State.Owner.Id), StartTicks $($State.Owner.StartTicks)" }
    Write-Host ("[{0}] {1} port {2}: {3} ({4}{5})" -f $Phase, $State.Role, $State.Port, $State.State, $State.Reason, $identity)
}

function Invoke-Launcher {
    param([Parameter(Mandatory = $true)] [ValidateSet("launch", "check")] [string] $LauncherMode)

    $root = [IO.Path]::GetFullPath($PSScriptRoot)
    $frontendDirectory = Join-Path $root "frontend"
    $backendPort = 8005
    $frontendPort = 3000
    $dataDirectory = if (-not [string]::IsNullOrWhiteSpace($env:PLOTPILOT_PROD_DATA_DIR)) {
        $env:PLOTPILOT_PROD_DATA_DIR
    }
    else {
        Join-Path $env:LOCALAPPDATA "PlotPilot\data"
    }
    $sourcePythonPath = $root

    # Single flow: observe both roles, plan, validate/start only missing roles, wait, then reobserve both.
    $backendState = Get-PortServiceState -Role "Backend" -Port $backendPort -Root $root -FrontendDirectory $frontendDirectory
    $frontendState = Get-PortServiceState -Role "Frontend" -Port $frontendPort -Root $root -FrontendDirectory $frontendDirectory
    $plan = Get-LauncherPlan -BackendState $backendState.State -FrontendState $frontendState.State -LauncherMode $LauncherMode
    Write-ServiceState -State $backendState
    Write-ServiceState -State $frontendState

    $tools = Resolve-SourceTools -NeedBackend $plan.ValidateBackend -NeedFrontend $plan.ValidateFrontend -FrontendDirectory $frontendDirectory
    Test-SourceEnvironment -NeedBackend $plan.ValidateBackend -NeedFrontend $plan.ValidateFrontend -Root $root -FrontendDirectory $frontendDirectory -DataDirectory $dataDirectory -Tools $tools -SourcePythonPath $sourcePythonPath

    if ($LauncherMode -eq "check") {
        if ($plan.StartBackend -or $plan.StartFrontend) {
            throw "Check mode produced an owned-service start plan."
        }
    }
    if ($LauncherMode -eq "launch" -and $plan.StartBackend -and -not (Test-Path -LiteralPath $dataDirectory -PathType Container)) {
        [void] [IO.Directory]::CreateDirectory($dataDirectory)
    }
    $owned = New-Object "System.Collections.Generic.List[object]"
    $temporaryRoot = [IO.Path]::GetTempPath()
    $backendOutput = Join-Path $temporaryRoot "PlotPilot-webui-backend.out.log"
    $backendError = Join-Path $temporaryRoot "PlotPilot-webui-backend.err.log"
    $frontendOutput = Join-Path $temporaryRoot "PlotPilot-webui-frontend.out.log"
    $frontendError = Join-Path $temporaryRoot "PlotPilot-webui-frontend.err.log"

    try {
        if ($plan.StartBackend) {
            $backendEnvironment = @{
                PLOTPILOT_PROD_DATA_DIR = $dataDirectory
                DISABLE_AUTO_DAEMON     = "1"
                PYTHONIOENCODING        = "utf-8"
                PYTHONUNBUFFERED        = "1"
                PYTHONDONTWRITEBYTECODE = "1"
                PYTHONPATH              = $sourcePythonPath
            }
            if ($null -eq [Environment]::GetEnvironmentVariable("VECTOR_STORE_ENABLED")) {
                $backendEnvironment["VECTOR_STORE_ENABLED"] = "false"
            }
            [void] (Start-OwnedGuard -Name "backend" -FilePath $tools.Python -Arguments @("-B", "-m", "uvicorn", "interfaces.main:app", "--app-dir", (ConvertTo-QuotedWindowsArgument -Value $root), "--host", "127.0.0.1", "--port", "$backendPort", "--workers", "1", "--log-level", "info") -WorkingDirectory $root -StandardOutput $backendOutput -StandardError $backendError -OwnedIdentities $owned -Environment $backendEnvironment)
        }
        if ($plan.StartFrontend) {
            [void] (Start-OwnedGuard -Name "frontend" -FilePath $env:ComSpec -Arguments @("/d", "/s", "/c", "pnpm.cmd exec vite --host 127.0.0.1 --port 3000 --strictPort") -WorkingDirectory $frontendDirectory -StandardOutput $frontendOutput -StandardError $frontendError -OwnedIdentities $owned)
        }
        if ($plan.StartBackend) {
            Wait-HttpReady -Url "http://127.0.0.1:$backendPort/health" -Label "Backend" -StandardOutput $backendOutput -StandardError $backendError -Validator { param($response) Test-BackendHttpContract -Response $response }
        }
        if ($plan.StartFrontend) {
            Wait-HttpReady -Url "http://127.0.0.1:$frontendPort/" -Label "Frontend" -StandardOutput $frontendOutput -StandardError $frontendError -Validator { param($response) Test-FrontendHttpContract -Response $response }
        }

        $finalBackend = Get-PortServiceState -Role "Backend" -Port $backendPort -Root $root -FrontendDirectory $frontendDirectory
        $finalFrontend = Get-PortServiceState -Role "Frontend" -Port $frontendPort -Root $root -FrontendDirectory $frontendDirectory
        Assert-FinalServiceState -Initial $backendState -Final $finalBackend -Started $plan.StartBackend
        Assert-FinalServiceState -Initial $frontendState -Final $finalFrontend -Started $plan.StartFrontend

        if ($LauncherMode -eq "check") {
            Write-ServiceState -State $finalBackend -Phase "final"
            Write-ServiceState -State $finalFrontend -Phase "final"
            Write-Host "[pass] --check completed without creating data, starting services, opening a browser, or writing bytecode."
            return 0
        }
        $browserUrl = "http://127.0.0.1:$frontendPort/"
        Start-Process -FilePath $browserUrl | Out-Null
        Write-Host "[done] The browser WebUI is running; reusable services were not added to owned cleanup."
        return 0
    }
    catch {
        $cleanupSucceeded = Stop-ExactOwnedProcessTrees -ExpectedIdentities $owned.ToArray()
        [Console]::Error.WriteLine("[failed] {0}", $_.Exception.Message)
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
