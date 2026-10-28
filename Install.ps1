#requires -Version 7.6
[CmdletBinding()]
param([string]$Python='python', [string]$SessionPath, [switch]$EnvironmentOnly)
$ErrorActionPreference='Stop'
if(-not $IsWindows){throw 'This package requires Windows.'}
$project=$PSScriptRoot
$venv=Join-Path $project '.venv'
$pythonExe=Join-Path $venv 'Scripts\python.exe'
if(-not (Test-Path -LiteralPath $pythonExe)) {
 & $Python -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"; assert sys.maxsize > 2**32, "64-bit Python required"'
 if($LASTEXITCODE -ne 0){throw 'Python prerequisite check failed.'}
 & $Python -m venv $venv
 if($LASTEXITCODE -ne 0){throw 'Could not create the local Python environment.'}
}
& $pythonExe -m pip install -r (Join-Path $project 'requirements-runtime.txt') $project
if($LASTEXITCODE -ne 0){throw 'Installation failed; no C1 configuration was changed.'}
$server=@{mcpServers=@{'capture-one-ai'=@{command=$pythonExe;args=@('-m','c1ai','mcp');env=@{PYTHONUTF8='1';PYTHONUNBUFFERED='1'}}}}
$server|ConvertTo-Json -Depth 8|Set-Content -LiteralPath (Join-Path $project 'config\mcp-server.local.json') -Encoding utf8NoBOM
if(-not $EnvironmentOnly) {
 $configure=@((Join-Path $project 'scripts\configure.py'))
 if($SessionPath){$configure+=@('--session',$SessionPath)}
 & $pythonExe @configure
 if($LASTEXITCODE -ne 0){throw 'Environment installed; Session configuration needs attention. See docs/INSTALL.md.'}
}
Write-Output 'Installed. MCP config: config/mcp-server.local.json'
Write-Output 'Next: start Capture One through Start-CaptureOne.ps1, then run .\Run.ps1 context --compact.'
