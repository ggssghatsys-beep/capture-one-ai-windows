#requires -Version 7.6
param([switch]$Bootstrap,[string]$CaptureOneExe,[switch]$PrepareOnly)
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
. (Join-Path $root 'resolve.ps1')
$installation=Get-C1Installation -CaptureOneExe $CaptureOneExe
$adapter=Get-C1Adapter -Installation $installation -Project (Split-Path -Parent $root)
$frameworks=@(Get-C1Frameworks -Installation $installation)
$core=($frameworks | Where-Object name -eq 'Microsoft.NETCore.App').path
$desktop=($frameworks | Where-Object name -eq 'Microsoft.WindowsDesktop.App').path
if(-not $core -or -not $desktop){throw 'The adapter requires the C1 core and Windows Desktop frameworks.'}
$refs=@(Get-ChildItem $core,$desktop -Filter '*.dll' | Where-Object {
 try { $stream=[IO.File]::OpenRead($_.FullName);$pe=[System.Reflection.PortableExecutable.PEReader]::new($stream);$ok=$pe.HasMetadata;$pe.Dispose();$stream.Dispose();$ok } catch {$false}
} | ForEach-Object FullName)
if(-not $refs){throw 'No reference assemblies for the matched C1 runtime'}
$name=if($Bootstrap){'StartupHook'}else{'NativeEngine'}
$source=Join-Path $root ($name+'.cs')
$sourceHash=(Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLower()
$identity=[ordered]@{source_sha256=$sourceHash;adapter=$adapter.id;capture_one_version=$installation.version;
 architecture=$installation.architecture;tfm=$installation.runtime.tfm;
 frameworks=@($frameworks | ForEach-Object {[ordered]@{name=$_.name;version=$_.version}});
 manifest_sha256=(Get-FileHash -LiteralPath (Join-Path (Split-Path -Parent $root) 'config\bridge-compatibility.json') -Algorithm SHA256).Hash.ToLower()}
$identityBytes=[Text.Encoding]::UTF8.GetBytes(($identity | ConvertTo-Json -Depth 8 -Compress))
$hash=[Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($identityBytes)).ToLower()
$bin=Join-Path $root ('bin\'+$hash.Substring(0,16))
New-Item -ItemType Directory -Path $bin -Force | Out-Null
$output=Join-Path $bin ('C1AI.'+$name+'.'+$hash.Substring(0,12)+'.dll')
function Test-Assembly([string]$Path) {
 if(-not (Test-Path -LiteralPath $Path)){return $false}
 try {
  $stream=[IO.File]::OpenRead($Path)
  try {$pe=[System.Reflection.PortableExecutable.PEReader]::new($stream);try {return $pe.HasMetadata -and $pe.PEHeaders.CorHeader.MetadataDirectory.Size -gt 0} finally {$pe.Dispose()}}
  finally {$stream.Dispose()}
 } catch {return $false}
}
if(-not (Test-Assembly $output)) {
 Add-Type -Path (Join-Path $PSHOME 'Microsoft.CodeAnalysis.dll')
 Add-Type -Path (Join-Path $PSHOME 'Microsoft.CodeAnalysis.CSharp.dll')
 $syntax=[Microsoft.CodeAnalysis.CSharp.CSharpSyntaxTree]::ParseText([IO.File]::ReadAllText($source))
 $metadata=[Microsoft.CodeAnalysis.MetadataReference[]]@($refs|ForEach-Object { [Microsoft.CodeAnalysis.MetadataReference]::CreateFromFile($_) })
 $options=[Microsoft.CodeAnalysis.CSharp.CSharpCompilationOptions]::new([Microsoft.CodeAnalysis.OutputKind]::DynamicallyLinkedLibrary)
 $compile=[Microsoft.CodeAnalysis.CSharp.CSharpCompilation]::Create('C1AI.'+$name+'.'+$hash.Substring(0,12),[Microsoft.CodeAnalysis.SyntaxTree[]]@($syntax),$metadata,$options)
 $temporary=$output+'.'+[guid]::NewGuid().ToString('N')+'.tmp'
 $targetStream=[IO.File]::Open($temporary,[IO.FileMode]::CreateNew)
 try { $result=$compile.Emit($targetStream) } finally { $targetStream.Dispose() }
 if(-not $result.Success){Remove-Item -LiteralPath $temporary;$result.Diagnostics|ForEach-Object ToString;throw 'Internal bridge compilation failed'}
 if(-not (Test-Assembly $temporary)){throw 'Compiled assembly is invalid'}
 [IO.File]::Move($temporary,$output,$true)
}
if(-not $Bootstrap -and -not $PrepareOnly){
 $config=@{path=$output;sha256=(Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash.ToLower();
  build_identity=$identity;protocol=1;color_policy='native-srgb-v1'}
 $state=Join-Path $env:LOCALAPPDATA 'CaptureOneAI'
 New-Item -ItemType Directory -Path $state -Force | Out-Null
 $temporaryConfig=Join-Path $state ('internal-engine.'+[guid]::NewGuid().ToString('N')+'.tmp')
 try {
  $config|ConvertTo-Json -Depth 8|Set-Content -LiteralPath $temporaryConfig -Encoding utf8NoBOM
  [IO.File]::Move($temporaryConfig,(Join-Path $state 'internal-engine.json'),$true)
 } finally {if(Test-Path -LiteralPath $temporaryConfig){Remove-Item -LiteralPath $temporaryConfig}}
}
Write-Output $output
