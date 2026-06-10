#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/home/yu/daily-news-briefing"
cd "$REPO_DIR"

URL="$(python3 scripts/generate_daily.py --commit --push | tail -n 1)"
DATE="$(python3 - <<'PY'
from datetime import datetime
from zoneinfo import ZoneInfo
print(datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d'))
PY
)"

cat <<MSG
📰 每日新闻简报已更新

日期：$DATE
链接：$URL
MSG
