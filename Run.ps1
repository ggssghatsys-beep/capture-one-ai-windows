#requires -Version 7.6
$pythonExe=Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if(-not (Test-Path -LiteralPath $pythonExe)){throw 'Run Install.ps1 first.'}
& $pythonExe -m c1ai @args
exit $LASTEXITCODE
