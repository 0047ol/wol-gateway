param([switch]$Update,[switch]$NoTray)
$ErrorActionPreference = 'Stop'
$executableName = 'WolPCService.exe' # build:executable-name
# Transition bridge used only by the updater installed with 1.2.x.
$exe = Join-Path $PSScriptRoot $executableName
$arguments = @('--install','--no-tray')
if ($Update) { $arguments += '--update' }
$p = Start-Process -FilePath $exe -ArgumentList $arguments -WindowStyle Hidden -PassThru -Wait
exit $p.ExitCode
