#requires -Version 7.6
[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
$python=Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$consolePython=Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if(-not (Test-Path -LiteralPath $python)){throw 'Run Install.ps1 -WithAI first.'}
& $consolePython -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('PySide6') and importlib.util.find_spec('openai') else 1)"
if($LASTEXITCODE -ne 0){throw 'Run Install.ps1 -WithAI -EnvironmentOnly first.'}
Start-Process -FilePath $python -ArgumentList @('-m','c1ai.ai_window') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
