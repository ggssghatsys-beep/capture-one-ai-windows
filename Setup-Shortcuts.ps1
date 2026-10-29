#requires -Version 7.6
[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
if(-not $IsWindows){throw 'Desktop launchers are supported on Windows only.'}
$desktop=[Environment]::GetFolderPath('DesktopDirectory')
$shell=New-Object -ComObject WScript.Shell
$pwsh=(Get-Command pwsh -ErrorAction Stop).Source
$entries=@(
 @{Name='Capture One + AI';Script='Start-CaptureOne.ps1'},
 @{Name='Capture One 图像分析';Script='Scopes.ps1'}
)
foreach($entry in $entries){
 $script=Join-Path $PSScriptRoot $entry.Script
 $path=Join-Path $desktop ($entry.Name+'.lnk')
 $link=$shell.CreateShortcut($path)
 if((Test-Path -LiteralPath $path) -and -not $link.Arguments.Contains('"'+$script+'"')){
  throw "Existing shortcut belongs to another installation; preserved: $path"
 }
 $link.TargetPath=$pwsh
 $link.Arguments='-NoProfile -WindowStyle Hidden -File "'+$script+'"'
 $link.WorkingDirectory=$PSScriptRoot
 $link.WindowStyle=7
 $link.Description=if($entry.Script -eq 'Scopes.ps1'){'打开或重用图像分析窗口，不重启 Capture One'}else{'通过原生桥接启动 C1，并按设置打开图像分析窗口'}
 $icon='C:\Program Files\Capture One\Capture One\CaptureOne.exe'
 if(Test-Path -LiteralPath $icon){$link.IconLocation=$icon+',0'}
 $link.Save()
 Write-Output $path
}
