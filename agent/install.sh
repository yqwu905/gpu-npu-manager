#!/bin/sh
# 在目标服务器上以 root 执行：sudo ./install.sh <运行用户> <token> [端口] [只允许读取的目录，多个用冒号分隔，默认不限制]
set -eu
USAGE="用法: install.sh <运行用户> <token> [端口] [允许读取的目录]"
RUN_USER=${1:?$USAGE}
TOKEN=${2:?$USAGE}
PORT=${3:-9100}
ALLOW_ROOTS=${4:-}
DIR=$(cd "$(dirname "$0")" && pwd)

install -d /opt/gnm-agent
install -m 0755 "$DIR/agent.py" /opt/gnm-agent/agent.py
install -m 0755 "$DIR/evaluate.py" /opt/gnm-agent/evaluate.py
install -m 0644 "$DIR/imaging.py" /opt/gnm-agent/imaging.py
umask 077
printf 'GNM_AGENT_TOKEN=%s\nGNM_AGENT_PORT=%s\n' "$TOKEN" "$PORT" > /etc/gnm-agent.env
if [ -n "$ALLOW_ROOTS" ]; then
    printf 'GNM_AGENT_ALLOW_ROOTS=%s\n' "$ALLOW_ROOTS" >> /etc/gnm-agent.env
fi
sed "s/^User=CHANGE_ME/User=$RUN_USER/" "$DIR/gnm-agent.service" > /etc/systemd/system/gnm-agent.service
systemctl daemon-reload
systemctl enable --now gnm-agent
systemctl --no-pager status gnm-agent | head -n 5
