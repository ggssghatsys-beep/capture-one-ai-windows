#requires -Version 7.6
. (Join-Path $PSScriptRoot 'internal\project.ps1')
$project=Get-C1RuntimeProject -Project $PSScriptRoot
$pythonExe=Join-Path $project '.venv\Scripts\python.exe'
if(-not (Test-Path -LiteralPath $pythonExe)){throw 'Run Install.ps1 first.'}
Push-Location -LiteralPath $project
try { & $pythonExe -m c1ai @args; $resultCode=$LASTEXITCODE }
finally { Pop-Location }
exit $resultCode
