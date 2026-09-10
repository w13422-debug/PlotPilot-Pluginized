#requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("launch", "check", "self-test-plan", "self-test-cleanup", "self-test-mismatch", "self-test-exited", "self-test-capture-failure")]
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

function ConvertTo-NormalizedProcessText {
    param([AllowNull()] [string] $Value)

    if ($null -eq $Value) {
        return ""
    }
    return $Value.Replace("/", "\")
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

function Test-CommandTokenPresent {
    param(
        [Parameter(Mandatory = $true)] [string[]] $Tokens,
        [Parameter(Mandatory = $true)] [string] $RequiredToken
    )

    foreach ($token in @($Tokens)) {
        if ([string]::Equals($token, $RequiredToken, [StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
        if ($RequiredToken -eq "vite") {
            $leaf = [IO.Path]::GetFileNameWithoutExtension(
                (ConvertTo-NormalizedProcessText -Value $token)
            )
            if ([string]::Equals($leaf, "vite", [StringComparison]::OrdinalIgnoreCase)) {
                return $true
            }
        }
    }
    return $false
}

function Test-RecordContainsProductPath {
    param(
        [Parameter(Mandatory = $true)] [string[]] $Tokens,
        [AllowNull()] [string] $ExecutablePath,
        [Parameter(Mandatory = $true)] [string] $RequiredPath
    )

    $normalizedRequired = (
        ConvertTo-NormalizedProcessText -Value ([IO.Path]::GetFullPath($RequiredPath))
    ).TrimEnd("\")
    foreach ($candidate in @($Tokens) + @([string] $ExecutablePath)) {
        if ([string]::IsNullOrWhiteSpace($candidate)) {
            continue
        }
        $normalizedCandidate = (ConvertTo-NormalizedProcessText -Value $candidate).TrimEnd("\")
        if (
            [string]::Equals($normalizedCandidate, $normalizedRequired, [StringComparison]::OrdinalIgnoreCase) -or
            $normalizedCandidate.StartsWith($normalizedRequired + "\", [StringComparison]::OrdinalIgnoreCase)
        ) {
            return $true
        }
    }
    return $false
}

function Test-PathArgumentValue {
    param(
        [Parameter(Mandatory = $true)] [string[]] $Tokens,
        [Parameter(Mandatory = $true)] [string] $ArgumentName,
        [Parameter(Mandatory = $true)] [string] $ExpectedPath
    )

    $normalizedExpected = (
        ConvertTo-NormalizedProcessText -Value ([IO.Path]::GetFullPath($ExpectedPath))
    ).TrimEnd("\")
    for ($index = 0; $index -lt ($Tokens.Count - 1); $index++) {
        if (-not [string]::Equals($Tokens[$index], $ArgumentName, [StringComparison]::OrdinalIgnoreCase)) {
            continue
        }
        $normalizedActual = (ConvertTo-NormalizedProcessText -Value $Tokens[$index + 1]).TrimEnd("\")
        if ([string]::Equals($normalizedActual, $normalizedExpected, [StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Test-ProcessSourceAssociation {
    param(
        [Parameter(Mandatory = $true)] [object[]] $ProcessRecords,
        [Parameter(Mandatory = $true)] [string] $RequiredPath,
        [Parameter(Mandatory = $true)] [string[]] $RequiredTokens,
        [string] $RequiredPathArgument
    )

    foreach ($record in @($ProcessRecords)) {
        $tokens = @(ConvertFrom-WindowsCommandLine -Value ([string] $record.CommandLine))
        if (-not (Test-RecordContainsProductPath -Tokens $tokens -ExecutablePath ([string] $record.ExecutablePath) -RequiredPath $RequiredPath)) {
            continue
        }
        $missingTokens = @(
            $RequiredTokens | Where-Object {
                -not (Test-CommandTokenPresent -Tokens $tokens -RequiredToken ([string] $_))
            }
        )
        if ($missingTokens.Count -gt 0) {
            continue
        }
        if (
            -not [string]::IsNullOrWhiteSpace($RequiredPathArgument) -and
            -not (Test-PathArgumentValue -Tokens $tokens -ArgumentName $RequiredPathArgument -ExpectedPath $RequiredPath)
        ) {
            continue
        }
        return $true
    }
    return $false
}

function ConvertTo-QuotedWindowsArgument {
    param([Parameter(Mandatory = $true)] [string] $Value)

    if ($Value.Contains('"')) {
        throw "A Windows command-line path argument cannot contain a quote."
    }
    return '"' + $Value + '"'
}

function Get-ProcessSourceChain {
    param([Parameter(Mandatory = $true)] [int] $ProcessId)

    $records = @{}
    foreach ($record in @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue)) {
        try {
            $records[[int] $record.ProcessId] = [pscustomobject]@{
                Id             = [int] $record.ProcessId
                ParentId       = [int] $record.ParentProcessId
                CommandLine    = [string] $record.CommandLine
                ExecutablePath = [string] $record.ExecutablePath
            }
        }
        catch {
            # An unreadable process cannot establish product ownership.
        }
    }

    $chain = New-Object System.Collections.ArrayList
    $seen = @{}
    $currentId = $ProcessId
    for ($depth = 0; $depth -lt 16; $depth++) {
        if ($seen.ContainsKey($currentId) -or -not $records.ContainsKey($currentId)) {
            break
        }
        $seen[$currentId] = $true
        $current = $records[$currentId]
        [void] $chain.Add($current)
        if ($current.ParentId -le 0 -or $current.ParentId -eq $current.Id) {
            break
        }
        $currentId = [int] $current.ParentId
    }
    return @($chain)
}

function Test-HttpSuccess {
    param([AllowNull()] $Response)

    return (
        $null -ne $Response -and
        [int] $Response.StatusCode -ge 200 -and
        [int] $Response.StatusCode -lt 300
    )
}

function Test-FrontendHttpContract {
    param([AllowNull()] $Response)

    if (-not (Test-HttpSuccess -Response $Response)) {
        return $false
    }
    $content = [string] $Response.Content
    $markers = @(
        'PlotPilot',
        'id="app"',
        'id="boot-splash"',
        'src="/src/main.ts"'
    )
    return (@($markers | Where-Object { $content.IndexOf($_, [StringComparison]::OrdinalIgnoreCase) -lt 0 }).Count -eq 0)
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

    $propertyNames = @($payload.PSObject.Properties | ForEach-Object { $_.Name })
    return (
        $propertyNames -contains "status" -and
        $propertyNames -contains "version" -and
        $propertyNames -contains "build_id" -and
        [string] $payload.status -eq "healthy" -and
        -not [string]::IsNullOrWhiteSpace([string] $payload.version) -and
        -not [string]::IsNullOrWhiteSpace([string] $payload.build_id)
    )
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

function Invoke-LauncherPlanSelfTest {
    $root = "C:\PlotPilot-self-test"
    $frontendDirectory = Join-Path $root "frontend"
    $frontendProcess = [pscustomobject]@{
        CommandLine    = ('node "{0}\node_modules\vite\bin\vite.js" "--host" "127.0.0.1" "--port" "3000" "--strictPort"' -f $frontendDirectory)
        ExecutablePath = "C:\Program Files\nodejs\node.exe"
    }
    $backendProcess = [pscustomobject]@{
        CommandLine    = ('python -m uvicorn interfaces.main:app --app-dir "{0}" --host "127.0.0.1" --port "8005" --workers "1"' -f $root)
        ExecutablePath = "C:\Python\python.exe"
    }
    $splitFrontendRecords = @(
        [pscustomobject]@{
            CommandLine    = ('node "{0}\node_modules\vite\bin\vite.js"' -f $frontendDirectory)
            ExecutablePath = "C:\Program Files\nodejs\node.exe"
        },
        [pscustomobject]@{
            CommandLine    = 'cmd "--host" "127.0.0.1" "--port" "3000" "--strictPort"'
            ExecutablePath = "C:\Windows\System32\cmd.exe"
        }
    )
    $frontendResponse = [pscustomobject]@{
        StatusCode = 200
        Content    = '<title>PlotPilot</title><div id="app"><div id="boot-splash"></div></div><script type="module" src="/src/main.ts"></script>'
    }
    $frontendTitleOnly = [pscustomobject]@{
        StatusCode = 200
        Content    = '<title>PlotPilot · 墨枢 | 作者的领航员</title>'
    }
    $backendResponse = [pscustomobject]@{
        StatusCode = 200
        Content    = '{"status":"healthy","version":"1.0.2","build_id":"build-test"}'
    }
    $backendUnhealthy = [pscustomobject]@{
        StatusCode = 200
        Content    = '{"status":"starting","version":"1.0.2","build_id":"build-test"}'
    }

    $frontendTokens = @("vite", "--host", "127.0.0.1", "--port", "3000", "--strictport")
    if (-not (Test-ProcessSourceAssociation -ProcessRecords @($frontendProcess) -RequiredPath $frontendDirectory -RequiredTokens $frontendTokens)) {
        throw "Plan self-test could not recognize the exact frontend source."
    }
    if (Test-ProcessSourceAssociation -ProcessRecords @($frontendProcess) -RequiredPath "C:\Other\frontend" -RequiredTokens $frontendTokens) {
        throw "Plan self-test accepted a foreign frontend path."
    }
    if (Test-ProcessSourceAssociation -ProcessRecords $splitFrontendRecords -RequiredPath $frontendDirectory -RequiredTokens $frontendTokens) {
        throw "Plan self-test combined path and tokens from different process records."
    }
    if (-not (Test-FrontendHttpContract -Response $frontendResponse)) {
        throw "Plan self-test rejected a valid frontend shell."
    }
    if (Test-FrontendHttpContract -Response $frontendTitleOnly) {
        throw "Plan self-test accepted a title-only frontend response."
    }
    if (-not (Test-ProcessSourceAssociation -ProcessRecords @($backendProcess) -RequiredPath $root -RequiredTokens @("uvicorn", "interfaces.main:app", "--app-dir", "--port", "8005", "--workers", "1") -RequiredPathArgument "--app-dir")) {
        throw "Plan self-test could not recognize the exact backend source."
    }
    if ((ConvertTo-QuotedWindowsArgument -Value $root) -ne ('"{0}"' -f $root)) {
        throw "Plan self-test did not quote the backend app directory."
    }
    if (-not (Test-BackendHttpContract -Response $backendResponse)) {
        throw "Plan self-test rejected a valid backend health response."
    }
    if (Test-BackendHttpContract -Response $backendUnhealthy) {
        throw "Plan self-test accepted an unhealthy backend response."
    }

    $empty = Get-LauncherPlan -BackendState "Available" -FrontendState "Available" -LauncherMode "launch"
    if (-not $empty.StartBackend -or -not $empty.StartFrontend -or -not $empty.OpenBrowser) {
        throw "Plan self-test failed the empty launch plan."
    }
    $partial = Get-LauncherPlan -BackendState "Available" -FrontendState "Reusable" -LauncherMode "launch"
    if (-not $partial.StartBackend -or $partial.StartFrontend -or -not $partial.OpenBrowser) {
        throw "Plan self-test failed the single-service reuse plan."
    }
    $both = Get-LauncherPlan -BackendState "Reusable" -FrontendState "Reusable" -LauncherMode "launch"
    if ($both.StartBackend -or $both.StartFrontend -or -not $both.OpenBrowser) {
        throw "Plan self-test failed the double-service reuse plan."
    }
    $check = Get-LauncherPlan -BackendState "Reusable" -FrontendState "Available" -LauncherMode "check"
    if ($check.StartBackend -or $check.StartFrontend -or $check.OpenBrowser) {
        throw "Plan self-test failed the side-effect-free check plan."
    }
    $occupiedRejected = $false
    try {
        [void] (Get-LauncherPlan -BackendState "Occupied" -FrontendState "Available" -LauncherMode "launch")
    }
    catch {
        $occupiedRejected = $true
    }
    if (-not $occupiedRejected) {
        throw "Plan self-test failed the occupied-port negative case."
    }

    Write-Host "[self-test] service classification and pure launcher plans passed."
    return 0
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
        [Parameter(Mandatory = $true)] $Tools,
        [Parameter(Mandatory = $true)] [string] $SourcePythonPath
    )

    $backendEntry = Join-Path $Root "interfaces\main.py"
    if (-not (Test-Path -LiteralPath $backendEntry -PathType Leaf)) {
        throw "Backend entry not found: $backendEntry"
    }

    Write-Host ("[check] Python/uvicorn: {0}" -f $Tools.Python)
    $previousPythonPath = [Environment]::GetEnvironmentVariable("PYTHONPATH", "Process")
    try {
        [Environment]::SetEnvironmentVariable("PYTHONPATH", $SourcePythonPath, "Process")
        & $Tools.Python -c "import sys, uvicorn, plotpilot_plugin_sdk; from backend.plotpilot_core.repositories.authority import P3_JOB_MIGRATIONS; print('  Python ' + sys.version.split()[0] + ' / uvicorn ' + getattr(uvicorn, '__version__', 'imported') + ' / source SDK + migrations verified')"
        if ($LASTEXITCODE -ne 0) {
            throw "Python exists but the WebUI source runtime cannot import its SDK or verified migrations."
        }
    }
    finally {
        [Environment]::SetEnvironmentVariable("PYTHONPATH", $previousPythonPath, "Process")
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

function Invoke-LocalHttpResponse {
    param([Parameter(Mandatory = $true)] [string] $Url)

    try {
        return Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 3
    }
    catch {
        return $null
    }
}

function Get-PortServiceState {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("Backend", "Frontend")]
        [string] $Role,
        [Parameter(Mandatory = $true)] [int] $Port,
        [Parameter(Mandatory = $true)] [string] $Root,
        [Parameter(Mandatory = $true)] [string] $FrontendDirectory
    )

    $ownerIds = @(
        Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
            ForEach-Object { [int] $_.OwningProcess } |
            Sort-Object -Unique
    )
    if ($ownerIds.Count -eq 0) {
        return [pscustomobject]@{
            Role      = $Role
            Port      = $Port
            State     = "Available"
            OwnerIds  = @()
            Reason    = "No listener"
        }
    }

    # Multiple owners or unreadable process metadata are ambiguous and fail closed.
    if ($ownerIds.Count -ne 1) {
        return [pscustomobject]@{
            Role      = $Role
            Port      = $Port
            State     = "Occupied"
            OwnerIds  = $ownerIds
            Reason    = "Multiple listeners"
        }
    }

    $ownerId = [int] $ownerIds[0]
    $processRecords = @(Get-ProcessSourceChain -ProcessId $ownerId)
    if ($processRecords.Count -eq 0) {
        return [pscustomobject]@{
            Role      = $Role
            Port      = $Port
            State     = "Occupied"
            OwnerIds  = $ownerIds
            Reason    = "Process source is unreadable"
        }
    }

    if ($Role -eq "Frontend") {
        $sourceMatches = Test-ProcessSourceAssociation `
            -ProcessRecords $processRecords `
            -RequiredPath $FrontendDirectory `
            -RequiredTokens @("vite", "--host", "127.0.0.1", "--port", "3000", "--strictport")
        $response = Invoke-LocalHttpResponse -Url "http://127.0.0.1:$Port/"
        $httpMatches = Test-FrontendHttpContract -Response $response
    }
    else {
        $sourceMatches = Test-ProcessSourceAssociation `
            -ProcessRecords $processRecords `
            -RequiredPath $Root `
            -RequiredTokens @("uvicorn", "interfaces.main:app", "--app-dir", "--port", "8005", "--workers", "1") `
            -RequiredPathArgument "--app-dir"
        $response = Invoke-LocalHttpResponse -Url "http://127.0.0.1:$Port/health"
        $httpMatches = Test-BackendHttpContract -Response $response
    }

    if (-not $sourceMatches -or -not $httpMatches) {
        return [pscustomobject]@{
            Role      = $Role
            Port      = $Port
            State     = "Occupied"
            OwnerIds  = $ownerIds
            Reason    = "Process source or HTTP product contract did not match"
        }
    }

    return [pscustomobject]@{
        Role      = $Role
        Port      = $Port
        State     = "Reusable"
        OwnerIds  = $ownerIds
        Reason    = "Exact product process source and HTTP contract matched"
    }
}

function Get-LauncherPlan {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("Available", "Reusable", "Occupied")]
        [string] $BackendState,
        [Parameter(Mandatory = $true)]
        [ValidateSet("Available", "Reusable", "Occupied")]
        [string] $FrontendState,
        [Parameter(Mandatory = $true)]
        [ValidateSet("launch", "check")]
        [string] $LauncherMode
    )

    if ($BackendState -eq "Occupied" -or $FrontendState -eq "Occupied") {
        throw "A required WebUI port is occupied by an unrelated, ambiguous, or unhealthy process."
    }

    return [pscustomobject]@{
        BackendState  = $BackendState
        FrontendState = $FrontendState
        StartBackend  = ($LauncherMode -eq "launch" -and $BackendState -eq "Available")
        StartFrontend = ($LauncherMode -eq "launch" -and $FrontendState -eq "Available")
        OpenBrowser   = ($LauncherMode -eq "launch")
    }
}

function Wait-HttpReady {
    param(
        [Parameter(Mandatory = $true)] [string] $Url,
        [Parameter(Mandatory = $true)] [string] $Label,
        [Parameter(Mandatory = $true)] [string] $StandardOutput,
        [Parameter(Mandatory = $true)] [string] $StandardError,
        [scriptblock] $Validator
    )

    $deadline = (Get-Date).AddSeconds(60)
    do {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 3
            if (
                $response.StatusCode -ge 200 -and
                $response.StatusCode -lt 300 -and
                ($null -eq $Validator -or [bool] (& $Validator $response))
            ) {
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
    $sourcePythonPathEntries = @((Join-Path $root "backend"), $root)
    $inheritedPythonPath = [Environment]::GetEnvironmentVariable("PYTHONPATH", "Process")
    if (-not [string]::IsNullOrWhiteSpace($inheritedPythonPath)) {
        $sourcePythonPathEntries += $inheritedPythonPath
    }
    $sourcePythonPath = $sourcePythonPathEntries -join [IO.Path]::PathSeparator
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

    $backendState = Get-PortServiceState `
        -Role "Backend" `
        -Port $backendPort `
        -Root $root `
        -FrontendDirectory $frontendDirectory
    $frontendState = Get-PortServiceState `
        -Role "Frontend" `
        -Port $frontendPort `
        -Root $root `
        -FrontendDirectory $frontendDirectory
    $plan = Get-LauncherPlan `
        -BackendState $backendState.State `
        -FrontendState $frontendState.State `
        -LauncherMode $LauncherMode
    Write-Host ("[check] Backend port {0}: {1} ({2})" -f $backendPort, $backendState.State, $backendState.Reason)
    Write-Host ("[check] Frontend port {0}: {1} ({2})" -f $frontendPort, $frontendState.State, $frontendState.Reason)

    if (
        $LauncherMode -eq "launch" -and
        -not $plan.StartBackend -and
        -not $plan.StartFrontend
    ) {
        $browserUrl = "http://127.0.0.1:$frontendPort/"
        Write-Host ("[ready] Opening the default browser: {0}" -f $browserUrl)
        Start-Process -FilePath $browserUrl | Out-Null
        Write-Host "[done] The browser WebUI is running; existing backend and frontend were reused."
        return 0
    }

    $tools = Resolve-SourceTools -Root $root -FrontendDirectory $frontendDirectory
    Test-SourceEnvironment `
        -Root $root `
        -FrontendDirectory $frontendDirectory `
        -DataDirectory $dataDirectory `
        -Tools $tools `
        -SourcePythonPath $sourcePythonPath

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
        $backendAppDirectoryArgument = ConvertTo-QuotedWindowsArgument -Value $root
        $backendEnvironment = @{
            PLOTPILOT_PROD_DATA_DIR = $dataDirectory
            DISABLE_AUTO_DAEMON     = "1"
            PYTHONIOENCODING        = "utf-8"
            PYTHONUNBUFFERED        = "1"
            PYTHONPATH              = $sourcePythonPath
        }
        if ($null -eq [Environment]::GetEnvironmentVariable("VECTOR_STORE_ENABLED")) {
            $backendEnvironment["VECTOR_STORE_ENABLED"] = "false"
        }

        if ($plan.StartBackend) {
            Write-Host ("[start] Backend: 127.0.0.1:{0}" -f $backendPort)
            [void] (Start-OwnedGuard `
                -Name "backend" `
                -FilePath $tools.Python `
                -Arguments @(
                    "-m", "uvicorn", "interfaces.main:app",
                    "--app-dir", $backendAppDirectoryArgument,
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
        }

        if ($plan.StartFrontend) {
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
        }

        if ($plan.StartBackend) {
            Wait-HttpReady `
                -Url "http://127.0.0.1:$backendPort/health" `
                -Label "Backend" `
                -StandardOutput $backendOutput `
                -StandardError $backendError `
                -Validator { param($response) Test-BackendHttpContract -Response $response }
        }
        if ($plan.StartFrontend) {
            Wait-HttpReady `
                -Url "http://127.0.0.1:$frontendPort/" `
                -Label "Frontend" `
                -StandardOutput $frontendOutput `
                -StandardError $frontendError `
                -Validator { param($response) Test-FrontendHttpContract -Response $response }
        }

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
        "self-test-plan" {
            exit (Invoke-LauncherPlanSelfTest)
        }
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
