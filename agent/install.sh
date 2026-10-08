#!/bin/sh
# 在目标服务器上以 root 执行：sudo ./install.sh <运行用户> <token> [端口]
set -eu
RUN_USER=${1:?用法: install.sh <运行用户> <token> [端口]}
TOKEN=${2:?用法: install.sh <运行用户> <token> [端口]}
PORT=${3:-9100}
DIR=$(cd "$(dirname "$0")" && pwd)

install -d /opt/gnm-agent
install -m 0755 "$DIR/agent.py" /opt/gnm-agent/agent.py
umask 077
printf 'GNM_AGENT_TOKEN=%s\nGNM_AGENT_PORT=%s\n' "$TOKEN" "$PORT" > /etc/gnm-agent.env
sed "s/^User=CHANGE_ME/User=$RUN_USER/" "$DIR/gnm-agent.service" > /etc/systemd/system/gnm-agent.service
systemctl daemon-reload
systemctl enable --now gnm-agent
systemctl --no-pager status gnm-agent | head -n 5
