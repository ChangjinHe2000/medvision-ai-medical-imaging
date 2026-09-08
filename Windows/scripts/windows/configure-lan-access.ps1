[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$FrontendPort = 5173,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$ruleName = 'MedVision LAN Frontend Access'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an elevated PowerShell window.'
}

if ($Remove) {
    Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    Write-Host 'The MedVision LAN firewall rule was removed.'
    exit 0
}

$route = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' |
    Sort-Object RouteMetric, InterfaceMetric |
    Select-Object -First 1
if ($null -eq $route) { throw 'No active IPv4 default route was found.' }
$profile = Get-NetConnectionProfile -InterfaceIndex $route.InterfaceIndex
if ($profile.NetworkCategory -ne 'Private') {
    throw 'The active network is not Private. Confirm it is trusted and change it manually before continuing.'
}

Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $ruleName `
    -Description 'Allow trusted local subnet to access only the MedVision Vue frontend.' `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort $FrontendPort `
    -Profile Private -RemoteAddress LocalSubnet | Out-Null

$addresses = Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $route.InterfaceIndex |
    Where-Object { $_.IPAddress -notlike '169.254.*' } |
    Select-Object -ExpandProperty IPAddress
Write-Host "Allowed LocalSubnet on the Private profile to TCP $FrontendPort only."
foreach ($address in $addresses) { Write-Host "LAN URL: http://${address}:$FrontendPort/" }
Write-Host "Undo: .\configure-lan-access.ps1 -Remove"
