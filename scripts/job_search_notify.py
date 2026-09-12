#!/usr/bin/env python3
"""Format and deliver the persisted shortlist through the existing Telegram bot."""
import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state" / "job_search_results.json"
TOKEN_ENV = "ZAMAL_TELEGRAM_TOKEN"
CHAT_ENV = "ZAMAL_TELEGRAM_CHAT_ID"


def format_shortlist(result, limit=10):
    jobs = result.get("jobs", [])[:limit]
    lines = ["Zamal job shortlist", "Status: %s | Results: %s" % (result.get("status"), result.get("result_origin"))]
    if not jobs:
        lines.append("No matching jobs found in the current result set.")
    for index, job in enumerate(jobs, 1):
        lines.extend(["", "%d. %s (%s/100)" % (index, job["title"], job["score"]),
                      "%s | %s" % (job.get("company") or "Company not listed", job.get("location") or "Location not listed"),
                      job["url"]])
    return "\n".join(lines)[:4096]


def _api_call(token, method, payload=None, timeout=15):
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    request = urllib.request.Request(url, data=json.dumps(payload or {}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read(65536))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "description": "Telegram API HTTP %s" % exc.code}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"ok": False, "description": "Telegram API network failure or timeout"}
    except (ValueError, json.JSONDecodeError):
        return {"ok": False, "description": "Telegram API returned invalid JSON"}


def _chat_id(token, api_call=_api_call):
    configured = os.environ.get(CHAT_ENV, "").strip()
    if configured:
        return configured, None
    response = api_call(token, "getUpdates", {"limit": 100, "allowed_updates": ["message"]})
    if not response.get("ok"):
        return None, response.get("description", "getUpdates failed")
    chats = []
    for update in response.get("result", []):
        chat = update.get("message", {}).get("chat", {})
        if chat.get("id") is not None:
            chats.append(str(chat["id"]))
    if not chats:
        return None, "no chat ID found; send the existing bot a message or set ZAMAL_TELEGRAM_CHAT_ID"
    return chats[-1], None


def send_shortlist(result, token=None, chat_id=None, api_call=_api_call):
    token = token or os.environ.get(TOKEN_ENV, "")
    if not token or len(token) < 20:
        return {"status": "BLOCKED", "reason": "Telegram token is not configured in ZAMAL_TELEGRAM_TOKEN"}
    chat_id = chat_id or os.environ.get(CHAT_ENV, "").strip()
    if not chat_id:
        chat_id, reason = _chat_id(token, api_call)
        if reason:
            return {"status": "BLOCKED", "reason": reason}
    response = api_call(token, "sendMessage", {"chat_id": chat_id, "text": format_shortlist(result)})
    if response.get("ok") is True and response.get("result", {}).get("message_id") is not None:
        return {"status": "SENT", "chat_id": chat_id, "message_id": response["result"]["message_id"]}
    return {"status": "BLOCKED", "reason": response.get("description", "Telegram API rejected sendMessage")}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=STATE)
    parser.add_argument("--test", action="store_true", help="Send the current persisted shortlist")
    args = parser.parse_args(argv)
    result = json.loads(args.state.read_text(encoding="utf-8"))
    delivery = send_shortlist(result)
    print(json.dumps(delivery, sort_keys=True))
    return 0 if delivery["status"] == "SENT" else 3


if __name__ == "__main__":
    raise SystemExit(main())