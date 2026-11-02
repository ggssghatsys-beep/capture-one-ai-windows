# Shared by Windows entry points. Resolving a project does not start an owner.
function Get-C1RuntimeProject {
 param([Parameter(Mandatory)][string]$Project)
 $pointer=Join-Path $Project 'config\bridge-selected.local.json'
 if(-not(Test-Path -LiteralPath $pointer)){return $Project}
 $python=Join-Path $Project '.venv\Scripts\python.exe'
 if(-not(Test-Path -LiteralPath $python)){throw 'Run Install.ps1 before resolving a prepared bridge package.'}
 $result=& $python (Join-Path $Project 'scripts\prepare_bridge.py') --action active --project $Project
 if($LASTEXITCODE -ne 0){throw 'Selected bridge package failed verification; inspect the prepared package.'}
 ($result | ConvertFrom-Json).project
}
