# Install the meeting-copilot command on Windows so you can launch it in any
# directory, the same way you launch claude. Creates a venv, installs the
# package into it, and puts the launcher on your PATH. Run it from the clone:
#
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# On Linux and macOS, run install.sh instead.
$ErrorActionPreference = "Stop"

$Here = $PSScriptRoot
$Venv = Join-Path $Here ".venv"
$BinDir = if ($env:BIN_DIR) { $env:BIN_DIR } else { Join-Path $HOME ".local\bin" }

function Fail($Message) {
    Write-Host "    $Message"
    exit 1
}

Write-Host "==> Ensuring system deps (ffmpeg, Python 3.10+) are available"
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Fail "ffmpeg not found. Install it first, e.g.:  winget install Gyan.FFmpeg`n    then open a new terminal so it is on PATH."
}

# The py launcher finds the newest install; a bare "python" can be the Store stub.
if ($env:PYTHON) { $Python = @($env:PYTHON) }
elseif (Get-Command py -ErrorAction SilentlyContinue) { $Python = @("py", "-3") }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $Python = @("python") }
else { Fail "Python not found. Install Python 3.10 or newer, e.g.:  winget install Python.Python.3.12" }
$PyExe, $PyArgs = $Python
& $PyExe @PyArgs -c "import sys; sys.exit(sys.version_info < (3, 10))"
if ($LASTEXITCODE -ne 0) {
    Fail "$($Python -join ' ') is older than 3.10. Install a newer Python, or set PYTHON to one."
}
$Found = & $PyExe @PyArgs -c "import sys; print(sys.version.split()[0], sys.executable)"
Write-Host "    Python $Found"

Write-Host "==> Creating venv at $Venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    & $PyExe @PyArgs -m venv $Venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPython)) {
        Fail "Could not create the venv with $($Python -join ' '). Set PYTHON to another Python 3.10+."
    }
}

Write-Host "==> Installing package (this pulls faster-whisper, may take a minute)"
& $VenvPython -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { Fail "Could not upgrade pip." }
& $VenvPython -m pip install --quiet -e $Here
if ($LASTEXITCODE -ne 0) { Fail "Could not install the package." }

Write-Host "==> Copying launcher into $BinDir"
# The launcher carries the venv's Python path inside it, so a copy runs anywhere.
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
Copy-Item -Force (Join-Path $Venv "Scripts\meeting-copilot.exe") (Join-Path $BinDir "meeting-copilot.exe")

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$OnPath = ($UserPath -split ";") -contains $BinDir
if (-not $OnPath) {
    $NewPath = if ($UserPath) { "$UserPath;$BinDir" } else { $BinDir }
    [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
    $env:Path = "$env:Path;$BinDir"
}

Write-Host ""
Write-Host "Done. 'meeting-copilot' is installed."
if ($OnPath) {
    Write-Host "You can run it now from any directory:  meeting-copilot"
} else {
    Write-Host "Added $BinDir to your user PATH. Open a new terminal, then run:  meeting-copilot"
}
Write-Host ""
Write-Host "Transcription uses Deepgram streaming by default. Put your key in:"
Write-Host "  $HOME\.config\meeting-copilot\config.env   ->   DEEPGRAM_API_KEY=..."
Write-Host "Or run fully offline with:  meeting-copilot --stt local   (downloads Whisper ~140MB)"
Write-Host "See which microphone it will use:  meeting-copilot --list-mics"
Write-Host "Check everything with:  meeting-copilot --self-test"
