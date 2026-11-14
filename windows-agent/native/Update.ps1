param([string]$Package,[ValidatePattern('^[0-9a-f]{64}$')][string]$Sha256,[ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version)
$ErrorActionPreference = 'Stop'
$executableName = 'WolPCService.exe' # build:executable-name
throw 'The native EXE manages updates. Restart the service to finish migration.'
