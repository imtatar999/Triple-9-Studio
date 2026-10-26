# Triple 9 Studio - one-click setup (started by "Install Triple 9 Studio.cmd").
# Everything goes into the "runtime" folder next to the program: no Python is installed into Windows,
# no administrator rights, PATH is not changed. Running it again repairs / completes an installation.
param([switch]$Yes, [switch]$NoShortcuts)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'   # makes Invoke-WebRequest much faster
try { $Host.UI.RawUI.WindowTitle = 'Triple 9 Studio - Setup' } catch {}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$app  = Split-Path -Parent $PSScriptRoot          # SYNCED-LYRICS-DOWNLOADER-MAIN
$root = Split-Path -Parent $app                   # folder with Triplenine_Lyrics_Studio.py
$rt   = Join-Path $root 'runtime'
$uv   = Join-Path $rt 'uv\uv.exe'
$py   = Join-Path $rt 'app\Scripts\python.exe'
$pyw  = Join-Path $rt 'app\Scripts\pythonw.exe'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $rt 'python'
$env:UV_CACHE_DIR = Join-Path $rt 'cache'
$env:UV_PYTHON_PREFERENCE = 'only-managed'
$env:PYTHONIOENCODING = 'utf-8'

# The 999 logo on this window (title bar and taskbar)
try {
    Add-Type -Namespace T9 -Name Win -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
[DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);
[DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr LoadImage(IntPtr hInst, string name, uint type, int cx, int cy, uint flags);
'@
    $logo = Join-Path $app 'triplenine.ico'
    $window = [T9.Win]::GetConsoleWindow()
    [void][T9.Win]::SendMessage($window, 0x80, [IntPtr]0, [T9.Win]::LoadImage([IntPtr]::Zero, $logo, 1, 16, 16, 0x10))
    [void][T9.Win]::SendMessage($window, 0x80, [IntPtr]1, [T9.Win]::LoadImage([IntPtr]::Zero, $logo, 1, 32, 32, 0x10))
} catch {}

function Say($text, $color = 'Gray') { Write-Host $text -ForegroundColor $color }
function Step($n, $text) { Write-Host ''; Write-Host "[$n/6] $text" -ForegroundColor Cyan }
function Fail($text) {
    Write-Host ''
    Write-Host 'SETUP STOPPED' -ForegroundColor Red
    Write-Host $text -ForegroundColor Red
    Write-Host ''
    Write-Host 'Check your internet connection and run "Install Triple 9 Studio.cmd" again -' -ForegroundColor Yellow
    Write-Host 'it continues where it stopped.' -ForegroundColor Yellow
    if (-not $Yes) { Read-Host 'Press Enter to close' | Out-Null }
    exit 1
}
function Check($what) { if ($LASTEXITCODE -ne 0) { Fail "$what failed (code $LASTEXITCODE)." } }

Clear-Host
Say '================================================================' 'DarkMagenta'
Say '                 TRIPLE 9 STUDIO  -  SETUP' 'White'
Say '================================================================' 'DarkMagenta'
Say ''
Say 'This installs everything the Downloader and Lyrics Sync need:'
Say '  - Python 3.11 (only inside the program folder, not into Windows)'
Say '  - lyrics download, Whisper speech recognition, Demucs, FFmpeg'
Say '  - PyTorch - with NVIDIA graphics card support when you have one'
Say ''
Say 'About 5 GB will be downloaded; it can take 10-30 minutes.'
Say 'No administrator rights are needed. Lyric Builder installs its extra'
Say 'parts later with its "Check / Setup Builder" button.'
Say ''
Say "Install folder: $rt" 'DarkGray'
Say ''
if (-not $Yes) { Read-Host 'Press Enter to start  (or close this window to cancel)' | Out-Null }

New-Item -ItemType Directory -Force -Path $rt | Out-Null

Step 1 'Installer tool (uv)'
if (Test-Path $uv) {
    Say '  already there'
} else {
    $zip = Join-Path $rt 'uv.zip'
    try {
        Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile $zip
        Expand-Archive -Force -Path $zip -DestinationPath (Join-Path $rt 'uv')
        Remove-Item -Force $zip
    } catch { Fail "Could not download uv: $($_.Exception.Message)" }
    if (-not (Test-Path $uv)) { Fail 'uv.exe was not found after unpacking.' }
    Say '  ok'
}

Step 2 'Python 3.11'
& $uv python install 3.11 --no-bin
Check 'Python download'

Step 3 'Program environment'
if (Test-Path $py) {
    Say '  already there'
} else {
    & $uv venv (Join-Path $rt 'app') --python 3.11
    Check 'Creating the environment'
}

Step 4 'Packages (the longest step)'
& $py (Join-Path $app 'studio_install.py')
Check 'Installing the packages'

Step 5 'Shortcuts'
if ($NoShortcuts) {
    Say '  skipped'
} else {
    try {
        $shell = New-Object -ComObject WScript.Shell
        foreach ($folder in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
            Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $folder 'Triplenine Studio.lnk')  # old name
            $link = $shell.CreateShortcut((Join-Path $folder 'Triple 9 Studio.lnk'))
            $link.TargetPath = $pyw
            $link.Arguments = '"' + (Join-Path $root 'Triplenine_Lyrics_Studio.py') + '"'
            $link.WorkingDirectory = $root
            $link.IconLocation = (Join-Path $app 'triplenine.ico')
            $link.Description = 'Triple 9 Studio - lyrics downloader, sync and builder'
            $link.Save()
        }
        Say '  "Triple 9 Studio" on the desktop and in the Start menu'
    } catch { Say "  could not create shortcuts: $($_.Exception.Message)" 'Yellow' }
}

Step 6 'Cleaning up the download cache'
& $uv cache clean | Out-Null
Say '  ok'

Say ''
Say '================================================================' 'DarkGreen'
Say '  READY - Triple 9 Studio is installed.' 'Green'
Say '================================================================' 'DarkGreen'
Say 'Start it with the "Triple 9 Studio" icon on your desktop.'
Say 'Speech-recognition models download the first time you sync a song.'
Say ''
if (-not $Yes) {
    $answer = Read-Host 'Start Triple 9 Studio now? [Y/N]'
    if ($answer -match '^[YyTt]') {
        Start-Process -FilePath $pyw -ArgumentList ('"' + (Join-Path $root 'Triplenine_Lyrics_Studio.py') + '"') -WorkingDirectory $root
    }
}
exit 0
