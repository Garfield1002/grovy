#!/bin/sh
# Example CCS_NOTIFY_CMD: forwards the message on stdin to an existing Telegram bot.
# Env from ccsd: CCS_NOTIFY_KIND (input|done|error), CCS_NOTIFY_SESSION.
# Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (e.g. in ~/.config/ccs/telegram.env).
[ -f "$HOME/.config/ccs/telegram.env" ] && . "$HOME/.config/ccs/telegram.env"
exec curl -fsS --max-time 10 -o /dev/null \
  --data-urlencode "chat_id=${TELEGRAM_CHAT_ID:?}" --data-urlencode "text=$(cat)" \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN:?}/sendMessage"
