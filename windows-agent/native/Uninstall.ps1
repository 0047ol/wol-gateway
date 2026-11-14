param([switch]$RemoveConfig)
$ErrorActionPreference = 'Stop'
$executableName = 'WolPCService.exe' # build:executable-name
# Configuration is preserved. The EXE owns confirmation and administrator consent.
$p = Start-Process -FilePath (Join-Path $PSScriptRoot $executableName) -ArgumentList '--uninstall' -WindowStyle Hidden -PassThru -Wait
exit $p.ExitCode
