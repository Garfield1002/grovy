#!/bin/sh
# Example GROVY_NOTIFY_CMD: forwards the message on stdin to an existing Telegram bot.
# Env from grovyd: GROVY_NOTIFY_KIND (input|done|error), GROVY_NOTIFY_SESSION.
# Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (e.g. in ~/.config/grovy/telegram.env).
[ -f "$HOME/.config/grovy/telegram.env" ] && . "$HOME/.config/grovy/telegram.env"
exec curl -fsS --max-time 10 -o /dev/null \
  --data-urlencode "chat_id=${TELEGRAM_CHAT_ID:?}" --data-urlencode "text=$(cat)" \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN:?}/sendMessage"
