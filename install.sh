#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_TARGET="/usr/local/bin/cms"

echo "Installing cms..."

# Make scripts executable
chmod +x "$SCRIPT_DIR/cms.py"
chmod +x "$SCRIPT_DIR/daemon.py"
chmod +x "$SCRIPT_DIR/launch.sh"

# Install tmux if missing
if ! command -v tmux &>/dev/null; then
  echo "Installing tmux..."
  brew install tmux
fi

# Install Python dependencies
echo "Installing Python dependencies..."
pip3 install browser-cookie3 playwright requests --quiet
python3 -m playwright install chromium --quiet

# Symlink cms to PATH
if [ -L "$BIN_TARGET" ] || [ -f "$BIN_TARGET" ]; then
  rm "$BIN_TARGET"
fi
ln -s "$SCRIPT_DIR/cms.py" "$BIN_TARGET"
echo "  ✓ cms installed to $BIN_TARGET"

echo ""
echo "Done. Run 'cms setup' to configure your accounts."
