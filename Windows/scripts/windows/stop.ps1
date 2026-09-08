[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot 'launcher-common.ps1')
$exitCode = Invoke-MedVisionLauncher -Command 'stop'
exit $exitCode
