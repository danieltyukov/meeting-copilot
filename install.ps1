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

function Find-Python([string]$Exe, [string[]]$Rest) {
    # "version path" for a Python 3.10+, else nothing. A bare "python" can be
    # the Microsoft Store stub; what it prints on stderr must not stop the script.
    $ErrorActionPreference = "Continue"
    if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) { return $null }
    $Out = & $Exe @Rest -c "import sys; sys.exit(1) if sys.version_info < (3, 10) else print(sys.version.split()[0], sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $Out) { return "$Out" }
    return $null
}

$PyExe = $null
$PyArgs = @()
if ($env:PYTHON) {
    if ($Found = Find-Python $env:PYTHON @()) { $PyExe = $env:PYTHON }
} elseif ($Found = Find-Python "python" @()) {
    $PyExe = "python"
} elseif ($Found = Find-Python "py" @("-3")) {
    $PyExe = "py"
    $PyArgs = @("-3")
}
if (-not $PyExe) {
    Fail "Python 3.10 or newer not found. Install it, e.g.:  winget install Python.Python.3.12`n    or set PYTHON to one."
}
Write-Host "    Python $Found"

Write-Host "==> Creating venv at $Venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    & $PyExe @PyArgs -m venv $Venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPython)) {
        Fail "Could not create the venv with $PyExe $PyArgs. Set PYTHON to another Python 3.10+."
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
