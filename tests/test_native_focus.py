"""PowerShell routing tests with a synthetic monitor; no desktop or C1 access."""
import json
import os
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell AST verification")


def test_numeric_focus_monitor_routes_without_requiring_background(tmp_path):
    script = r'''
param([string]$BackgroundSource,[string]$UiaSource)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
$tokens=$null;$errors=$null
$background=[Management.Automation.Language.Parser]::ParseFile($BackgroundSource,[ref]$tokens,[ref]$errors)
if($errors.Count -ne 0){throw 'Background source does not parse'}
$uia=[Management.Automation.Language.Parser]::ParseFile($UiaSource,[ref]$tokens,[ref]$errors)
if($errors.Count -ne 0){throw 'UIA source does not parse'}
# Define a fully synthetic monitor. No imported native functions execute.
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
public class C1FocusWatch {
    public class Sample { public long foreground=10,focus=20,active=10; public uint pid=77; public bool readable=true; }
    public static bool DefaultHooks=true,DefaultReadable=true,DefaultUnchanged=true;
    public static C1FocusWatch Last;
    public bool HooksInstalled,Unchanged,Disposed;
    public Sample Before=new Sample(),After=new Sample();
    public int Samples=1;
    public List<int> Changes=new List<int>();
    public C1FocusWatch(){HooksInstalled=DefaultHooks;Before.readable=DefaultReadable;Unchanged=DefaultUnchanged;Last=this;}
    public void Dispose(){Disposed=true;}
}
'@
function Fail($Code,$Message,$Detail){throw $Code}
foreach($name in @('Start-FocusWatch','Start-BackgroundWatch','Watch-Report','Assert-BackgroundWatch')){
    $found=@($background.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name},$true))
    if($found.Count -ne 1){throw ('Missing function '+$name)}
    Invoke-Expression $found[0].Extent.Text
}
$script:WindowInfo=@{pid=77}
$watch=Start-FocusWatch
if($watch.Before.pid -ne 77 -or $watch.Disposed){throw 'Foreground numeric monitor was rejected'}
Assert-BackgroundWatch $watch
$watch.Dispose()
$oldRejected=$false
try{Start-BackgroundWatch | Out-Null}catch{if($_.Exception.Message -ne 'BACKGROUND_REQUIRED'){throw};$oldRejected=$true}
if(-not $oldRejected -or -not [C1FocusWatch]::Last.Disposed){throw 'Old background-only contract was lost'}
[C1FocusWatch]::DefaultHooks=$false
$unavailable=$false
try{Start-FocusWatch | Out-Null}catch{if($_.Exception.Message -ne 'FOCUS_MONITOR_UNAVAILABLE'){throw};$unavailable=$true}
if(-not $unavailable -or -not [C1FocusWatch]::Last.Disposed){throw 'Unavailable monitor accepted'}
[C1FocusWatch]::DefaultHooks=$true
[C1FocusWatch]::DefaultUnchanged=$false
$watch=Start-FocusWatch;$changed=$false
try{Assert-BackgroundWatch $watch}catch{if($_.Exception.Message -ne 'FOCUS_CHANGED'){throw};$changed=$true}
if(-not $changed){throw 'Focus change was accepted'}
$watch.Dispose()
# Evaluate only the actual module-loading condition, never the UIA program.
$loaders=@($uia.FindAll({param($node)
    $node -is [Management.Automation.Language.IfStatementAst] -and
    $node.Clauses.Count -eq 1 -and
    $node.Clauses[0].Item2.Extent.Text -match "^\{\s*\.\s*\(Join-Path.*'background\.ps1'"
},$true))
if($loaders.Count -ne 1){throw 'Could not locate unique helper-loading condition'}
$condition=[scriptblock]::Create($loaders[0].Clauses[0].Item1.Extent.Text)
$script:BackgroundOnly=$false
foreach($operation in @('set','set_many')){if(-not (& $condition)){throw ('Missing focus helper load for '+$operation)}}
# The actual numeric set branch must not activate or send keyboard/mouse input.
$switches=@($uia.FindAll({param($node) $node -is [Management.Automation.Language.SwitchStatementAst] -and $node.Condition.Extent.Text -eq '$operation'},$true))
$setBlocks=@($switches | ForEach-Object {$_.Clauses | Where-Object {$_.Item1.Value -eq 'set'}})
if($setBlocks.Count -ne 1){throw 'Could not locate unique native set branch'}
$block=$setBlocks[0].Item2.Extent.Text
if($block -notmatch 'Start-FocusWatch' -or $block -notmatch 'parameterWatch.Dispose\(\)') {throw 'Numeric monitor lifecycle missing'}
if($block -match 'SetForegroundWindow|\.SetFocus\(|SendInput|SendKeys'){throw 'Native numeric set unexpectedly activates or synthesizes input'}
if(-not $block.Contains("Check-Guard (Field `$request 'post_guard')")){throw 'Native numeric target post-guard was removed'}
# Execute only the real post-dispatch focus policy block against a changed
# synthetic monitor. Setter/target/readback facts already succeeded here.
$postChecks=@($setBlocks[0].Item2.FindAll({param($node)
    $node -is [Management.Automation.Language.IfStatementAst] -and
    $node.Clauses.Count -eq 1 -and
    $node.Clauses[0].Item1.Extent.Text -match '\$parameterWatch' -and
    $node.Clauses[0].Item2.Extent.Text -match 'Watch-Report \$parameterWatch'
},$true))
if($postChecks.Count -ne 1){throw 'Could not locate unique post-dispatch focus policy'}
$parameterWatch=New-Object C1FocusWatch
$result=@{accepted=$true;verified=$true;identity_verified=$true;render_verified=$false}
$script:BackgroundOnly=$false
Invoke-Expression $postChecks[0].Extent.Text
if(-not $result.accepted -or -not $result.verified -or -not $result.identity_verified -or $result.focus.unchanged){throw 'Non-strict focus side effect erased successful numeric/target evidence'}
if($result.focus_policy -ne 'native_no_explicit_activation'){throw 'Missing non-strict focus policy label'}
$script:BackgroundOnly=$true;$strictRejected=$false
try{Invoke-Expression $postChecks[0].Extent.Text}catch{if($_.Exception.Message -ne 'FOCUS_CHANGED'){throw};$strictRejected=$true}
if(-not $strictRejected){throw 'Strict background policy accepted changed focus'}
$parameterWatch.Dispose()
@{foreground_numeric_allowed=$true;legacy_background_rejected=$true;missing_hooks_rejected=$true;focus_change_rejected=$true;helper_loaded_for_numeric=$true;numeric_has_no_activation=$true;nonstrict_change_preserves_result=$true;strict_change_rejected=$true} | ConvertTo-Json -Compress
'''
    path = tmp_path / "test-focus.ps1"
    path.write_text(script, encoding="utf-8-sig")
    project = Path(__file__).resolve().parents[1]
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    completed = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                "-File", str(path), str(project / "c1ai/native/background.ps1"),
                                str(project / "c1ai/native/uia.ps1")], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    result = json.loads(completed.stdout.decode("utf-8-sig"))
    assert all(result.values())
