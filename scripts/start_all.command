#!/bin/bash

BASE="$HOME/zamal-ai-operator"

export NVM_DIR="$HOME/.nvm"
if [ -s "$NVM_DIR/nvm.sh" ]; then
  . "$NVM_DIR/nvm.sh"
else
  echo "STOP: nvm not found"
  read -p "Press Enter to close..."
  exit 1
fi

cd "$BASE" || exit 1

echo "22" > .nvmrc
nvm use 22

export N8N_USER_FOLDER="$BASE/.n8n-data"
export N8N_HOST="127.0.0.1"
export N8N_PORT="5678"
export N8N_PROTOCOL="http"
export GENERIC_TIMEZONE="Asia/Qatar"
export TZ="Asia/Qatar"
export N8N_RESTRICT_FILE_ACCESS_TO="$BASE/outbox;$BASE/inbox;$BASE/approvals"

echo "Starting CPC Read-Only Inspector..."
if ! lsof -ti :8787 >/dev/null 2>&1; then
  node "$BASE/scripts/cpc_readonly_server.js" > "$BASE/logs/cpc_inspector.log" 2>&1 &
  echo "CPC Inspector started."
else
  echo "CPC Inspector already running."
fi

sleep 2

open "http://127.0.0.1:5678"
open "http://127.0.0.1:8787/health"

echo "Starting n8n..."
"$BASE/node_modules/.bin/n8n" start
