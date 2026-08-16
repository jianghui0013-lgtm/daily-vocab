#!/usr/bin/env bash
# 一键安装：在一台干净的 Ubuntu / Debian 服务器上跑
#   curl -sSL https://raw.githubusercontent.com/jianghui0013-lgtm/daily-vocab/main/install.sh | bash
set -euo pipefail

APP=/opt/vocab
DATA=$APP/data
PORT=${VOCAB_PORT:-8765}
ENVFILE=/etc/vocab.env
REPO=https://github.com/jianghui0013-lgtm/daily-vocab.git

say(){ printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[ "$(id -u)" = "0" ] || { echo "请用 root 运行（前面加 sudo）"; exit 1; }

say "1/6 装依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 git curl ca-certificates >/dev/null
python3 -c 'import sqlite3,sys; assert sys.version_info>=(3,7)' \
  || { echo "需要 Python 3.7 以上"; exit 1; }
echo "  $(python3 -V)"

say "2/6 拿代码"
if [ -d "$APP/.git" ]; then
  git -C "$APP" pull -q --ff-only && echo "  已更新到最新"
else
  git clone -q "$REPO" "$APP" && echo "  已克隆到 $APP"
fi
mkdir -p "$DATA"

say "3/6 建离线词典（约 40 万条，第一次要下 58MB，之后跳过）"
if [ -s "$DATA/dict.db" ]; then
  echo "  已存在，跳过"
else
  curl -sSL --retry 3 -o /tmp/ecdict.csv \
    https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv
  VOCAB_HOME=$DATA python3 "$APP/vocab.py" dict import /tmp/ecdict.csv
  rm -f /tmp/ecdict.csv
fi

say "4/6 导入词库"
VOCAB_HOME=$DATA python3 "$APP/vocab.py" import "$APP/seed/words.json"

say "5/6 生成访问口令"
if [ -s "$ENVFILE" ]; then
  echo "  已存在，沿用旧口令（要换就删掉 $ENVFILE 重跑）"
else
  TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')
  cat > "$ENVFILE" <<EOF
VOCAB_HOME=$DATA
VOCAB_TOKEN=$TOKEN
VOCAB_API_KEY=
EOF
  chmod 600 "$ENVFILE"
  echo "  已写入 $ENVFILE（权限 600）"
fi
TOKEN=$(grep '^VOCAB_TOKEN=' "$ENVFILE" | cut -d= -f2-)

say "6/6 设为开机自启"
if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
  cat > /etc/systemd/system/vocab-web.service <<EOF
[Unit]
Description=Vocab web
After=network.target
[Service]
EnvironmentFile=$ENVFILE
ExecStart=/usr/bin/python3 $APP/vocab.py serve --lan -p $PORT
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF
  cat > /etc/systemd/system/vocab-jobs.service <<EOF
[Unit]
Description=Vocab background jobs (news, summaries, examples)
After=network.target
[Service]
EnvironmentFile=$ENVFILE
ExecStart=/usr/bin/python3 $APP/vocab.py watch
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable -q --now vocab-web vocab-jobs
  sleep 2
  systemctl is-active --quiet vocab-web && echo "  网页服务已启动" || {
    echo "  启动失败，看日志：journalctl -u vocab-web -n 30"; exit 1; }
else
  echo "  没有 systemd，用后台进程启动（重启后需要重跑本脚本）"
  set -a; . "$ENVFILE"; set +a
  nohup python3 "$APP/vocab.py" serve --lan -p "$PORT" >/var/log/vocab-web.log 2>&1 &
  nohup python3 "$APP/vocab.py" watch                  >/var/log/vocab-jobs.log 2>&1 &
  sleep 2
fi

IP=$(curl -s --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')
cat <<EOF

────────────────────────────────────────
  装好了。用这个地址打开：

  http://$IP:$PORT/?k=$TOKEN

  还要做两件事：
  1) 到云服务商的防火墙 / 安全组，放行 TCP $PORT 端口
  2) 打开网页 → 右上角 ⚙ → 填 DeepSeek 密钥（例句和新闻摘要要用）

  常用命令：
    systemctl restart vocab-web     重启网页
    systemctl status  vocab-jobs    看后台任务
    journalctl -u vocab-web -f      看实时日志
────────────────────────────────────────
EOF
