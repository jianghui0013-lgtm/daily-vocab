#!/usr/bin/env bash
# 把本机改动部署到服务器。需要先让 Mac 能免密 ssh 到服务器。
#   ~/.vocab/deploy.sh            提交并部署
#   ~/.vocab/deploy.sh "说明文字"  自定义提交说明
set -euo pipefail

HOST=${VOCAB_HOST:-ubuntu@122.51.84.95}
URL=${VOCAB_URL:-http://122.51.84.95:8765}
MSG=${1:-"chore: 更新"}
cd "$(dirname "$0")"

say(){ printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "1/4 本机改动"
if [ -n "$(git status --porcelain)" ]; then
  git status --short | sed 's/^/  /'
  git add -A
  git commit -q -m "$MSG

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
  echo "  已提交"
else
  echo "  没有未提交的改动"
fi

say "2/4 推到 GitHub"
git push -q origin main && echo "  $(git log --oneline -1)"

say "3/4 服务器拉取并重启"
ssh -o ConnectTimeout=10 "$HOST" '
  set -e
  sudo git -C /opt/vocab pull -q --ff-only
  sudo systemctl restart vocab-web vocab-jobs
  sleep 2
  systemctl is-active --quiet vocab-web && echo "  网页服务已重启" || {
    echo "  启动失败："; sudo journalctl -u vocab-web -n 20 --no-pager; exit 1; }
  echo "  服务器代码: $(git -C /opt/vocab log --oneline -1)"
'

say "4/4 验证"
code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$URL/")
[ "$code" = "200" ] && echo "  线上可访问 ✓" || { echo "  线上返回 $code ✗"; exit 1; }
echo
echo "  完成 → $URL"
