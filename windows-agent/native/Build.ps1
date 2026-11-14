$ErrorActionPreference = 'Stop'
# The only editable filename is StorageSettings.ExecutableFileName at the top of Program.cs.
$source = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'Program.cs') -Encoding UTF8 -Raw
$nameMatch = [regex]::Match($source, 'public const string ExecutableFileName = @"([^"]+)";')
if (-not $nameMatch.Success) { throw 'ExecutableFileName setting is missing' }
$executableName = $nameMatch.Groups[1].Value
if ($executableName.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0 -or -not $executableName.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase) -or $executableName.Length -le 4) { throw 'ExecutableFileName must be a valid .exe filename' }

$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) {
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
$out = Join-Path $PSScriptRoot 'build'
New-Item -ItemType Directory -Path $out -Force | Out-Null
$exe = Join-Path $out $executableName
& $compiler /nologo /optimize+ /target:winexe /platform:anycpu "/out:$exe" /reference:System.ServiceProcess.dll /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll /reference:System.Security.dll /reference:System.Management.dll ("/resource:" + (Join-Path $PSScriptRoot 'enroll_ca.pem') + ",WolPC.enroll_ca.pem") (Join-Path $PSScriptRoot 'Program.cs') (Join-Path $PSScriptRoot 'Maintenance.cs') (Join-Path $PSScriptRoot 'Platform.cs') (Join-Path $PSScriptRoot 'Installer.cs') (Join-Path $PSScriptRoot 'Desktop.cs')
if ($LASTEXITCODE -ne 0) { throw 'Compilation failed' }
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'WolPCService.exe.config') -Destination ($exe + '.config') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'enroll_ca.pem') -Destination $out -Force
$test = Start-Process -FilePath $exe -ArgumentList '--self-test' -WindowStyle Hidden -PassThru -Wait
if ($test.ExitCode -ne 0) { throw "Self-test failed: $($test.ExitCode)" }
$zip = Join-Path $PSScriptRoot 'WolPCService.zip'
$releaseFiles = @($executableName,($executableName + '.config'),'enroll_ca.pem','Install.ps1','Uninstall.ps1','Tray.ps1','Control.ps1','Update.ps1')
foreach ($scriptName in @('Install.ps1','Uninstall.ps1','Tray.ps1','Control.ps1','Update.ps1')) {
    $scriptText = Get-Content -LiteralPath (Join-Path $PSScriptRoot $scriptName) -Encoding UTF8 -Raw
    $assignment = '$executableName = ' + "'" + $executableName.Replace("'", "''") + "' # build:executable-name"
    $scriptText = [regex]::Replace($scriptText, '(?m)^\$executableName = .* # build:executable-name\r?$', [Text.RegularExpressions.MatchEvaluator]{param($match) $assignment})
    [IO.File]::WriteAllText((Join-Path $out $scriptName), $scriptText, [Text.UTF8Encoding]::new($true))
}
Copy-Item -LiteralPath $exe -Destination (Join-Path $PSScriptRoot $executableName) -Force
# Legacy updater transition only. Public downloads use the standalone EXE.
# Package an explicit allowlist so stale build files/configuration cannot leak.
$releasePaths = @($releaseFiles | ForEach-Object { Join-Path $out $_ })
Compress-Archive -LiteralPath $releasePaths -DestinationPath $zip -Force
Write-Output "Built and self-tested: $exe"
Get-Item -LiteralPath $exe, $zip | Select-Object Name, Length
