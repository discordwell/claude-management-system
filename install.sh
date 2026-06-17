#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
BIN_TARGET="$BIN_DIR/cms"

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
pip3 install -r "$SCRIPT_DIR/requirements.txt" --quiet
python3 -m playwright install chromium --quiet

# Symlink cms onto PATH (no sudo needed)
mkdir -p "$BIN_DIR"
ln -sf "$SCRIPT_DIR/cms.py" "$BIN_TARGET"
echo "  ✓ cms installed to $BIN_TARGET"

# An older install.sh symlinked into /usr/local/bin — remove it so it can't
# shadow the new symlink on PATH.
if [ -L /usr/local/bin/cms ] || [ -e /usr/local/bin/cms ]; then
  if rm -f /usr/local/bin/cms 2>/dev/null; then
    echo "  ✓ Removed stale /usr/local/bin/cms"
  else
    echo "  ⚠ Stale /usr/local/bin/cms exists and may shadow $BIN_TARGET — remove it manually (needs sudo)."
  fi
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "  ⚠ $BIN_DIR is not on your PATH — add it to your shell profile." ;;
esac

echo ""
echo "Done. Run 'cms setup' to configure your accounts."
