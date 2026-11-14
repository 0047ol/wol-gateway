param([ValidateSet('on','off')][string]$Startup,[switch]$Unbind,[ValidatePattern('^[0-9a-f]{64}$')][string]$BindingToken)
$ErrorActionPreference = 'Stop'
$executableName = 'WolPCService.exe' # build:executable-name
$arguments = if ($Unbind) { @('--unbind',$BindingToken) } else { @('--startup',$Startup) }
$p = Start-Process -FilePath (Join-Path $PSScriptRoot $executableName) -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -PassThru -Wait
exit $p.ExitCode
