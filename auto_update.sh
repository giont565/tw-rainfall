#!/bin/zsh
# 雨量網站每日自動更新（launchd com.hao.twrainfall-auto 每天 04:30 跑）
#   1. 重抓今年（1 月時連去年）全台逐日資料 → 網站立刻看到最新
#   2. 網頁與抓取程式有變就同步到 GitHub 公開版 giont565/tw-rainfall
#      資料不上傳（量大、每天變動會讓 repo 暴肥；網站讀的是本機資料）
#      serve.py 不同步：本機版與 GitHub 版分岔（GitHub 版多 data.zip 讀取層），避免互蓋
set -u
export PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin
PROJ="$HOME/Documents/AI-Assistant/projects/tw-rainfall"
PUB="$HOME/Projects/tw-rainfall-pub"          # GitHub 公開版工作副本（扁平結構：index.html 在最外層）
echo "=== $(date '+%F %T') ==="

cd "$PROJ" || exit 1
Y=$(date +%Y); M=$(date +%m)
YEARS="$Y"; [ "$M" = "01" ] && YEARS="$((Y-1)),$Y"
python3 fetch.py --years "$YEARS" --force --workers 4 2>&1 | tail -3

if [ ! -d "$PUB/.git" ]; then
  git clone -q --depth 1 git@github.com:giont565/tw-rainfall.git "$PUB" || { echo "!! clone 失敗"; exit 1; }
fi
cd "$PUB" || exit 1
git fetch -q origin && git reset -q --hard origin/main || echo "!! 取 GitHub 最新版失敗（繼續用本地版）"   # 公開副本只當鏡像，先完全對齊
cp "$PROJ/web/index.html" index.html
for f in fetch.py fetch_aqi.py auto_update.sh; do
  [ -f "$PROJ/$f" ] && cp "$PROJ/$f" .
done
FILES=(index.html fetch.py fetch_aqi.py auto_update.sh)
if [ -n "$(git status --porcelain -- $FILES)" ]; then
  git add -- $FILES
  git commit -q -m "chore: 自動同步網頁與程式 $(date +%F)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
  if git push -q; then echo "GitHub 已同步"; else echo "!! push 失敗"; fi
else
  echo "程式沒有變更，GitHub 不用同步"
fi
