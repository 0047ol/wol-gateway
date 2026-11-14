param(
    [ValidateSet('build', 'flash', 'monitor', 'menuconfig', 'size', 'reconfigure')]
    [string]$Action = 'build',
    [string]$Port = '',
    [string]$IdfPath = 'C:\esp\v6.1\esp-idf',
    [string]$ToolsPath = 'C:\Espressif',
    [string]$Python = 'C:\Python315\python.exe',
    [string]$PythonEnvironment = '',
    [string]$BuildDirectory = 'build-idf61',
    [switch]$InstallPythonEnvironment
)
$ErrorActionPreference = 'Stop'
$arguments = @((Join-Path $PSScriptRoot 'build.py'), $Action,
    '--idf-path', $IdfPath, '--tools-path', $ToolsPath, '--python', $Python,
    '--build-directory', $BuildDirectory)
if ($Port) { $arguments += @('--port', $Port) }
if ($PythonEnvironment) { $arguments += @('--python-environment', $PythonEnvironment) }
if ($InstallPythonEnvironment) { $arguments += '--install-python-environment' }
& $Python @arguments
if ($LASTEXITCODE -ne 0) { throw "Firmware $Action failed: $LASTEXITCODE" }
