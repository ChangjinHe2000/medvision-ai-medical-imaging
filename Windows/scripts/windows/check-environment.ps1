[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot 'launcher-common.ps1')

if ($PSVersionTable.PSVersion.Major -lt 7) {
    Write-Warning "PowerShell $($PSVersionTable.PSVersion) detected. PowerShell 7 is recommended; checks will continue."
}
$exitCode = Invoke-MedVisionLauncher -Command 'check' -Arguments @('--json')
exit $exitCode
