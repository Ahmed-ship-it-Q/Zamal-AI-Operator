#!/bin/bash
set -e

export NVM_DIR="$HOME/.nvm"
if [ -s "$NVM_DIR/nvm.sh" ]; then
  . "$NVM_DIR/nvm.sh"
fi

BASE="$HOME/zamal-ai-operator"
cd "$BASE"

nvm use 22 >/dev/null 2>&1 || true

echo "===== ZAMAL AI OPERATOR STATUS ====="
echo "Base folder: $BASE"
echo ""

echo "Node:"
node -v || true
echo ""

echo "n8n:"
"$BASE/node_modules/.bin/n8n" --version || true
echo ""

echo "Knowledge files:"
ls -1 "$BASE/knowledge"
echo ""

echo "Workspace folders:"
ls -1 "$BASE" | grep -E "knowledge|approvals|logs|scripts|inbox|outbox" || true
echo ""

echo "Default Node alias:"
nvm alias default || true
