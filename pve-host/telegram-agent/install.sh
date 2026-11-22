#!/bin/sh
# Instala/atualiza o telegram-agent no pve. Segredos em /etc/telegram-agent.env (não vem do git).
set -e
cd "$(dirname "$0")"
install -d /usr/local/lib/telegram-agent /var/lib/telegram-agent
install -m 644 agent.py confirm_hook.py /usr/local/lib/telegram-agent/
install -m 644 telegram-agent.service /etc/systemd/system/
[ -f /etc/telegram-agent.env ] || { umask 077; cat > /etc/telegram-agent.env <<'ENV'
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_THREAD_ID=25
ADMIN_IDS=1199930908
AGENT_TOKEN=
ENV
echo "preencha /etc/telegram-agent.env"; }
systemctl daemon-reload
systemctl enable telegram-agent >/dev/null 2>&1
systemctl restart telegram-agent
systemctl --no-pager -l status telegram-agent | head -5
