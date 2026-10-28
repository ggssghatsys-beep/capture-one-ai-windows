# Hidden STA worker. All commands are JSON data passed to the bundled driver.
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$driverPath = Join-Path $PSScriptRoot 'uia.ps1'
while ($null -ne ($requestLine = [Console]::In.ReadLine())) {
    if ($requestLine.Length -gt 1048576) {
        [Console]::Out.WriteLine('{"ok":false,"error":{"code":"INVALID_REQUEST","message":"Request too large","possibly_mutated":false}}')
    } else {
        & $driverPath -RequestJson $requestLine -Resident
    }
    [Console]::Out.Flush()
}
