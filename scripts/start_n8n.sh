#!/bin/bash
set -e

export NVM_DIR="$HOME/.nvm"
if [ -s "$NVM_DIR/nvm.sh" ]; then
  . "$NVM_DIR/nvm.sh"
else
  echo "STOP: nvm not found"
  exit 1
fi

# Node 22 required by n8n 2.33.0
nvm use 22

# Verified original database: zamalahmed006@gmail.com, 11 workflows
# located in the backup directory (project node_modules live there too).
N8N_BIN="$HOME/zamal-ai-operator-backup-20260913-164656/node_modules/.bin/n8n"
export N8N_USER_FOLDER="$HOME/zamal-ai-operator-backup-20260913-164656/.n8n-data"
export N8N_HOST="127.0.0.1"
export N8N_PORT="5678"
export N8N_PROTOCOL="http"
export GENERIC_TIMEZONE="Asia/Qatar"
export TZ="Asia/Qatar"

if [ ! -x "$N8N_BIN" ]; then
  echo "STOP: n8n binary not found at $N8N_BIN"
  exit 1
fi

echo "Starting n8n (Node $(node --version))..."
echo "Data: $N8N_USER_FOLDER"
echo "Open: http://$N8N_HOST:$N8N_PORT"

exec "$N8N_BIN" start
