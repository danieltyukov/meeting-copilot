#!/usr/bin/env bash
# Install the meeting-copilot command so you can launch it in any directory,
# the same way you launch claude. Creates a venv, installs the package into it,
# and symlinks the launcher onto your PATH. Linux and macOS; on Windows, run
# install.ps1 instead.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
PYTHON="${PYTHON:-python3}"

if [ "$(uname -s)" = "Darwin" ]; then
  FFMPEG_HINT="brew install ffmpeg"
  RC="$HOME/.zshrc"
else
  FFMPEG_HINT="sudo apt install ffmpeg   (or your distro's package)"
  RC="$HOME/.bashrc"
fi

echo "==> Ensuring system deps (ffmpeg, Python 3.10+) are available"
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "    ffmpeg not found. Install it first, e.g.:  $FFMPEG_HINT"
  exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  echo "    $PYTHON is missing or older than 3.10. Install a newer Python, or point"
  echo "    PYTHON at one:  PYTHON=python3.12 ./install.sh"
  exit 1
fi

echo "==> Creating venv at $VENV"
[ -d "$VENV" ] || "$PYTHON" -m venv "$VENV"

echo "==> Installing package (this pulls faster-whisper, may take a minute)"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -e "$HERE"

echo "==> Linking launcher into $BIN_DIR"
mkdir -p "$BIN_DIR"
ln -sf "$VENV/bin/meeting-copilot" "$BIN_DIR/meeting-copilot"

echo
echo "Done. 'meeting-copilot' is installed."
case ":$PATH:" in
  *":$BIN_DIR:"*) echo "You can run it now from any directory:  meeting-copilot" ;;
  *) echo "Add $BIN_DIR to your PATH, then run:  meeting-copilot"
     echo "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> $RC && source $RC" ;;
esac
echo
echo "Transcription uses Deepgram streaming by default. Put your key in:"
echo "  ~/.config/meeting-copilot/config.env   ->   DEEPGRAM_API_KEY=..."
echo "Or run fully offline with:  meeting-copilot --stt local   (downloads Whisper ~140MB)"
echo "Check everything with:  meeting-copilot --self-test"
