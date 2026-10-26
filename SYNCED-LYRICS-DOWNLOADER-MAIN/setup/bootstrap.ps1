# Triple 9 Studio - downloads the latest version from GitHub into one folder and starts the installer.
# Started by "Triple9Studio-Setup.cmd". Running it again updates the program files and keeps your settings.
param([string]$Target = (Join-Path $env:USERPROFILE 'Triple 9 Studio'), [switch]$NoInstall, [switch]$Yes)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$repo = 'imtatar999/Triple-9-Studio'

Write-Host ''
Write-Host '  TRIPLE 9 STUDIO - SETUP' -ForegroundColor Cyan
Write-Host "  Install folder: $Target" -ForegroundColor DarkGray
Write-Host ''

# newest version tag (vX.Y.Z); the main branch when there is none
$zipUrl = "https://github.com/$repo/archive/refs/heads/main.zip"
try {
    $tags = Invoke-RestMethod -UseBasicParsing -Uri "https://api.github.com/repos/$repo/tags?per_page=100" -Headers @{ 'User-Agent' = 'Triple9Studio-setup' }
    $best = $tags | Where-Object { $_.name -match '^v?\d+\.\d+\.\d+$' } |
        Sort-Object { [version]($_.name -replace '^v', '') } -Descending | Select-Object -First 1
    if ($best) { $zipUrl = "https://github.com/$repo/archive/refs/tags/$($best.name).zip"; Write-Host "  Version: $($best.name)" }
} catch { Write-Host '  (could not read the version list - using the main branch)' -ForegroundColor Yellow }

$work = Join-Path ([IO.Path]::GetTempPath()) ('t9setup_' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $work | Out-Null
$zip = Join-Path $work 'studio.zip'
Write-Host '  Downloading the program...'
Invoke-WebRequest -UseBasicParsing -Uri $zipUrl -OutFile $zip
Expand-Archive -Force -Path $zip -DestinationPath $work
$source = Get-ChildItem -Directory $work | Select-Object -First 1
if (-not $source) { throw 'The download is empty.' }

New-Item -ItemType Directory -Force -Path $Target | Out-Null
# copy over the folder; a previous installation keeps its Python (runtime), settings, backups and caches
& robocopy $source.FullName $Target /E /XD runtime BACKUPS PRIVATE lyrics_sync_state /XF lyrics_gui_config.json studio_preferences.json /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Copying the files failed (robocopy code $LASTEXITCODE)." }
Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
Write-Host '  Program files are in place.' -ForegroundColor Green

if ($NoInstall) { exit 0 }
$install = Join-Path $Target 'SYNCED-LYRICS-DOWNLOADER-MAIN\setup\install.ps1'
if ($Yes) { & powershell -NoProfile -ExecutionPolicy Bypass -File $install -Yes } else { & powershell -NoProfile -ExecutionPolicy Bypass -File $install }
exit $LASTEXITCODE
