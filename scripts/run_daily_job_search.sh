#!/bin/zsh

ROOT="/Users/book/zamal-ai-operator"
cd "$ROOT" || exit 1
mkdir -p logs

# Load existing local Telegram credentials without printing secrets.
if [ -f "$ROOT/state/gateway/auth.env" ]; then
    set -a
    source "$ROOT/state/gateway/auth.env"
    set +a
fi

export ZAMAL_TELEGRAM_TOKEN="${ZAMAL_TELEGRAM_TOKEN:-${TELEGRAM_BOT_TOKEN:-${TELEGRAM_TOKEN:-${TG_BOT_TOKEN:-${BOT_TOKEN:-}}}}}"
export ZAMAL_TELEGRAM_CHAT_ID="${ZAMAL_TELEGRAM_CHAT_ID:-${TELEGRAM_CHAT_ID:-${TG_CHAT_ID:-${CHAT_ID:-}}}}"

python3 scripts/job_search_runner.py >> logs/job-search.log 2>> logs/job-search-error.log
SEARCH_RC=$?

python3 scripts/job_search_notify.py >> logs/job-search.log 2>> logs/job-search-error.log
NOTIFY_RC=$?

echo "$(date '+%Y-%m-%d %H:%M:%S') search_rc=$SEARCH_RC notify_rc=$NOTIFY_RC" >> logs/job-search.log

exit $SEARCH_RC
