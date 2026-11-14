$ErrorActionPreference = 'Stop'
$executableName = 'WolPCService.exe' # build:executable-name
Start-Process -FilePath (Join-Path $PSScriptRoot $executableName) -ArgumentList '--tray' -WindowStyle Hidden
