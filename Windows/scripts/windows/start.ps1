[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$Lan,
    [ValidateRange(10, 600)]
    [int]$StartupTimeout = 90
)

. (Join-Path $PSScriptRoot 'launcher-common.ps1')

$arguments = @('--startup-timeout', [string]$StartupTimeout)
if ($NoBrowser) { $arguments += '--no-browser' }
if ($Lan) { $arguments += '--lan' }
$exitCode = Invoke-MedVisionLauncher -Command 'start' -Arguments $arguments
exit $exitCode
