#requires -Version 7.6
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'internal\project.ps1')
$project=Get-C1RuntimeProject -Project $PSScriptRoot
$python=Join-Path $project '.venv\Scripts\python.exe'
if(-not(Test-Path -LiteralPath $python)){throw 'Run Install.ps1 first.'}
# Stdout must contain MCP frames only; project resolution prints no banners.
Push-Location -LiteralPath $project
try { & $python -m c1ai mcp; $resultCode=$LASTEXITCODE }
finally { Pop-Location }
exit $resultCode
