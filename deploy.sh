#!/usr/bin/env bash
# 把本机改动发布到服务器。
# 不让服务器去拉 GitHub（国内机器常连不上），而是从这台 Mac 直接把文件推过去。
#   ~/.vocab/deploy.sh            提交并发布
#   ~/.vocab/deploy.sh "说明文字"  自定义提交说明
set -euo pipefail

HOST=${VOCAB_HOST:-ubuntu@122.51.84.95}
URL=${VOCAB_URL:-http://122.51.84.95:8765}
MSG=${1:-"chore: 更新"}
cd "$(dirname "$0")"

say(){ printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "1/5 本机改动"
if [ -n "$(git status --porcelain)" ]; then
  git status --short | sed 's/^/  /'
  git add -A
  git commit -q -m "$MSG

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
  echo "  已提交"
else
  echo "  没有未提交的改动"
fi

say "2/5 备份到 GitHub"
if git push -q origin main 2>/dev/null; then
  echo "  $(git log --oneline -1)"
else
  echo "  推送失败（不影响发布，稍后再推）"
fi

say "3/5 传文件到服务器"
scp -q -o ConnectTimeout=10 vocab.py install.sh "$HOST:/tmp/"
ssh -o ConnectTimeout=10 "$HOST" '
  set -e
  sudo install -m 644 /tmp/vocab.py  /opt/vocab/vocab.py
  sudo install -m 755 /tmp/install.sh /opt/vocab/install.sh
  rm -f /tmp/vocab.py /tmp/install.sh
  echo "  已更新 /opt/vocab/vocab.py"
'

say "4/5 重启服务"
ssh -o ConnectTimeout=10 "$HOST" '
  set -e
  sudo systemctl restart vocab-web vocab-jobs
  sleep 2
  systemctl is-active --quiet vocab-web || {
    echo "  启动失败："; sudo journalctl -u vocab-web -n 25 --no-pager; exit 1; }
  echo "  已重启"
'

say "5/5 验证"
code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$URL/")
[ "$code" = "200" ] || { echo "  线上返回 $code ✗"; exit 1; }
echo "  线上可访问 ✓"
echo
echo "  完成 → $URL"
