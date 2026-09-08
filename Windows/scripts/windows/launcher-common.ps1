Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-MedVisionRoot {
    return (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
}

function Read-MedVisionLocalConfig {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    $values = @{}
    $path = Join-Path $ProjectRoot '.env.local'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return $values
    }
    $lineNumber = 0
    foreach ($rawLine in Get-Content -LiteralPath $path -Encoding UTF8) {
        $lineNumber++
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        $index = $line.IndexOf('=')
        if ($index -lt 1) { throw ".env.local line $lineNumber is invalid" }
        $key = $line.Substring(0, $index).Trim()
        if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw ".env.local key on line $lineNumber is invalid" }
        $value = $line.Substring($index + 1).Trim()
        if ($value.Length -ge 2 -and (($value[0] -eq '"' -and $value[-1] -eq '"') -or ($value[0] -eq "'" -and $value[-1] -eq "'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$key] = $value
    }
    return $values
}

function Get-MedVisionLauncherPython {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    $values = Read-MedVisionLocalConfig -ProjectRoot $ProjectRoot
    $configured = $env:PPGL_PYTHON_BIN
    if (-not $configured -and $values.ContainsKey('PPGL_PYTHON_BIN')) { $configured = $values['PPGL_PYTHON_BIN'] }
    if ($configured) {
        if ($configured -notmatch '[\\/]') {
            $resolvedCommand = Get-Command -Name $configured -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($null -ne $resolvedCommand) { return $resolvedCommand.Source }
        }
        $candidate = $configured
        if (-not [IO.Path]::IsPathRooted($candidate)) { $candidate = Join-Path $ProjectRoot $candidate }
        $candidate = [IO.Path]::GetFullPath($candidate)
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        throw "PPGL_PYTHON_BIN does not exist: $candidate"
    }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) { return $command.Source }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $command) { return $command.Source }
    throw 'Python was not found. Configure PPGL_PYTHON_BIN.'
}

function Invoke-MedVisionLauncher {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [string[]]$Arguments = @()
    )
    $projectRoot = Get-MedVisionRoot
    $python = Get-MedVisionLauncherPython -ProjectRoot $projectRoot
    $launcher = Join-Path $projectRoot 'start_system.py'
    & $python $launcher $Command @Arguments | Out-Host
    $nativeExitCode = $LASTEXITCODE
    return $nativeExitCode
}
