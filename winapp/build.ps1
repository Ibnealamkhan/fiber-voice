param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
& $Python "$PSScriptRoot/prepare_bundle.py"
if ($LASTEXITCODE -ne 0) { throw 'Calling engine packaging failed (Python 3.14+ required on the build machine)' }
# The app EXE is only an input to the installer, so it stays in build/.
$app = Join-Path $PSScriptRoot 'build/app'
dotnet publish "$PSScriptRoot/FiberVoice.csproj" -c Release -r win-x64 --self-contained true -o $app
if ($LASTEXITCODE -ne 0) { throw 'Windows app build failed' }
$nsis = Get-Command makensis -ErrorAction SilentlyContinue
if ($nsis) { $compiler = $nsis.Source }
else { $compiler = "${env:ProgramFiles(x86)}/NSIS/makensis.exe" }
if (-not (Test-Path $compiler)) { throw 'Install NSIS to build the installer.' }
New-Item -ItemType Directory -Force (Join-Path $PSScriptRoot 'dist') | Out-Null
Push-Location $PSScriptRoot
try {
    & $compiler '/WX' 'installer.nsi'
    if ($LASTEXITCODE -ne 0) { throw 'Windows installer build failed' }
} finally { Pop-Location }
Write-Host "Built installer: $PSScriptRoot/dist/FiberVoice-Setup.exe"
