#!/bin/bash
set -e

export NVM_DIR="$HOME/.nvm"
if [ -s "$NVM_DIR/nvm.sh" ]; then
  . "$NVM_DIR/nvm.sh"
else
  echo "STOP: nvm not found"
  exit 1
fi

cd "$HOME/zamal-ai-operator"

echo "22" > .nvmrc
nvm use 22

export N8N_USER_FOLDER="$HOME/zamal-ai-operator/.n8n-data"
export N8N_HOST="127.0.0.1"
export N8N_PORT="5678"
export N8N_PROTOCOL="http"
export GENERIC_TIMEZONE="Asia/Qatar"
export TZ="Asia/Qatar"

echo "Starting Zamal AI Operator..."
echo "Open: http://127.0.0.1:5678"

./node_modules/.bin/n8n start
