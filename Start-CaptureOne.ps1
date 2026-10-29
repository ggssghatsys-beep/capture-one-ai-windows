#requires -Version 7.6
[CmdletBinding()]
param([switch]$Restart, [string]$CaptureOneExe='C:\Program Files\Capture One\Capture One\CaptureOne.exe',
      [switch]$NoScopes)
$ErrorActionPreference='Stop'
& (Join-Path $PSScriptRoot 'internal\start.ps1') -Restart:$Restart -CaptureOneExe $CaptureOneExe
if(-not $NoScopes) { & (Join-Path $PSScriptRoot 'Scopes.ps1') -IfEnabled }
