#requires -Version 7.6
param([string]$File,[ValidateSet('waveform','parade','histogram','vectorscope')][string]$Mode,
      [switch]$IfEnabled,[ValidateSet('on','off')][string]$AutoStart,[switch]$ConfigureOnly)
$ErrorActionPreference='Stop'
$python=Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if(-not (Test-Path -LiteralPath $python)){throw 'Run Install.ps1 and install the scopes extra first.'}
$consolePython=Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $consolePython -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('PySide6') and importlib.util.find_spec('av') else 1)"
if($LASTEXITCODE -ne 0){
 if($IfEnabled){return} # Existing MCP-only installations keep working.
 throw 'Window dependencies missing. Run Install.ps1 -WithScopes -EnvironmentOnly.'
}
$arguments=@('-m','c1ai.scopes_window')
if($Mode){$arguments+=@('--mode',$Mode)}
if($IfEnabled){$arguments+='--if-enabled'}
if($AutoStart){$arguments+=@('--autostart',$AutoStart)}
if($ConfigureOnly){$arguments+='--configure-only'}
if($File){$arguments+=@('--file',('"'+$File+'"'))}
Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
