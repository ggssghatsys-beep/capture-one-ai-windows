param([switch]$Bootstrap)
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
$core=(Get-ChildItem 'C:\Program Files\dotnet\shared\Microsoft.NETCore.App' -Directory | Where-Object {([version]$_.Name).Major -eq 10} | Sort-Object {[version]$_.Name} -Descending | Select-Object -First 1).FullName
$desktop=(Get-ChildItem 'C:\Program Files\dotnet\shared\Microsoft.WindowsDesktop.App' -Directory | Where-Object {([version]$_.Name).Major -eq 10} | Sort-Object {[version]$_.Name} -Descending | Select-Object -First 1).FullName
$refs=@(Get-ChildItem $core,$desktop -Filter '*.dll' | Where-Object {
 try { $stream=[IO.File]::OpenRead($_.FullName);$pe=[System.Reflection.PortableExecutable.PEReader]::new($stream);$ok=$pe.HasMetadata;$pe.Dispose();$stream.Dispose();$ok } catch {$false}
} | ForEach-Object FullName)
if(-not $refs){throw 'No .NET 10 reference assemblies'}
$name=if($Bootstrap){'StartupHook'}else{'NativeEngine'}
$source=Join-Path $root ($name+'.cs')
$hash=(Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLower()
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
if(-not $Bootstrap){
 $config=@{path=$output;sha256=(Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash.ToLower()}
 $state=Join-Path $env:LOCALAPPDATA 'CaptureOneAI'
 $config|ConvertTo-Json|Set-Content -LiteralPath (Join-Path $state 'internal-engine.json') -Encoding UTF8
}
Write-Output $output
