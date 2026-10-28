#requires -Version 7.6
[CmdletBinding()]
param([switch]$Restart, [string]$CaptureOneExe='C:\Program Files\Capture One\Capture One\CaptureOne.exe')
$ErrorActionPreference='Stop'
& (Join-Path $PSScriptRoot 'internal\start.ps1') -Restart:$Restart -CaptureOneExe $CaptureOneExe
