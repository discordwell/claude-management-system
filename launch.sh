#!/bin/bash
# Per-account Claude launcher. Called by cms.py via tmux.
ACCOUNT="$1"
ACCOUNTS_DIR="$HOME/.claude-accounts"
TOKEN_FILE="$ACCOUNTS_DIR/$ACCOUNT/oauth_token.json"

if [ ! -f "$TOKEN_FILE" ]; then
  echo "Error: no token file for account '$ACCOUNT' at $TOKEN_FILE"
  echo "Run: cms setup"
  read -p "Press Enter to close..."
  exit 1
fi

export CLAUDE_CODE_OAUTH_TOKEN=$(cat "$TOKEN_FILE")
export CLAUDE_CONFIG_DIR="$ACCOUNTS_DIR/$ACCOUNT"

claude

echo ""
echo "[cms: session ended — account: $ACCOUNT]"
read -p "Press Enter to close..."
