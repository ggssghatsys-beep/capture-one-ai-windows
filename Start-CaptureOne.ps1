#requires -Version 7.6
[CmdletBinding()]
param([switch]$Restart,[string]$CaptureOneExe,[switch]$NoScopes,
      [switch]$NoBridgeUpdate,[switch]$CheckBridgeUpdates)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'internal\resolve.ps1')
. (Join-Path $PSScriptRoot 'internal\project.ps1')
$installation=Get-C1Installation -CaptureOneExe $CaptureOneExe
$existing=@(Get-Process -Name CaptureOne -ErrorAction SilentlyContinue)
if($existing.Count){
 if(-not $Restart){Write-Output 'C1 is running. No bridge update or build was performed.';return}
 if(-not $existing[0].CloseMainWindow()){throw 'C1 refused a normal close.'}
 if(-not $existing[0].WaitForExit(20000)){throw 'C1 remains open; no forced termination.'}
}
$basePython=Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if(-not(Test-Path -LiteralPath $basePython)){throw 'Run Install.ps1 first.'}
$prepare=Join-Path $PSScriptRoot 'scripts\prepare_bridge.py'
$project=Get-C1RuntimeProject -Project $PSScriptRoot
$frameworks=@($installation.runtime.frameworks)+@($installation.runtime.framework) | Where-Object {$_}
$core=$frameworks | Where-Object name -eq 'Microsoft.NETCore.App'
$hostJson=@{platform='windows';architecture=$installation.architecture;
 capture_one_version=$installation.version;runtime_major=([version]$core.version).Major} | ConvertTo-Json -Compress
if(-not $NoBridgeUpdate){
 $arguments=@($prepare,'--action','prepare','--project',$PSScriptRoot,'--host-json',$hostJson)
 if($CheckBridgeUpdates){$arguments+='--force'}
 $prepared=& $basePython @arguments
 if($LASTEXITCODE -ne 0){
  $null=Get-C1Adapter -Installation $installation -Project $project
  Write-Warning 'Update check deferred; using the matched local bridge package.'
 }
 else {
  $result=$prepared | ConvertFrom-Json
  if($result.status -eq 'prepared'){
   $candidate=$result.project
   $python=Join-Path $candidate '.venv\Scripts\python.exe'
   if(-not(Test-Path -LiteralPath $python)){
    & $basePython -m venv (Join-Path $candidate '.venv')
    if($LASTEXITCODE -ne 0){throw 'Prepared environment could not be created; current package preserved.'}
   }
   $currentPython=Join-Path $project '.venv\Scripts\python.exe'
   $extras=& $currentPython -c "import importlib.util; print(','.join(n for n,m in [('ai','openai'),('scopes','av')] if importlib.util.find_spec(m)))"
   if($LASTEXITCODE -ne 0){throw 'Cannot determine the installed optional features.'}
   $package=if($extras){$candidate+'['+$extras+']'}else{$candidate}
   & $python -m pip install -r (Join-Path $candidate 'requirements-runtime.txt') $package
   if($LASTEXITCODE -ne 0){throw 'Prepared dependencies failed; current package preserved.'}
   $null=& $basePython $prepare --action activate --project $PSScriptRoot --host-json $hostJson --candidate $candidate
   if($LASTEXITCODE -ne 0){throw 'Package activation deferred; current bridge preserved.'}
   $project=$candidate
  }
 }
}
$null=Get-C1Adapter -Installation $installation -Project $project
& (Join-Path $project 'internal\start.ps1') -CaptureOneExe $installation.executable
if(-not $NoScopes){& (Join-Path $project 'Scopes.ps1') -IfEnabled}
