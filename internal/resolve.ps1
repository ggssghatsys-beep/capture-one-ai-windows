# Shared discovery only. Dot-sourcing this file does not start, build or connect to C1.
function Get-C1Installation {
 param([string]$CaptureOneExe)
 $running=@(Get-Process -Name CaptureOne -ErrorAction SilentlyContinue)
 if($running.Count -gt 1){throw 'Multiple C1 processes; choose one instance before starting the bridge.'}
 if($CaptureOneExe){$paths=@($CaptureOneExe)}
 elseif($running.Count -eq 1){
  if(-not $running[0].Path){throw 'Cannot read the running C1 executable path.'}
  $paths=@($running[0].Path)
 }
 else {
  $paths=@()
  foreach($key in @('HKCU:\Software\Microsoft\Windows\CurrentVersion\App Paths\CaptureOne.exe',
                    'HKLM:\Software\Microsoft\Windows\CurrentVersion\App Paths\CaptureOne.exe')){
   if(Test-Path -LiteralPath $key){$paths+=@((Get-Item -LiteralPath $key).GetValue(''))}
  }
  foreach($key in @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
                    'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
                    'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*')){
   foreach($item in @(Get-ItemProperty -Path $key -ErrorAction SilentlyContinue)){
    if($item.DisplayName -like 'Capture One*' -and $item.InstallLocation){
     $paths+=Join-Path $item.InstallLocation 'CaptureOne.exe'
    }
    if($item.DisplayName -like 'Capture One*' -and $item.DisplayIcon){
     $iconPath=([string]$item.DisplayIcon -replace ',\s*-?\d+\s*$','').Trim().Trim('"')
     if([IO.Path]::GetFileName($iconPath) -ieq 'CaptureOne.exe'){$paths+=$iconPath}
    }
   }
  }
  if($env:ProgramFiles){$paths+=Join-Path $env:ProgramFiles 'Capture One\Capture One\CaptureOne.exe'}
 }
 $paths=@($paths | Where-Object {$_ -and (Test-Path -LiteralPath $_ -PathType Leaf)} |
          ForEach-Object {(Resolve-Path -LiteralPath $_).ProviderPath} | Sort-Object -Unique)
 if($paths.Count -eq 0){throw 'Capture One executable not found. Supply -CaptureOneExe with its full path.'}
 if($paths.Count -gt 1){throw ('Multiple C1 installations found. Supply -CaptureOneExe to choose one: '+($paths -join '; '))}
 $exe=$paths[0]
 if([IO.Path]::GetFileName($exe) -ine 'CaptureOne.exe'){throw 'Expected CaptureOne.exe.'}
 if($running.Count -and $running[0].Path -ine $exe){throw 'The chosen installation differs from the running C1 instance.'}
 $info=[Diagnostics.FileVersionInfo]::GetVersionInfo($exe)
 $version='{0}.{1}.{2}.{3}' -f $info.FileMajorPart,$info.FileMinorPart,$info.FileBuildPart,$info.FilePrivatePart
 $stream=[IO.File]::OpenRead($exe)
 try {
  $reader=[IO.BinaryReader]::new($stream)
  if($reader.ReadUInt16() -ne 0x5a4d){throw 'Invalid C1 executable header.'}
  $stream.Position=0x3c;$offset=$reader.ReadInt32()
  if($offset -lt 64 -or $offset -gt ($stream.Length-6)){throw 'Invalid PE header offset.'}
  $stream.Position=$offset
  if($reader.ReadUInt32() -ne 0x4550){throw 'Invalid C1 PE signature.'}
  $architecture=switch($reader.ReadUInt16()){0x8664 {'x64'} 0xaa64 {'arm64'} default {throw 'Unsupported C1 executable architecture.'}}
 } finally {$stream.Dispose()}
 $runtimePath=Join-Path (Split-Path -Parent $exe) 'CaptureOne.runtimeconfig.json'
 if(-not(Test-Path -LiteralPath $runtimePath)){throw 'C1 runtime configuration not found.'}
 $runtime=Get-Content -LiteralPath $runtimePath -Raw -Encoding UTF8 | ConvertFrom-Json
 if(-not $runtime.runtimeOptions.tfm){throw 'C1 runtime configuration has no target framework.'}
 [pscustomobject]@{executable=$exe;version=$version;architecture=$architecture;
  platform='windows';runtime=$runtime.runtimeOptions;running_pid=if($running.Count){$running[0].Id}else{$null}}
}

function Get-C1Adapter {
 param([Parameter(Mandatory)]$Installation,[Parameter(Mandatory)][string]$Project)
 $matrix=Get-Content -LiteralPath (Join-Path $Project 'config\bridge-compatibility.json') -Raw -Encoding UTF8 | ConvertFrom-Json
 if($matrix.schema -ne 1 -or $matrix.protocol -ne 1 -or $matrix.color_policy -ne 'native-srgb-v1'){
  throw 'Unsupported bridge compatibility or native color policy.'
 }
 $adapters=@($matrix.adapters | Where-Object {$_.platform -eq 'windows' -and
  $_.architecture -eq $Installation.architecture -and $_.capture_one_version -eq $Installation.version})
 if($adapters.Count -ne 1){throw "No verified bridge adapter for C1 $($Installation.version) / $($Installation.architecture). Prepare a matching release first; version gates remain enabled."}
 $adapter=$adapters[0]
 $frameworks=@($Installation.runtime.frameworks)+@($Installation.runtime.framework) | Where-Object {$_}
 $core=@($frameworks | Where-Object name -eq 'Microsoft.NETCore.App')
 if($core.Count -ne 1 -or ([version]$core[0].version).Major -ne $adapter.runtime_major){throw 'C1 runtime does not match the adapter.'}
 $qualification=Get-Content -LiteralPath (Join-Path $Project $adapter.profile) -Raw -Encoding UTF8 | ConvertFrom-Json
 if($qualification.qualified_version -ne $Installation.version){throw 'Adapter and qualification profile disagree.'}
 $adapter
}

function Get-C1Frameworks {
 param([Parameter(Mandatory)]$Installation)
 $roots=@()
 $arch=$Installation.architecture
 foreach($name in @('DOTNET_ROOT_'+$arch.ToUpperInvariant(),'DOTNET_ROOT')){
  $value=[Environment]::GetEnvironmentVariable($name)
  if($value){$roots+=$value}
 }
 foreach($registry in @("HKLM:\SOFTWARE\dotnet\Setup\InstalledVersions\$arch",
                        "HKLM:\SOFTWARE\dotnet\Setup\InstalledVersions\$arch\sharedhost")){
  if(Test-Path -LiteralPath $registry){
   $location=(Get-Item -LiteralPath $registry).GetValue('InstallLocation')
   if($location){$roots+=$location}
  }
 }
 $dotnet=Get-Command dotnet -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
 if($dotnet){$roots+=Split-Path -Parent $dotnet.Source}
 if($env:ProgramFiles){$roots+=Join-Path $env:ProgramFiles 'dotnet'}
 $wanted=@($Installation.runtime.frameworks)+@($Installation.runtime.framework) | Where-Object {$_}
 # Use one dotnet installation; mixing framework trees produces invalid bridge builds.
 foreach($root in @($roots | Where-Object {$_ -and (Test-Path -LiteralPath $_ -PathType Container)} | Select-Object -Unique)){
  $found=@();$complete=$true
  foreach($framework in $wanted){
   $minimum=[version]$framework.version
   $shared=Join-Path $root ('shared\'+$framework.name)
   $versions=if(Test-Path -LiteralPath $shared){@(Get-ChildItem -LiteralPath $shared -Directory | Where-Object {
    $parsed=$null
    [version]::TryParse($_.Name,[ref]$parsed) -and $parsed.Major -eq $minimum.Major -and
     $parsed.Minor -eq $minimum.Minor -and $parsed -ge $minimum
   } | Sort-Object {[version]$_.Name} -Descending)}else{@()}
   if(-not $versions.Count){$complete=$false;break}
   $found+=@([pscustomobject]@{name=$framework.name;version=$versions[0].Name;path=$versions[0].FullName})
  }
  if($complete -and $found.Count){return $found}
 }
 throw 'Matching C1 .NET frameworks were not found. Install the required runtime for this architecture.'
}
