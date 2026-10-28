#requires -Version 7.6
param([switch]$Restart,[string]$CaptureOneExe='C:\Program Files\Capture One\Capture One\CaptureOne.exe')
$ErrorActionPreference='Stop'
if(-not (Test-Path -LiteralPath $CaptureOneExe -PathType Leaf)){throw 'Capture One executable not found; supply -CaptureOneExe.'}
$hostVersion=(Get-Item -LiteralPath $CaptureOneExe).VersionInfo.FileVersion
if($hostVersion -ne '16.8.6.3784'){throw "Unqualified Capture One version: $hostVersion. This release supports 16.8.6.3784 only."}
$project=Split-Path $PSScriptRoot -Parent
$state=Join-Path $env:LOCALAPPDATA 'CaptureOneAI'
New-Item -ItemType Directory -Path $state -Force | Out-Null
$tokenFile=Join-Path $state 'internal-token.txt'
if(-not(Test-Path -LiteralPath $tokenFile)) {
 [IO.File]::WriteAllText($tokenFile,[Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLower())
}
$acl=[Security.AccessControl.FileSecurity]::new()
$acl.SetAccessRuleProtection($true,$false)
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','Allow'))
[IO.FileSystemAclExtensions]::SetAccessControl([IO.FileInfo]::new($tokenFile),$acl)
$bootstrap=& (Join-Path $PSScriptRoot 'build.ps1') -Bootstrap
$null=& (Join-Path $PSScriptRoot 'build.ps1')
$existing=@(Get-Process -Name CaptureOne -ErrorAction SilentlyContinue)
if($existing.Count -gt 1){throw 'Multiple C1 processes; choose the intended instance first'}
if($existing.Count -eq 1) {
 if(-not $Restart){Write-Output 'C1 is already running. Use -Restart only when loading a changed bootstrap.';return}
 if(-not $existing[0].CloseMainWindow()){throw 'C1 refused a normal close'}
 if(-not $existing[0].WaitForExit(20000)){throw 'C1 remains open; not forcing termination'}
}
$exe=(Resolve-Path -LiteralPath $CaptureOneExe).Path
$launch=[Diagnostics.ProcessStartInfo]::new($exe)
$launch.UseShellExecute=$false
$launch.WorkingDirectory=Split-Path $exe -Parent
$inherited=[Environment]::GetEnvironmentVariable('DOTNET_STARTUP_HOOKS')
$launch.Environment['DOTNET_STARTUP_HOOKS']=if($inherited){$inherited+';'+$bootstrap}else{$bootstrap}
$process=[Diagnostics.Process]::Start($launch)
[pscustomobject]@{pid=$process.Id;bootstrap=$bootstrap;installation_modified=$false;global_environment_modified=$false}
