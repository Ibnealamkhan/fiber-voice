param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
& $Python "$PSScriptRoot/prepare_bundle.py"
if ($LASTEXITCODE -ne 0) { throw 'Calling engine packaging failed (Python 3.14+ required on the build machine)' }
$output = Join-Path $PSScriptRoot 'dist/standalone-win-x64'
dotnet publish "$PSScriptRoot/FiberVoice.csproj" -c Release -r win-x64 --self-contained true -o $output
if ($LASTEXITCODE -ne 0) { throw 'Windows app build failed' }
Copy-Item "$output/FiberVoice.exe" "$PSScriptRoot/dist/FiberVoice.exe" -Force
Write-Host "Built standalone app: $PSScriptRoot/dist/FiberVoice.exe"
$nsis = Get-Command makensis -ErrorAction SilentlyContinue
if ($nsis) { $compiler = $nsis.Source }
else { $compiler = "${env:ProgramFiles(x86)}/NSIS/makensis.exe" }
if (-not (Test-Path $compiler)) { throw 'Install NSIS to build the installer. The portable EXE is already built.' }
Push-Location $PSScriptRoot
try {
    & $compiler '/WX' 'installer.nsi'
    if ($LASTEXITCODE -ne 0) { throw 'Windows installer build failed' }
} finally { Pop-Location }
Write-Host "Built installer: $PSScriptRoot/dist/FiberVoice-Setup.exe"
