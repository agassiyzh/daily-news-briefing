#!/usr/bin/env python3
"""Generate a daily news briefing as static GitHub Pages HTML.

The script is intentionally dependency-free so it can run from Hermes cron.
It reads RSS/Atom sources from config/news_sources.json, writes:

- briefings/YYYY-MM-DD.html
- index.html
- archive.html
- data/latest.json

When called with --commit and --push, it commits generated files and pushes to GitHub.
It prints the published GitHub Pages URL on stdout as the final line.
"""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "news_sources.json"
BRIEFINGS_DIR = ROOT / "briefings"
DATA_DIR = ROOT / "data"
USER_AGENT = "daily-news-briefing/1.0 (+https://agassiyzh.github.io/daily-news-briefing/)"


@dataclass
class NewsItem:
    section: str
    source: str
    title: str
    url: str
    published: str
    summary: str
    score: int


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def fetch_url(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def text_of(node: ET.Element | None, names: list[str]) -> str:
    if node is None:
        return ""
    for name in names:
        found = node.find(name)
        if found is not None and found.text:
            return clean_text(found.text)
    # Namespace-insensitive fallback.
    wanted = {n.split("}")[-1].split(":")[-1] for n in names}
    for child in list(node):
        tag = child.tag.split("}")[-1]
        if tag in wanted and child.text:
            return clean_text(child.text)
    return ""


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url.strip())
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), ""))


def parse_date(value: str, tz: ZoneInfo) -> datetime:
    if not value:
        return datetime.now(tz)
    value = clean_text(value)
    try:
        dt = email.utils.parsedate_to_datetime(value)
        if dt is not None:
            return dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=timezone.utc).astimezone(tz)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value[:25], fmt)
            return dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=tz)
        except Exception:
            continue
    return datetime.now(tz)


def link_from_atom_entry(entry: ET.Element) -> str:
    for child in list(entry):
        if child.tag.split("}")[-1] == "link":
            href = child.attrib.get("href", "")
            rel = child.attrib.get("rel", "alternate")
            if href and rel in {"alternate", ""}:
                return normalize_url(href)
    return ""


def parse_feed(content: bytes, section: str, source_name: str, tz: ZoneInfo) -> list[NewsItem]:
    items: list[NewsItem] = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return items

    root_tag = root.tag.split("}")[-1].lower()
    if root_tag == "rss" or root.find("channel") is not None:
        channel = root.find("channel") or root
        entries = channel.findall("item")
        for entry in entries:
            title = text_of(entry, ["title"])
            url = normalize_url(text_of(entry, ["link", "guid"]))
            summary = text_of(entry, ["description", "summary", "content"])
            published_raw = text_of(entry, ["pubDate", "published", "updated", "date"])
            if title and url:
                dt = parse_date(published_raw, tz)
                items.append(NewsItem(section, source_name, title, url, dt.isoformat(), summary, 0))
    else:
        # Atom and namespace-heavy feeds.
        entries = [n for n in root.iter() if n.tag.split("}")[-1] == "entry"]
        for entry in entries:
            title = text_of(entry, ["title"])
            url = link_from_atom_entry(entry) or normalize_url(text_of(entry, ["id"]))
            summary = text_of(entry, ["summary", "content", "subtitle"])
            published_raw = text_of(entry, ["published", "updated", "date"])
            if title and url:
                dt = parse_date(published_raw, tz)
                items.append(NewsItem(section, source_name, title, url, dt.isoformat(), summary, 0))
    return items


def item_key(item: NewsItem) -> str:
    base = normalize_url(item.url) or re.sub(r"\W+", "", item.title.lower())
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


def score_item(item: NewsItem, keywords: list[str], now: datetime) -> int:
    text = f"{item.title} {item.summary}".lower()
    score = 0
    for kw in keywords:
        if kw.lower() in text:
            score += 10
    try:
        age_hours = (now - datetime.fromisoformat(item.published)).total_seconds() / 3600
        if age_hours <= 12:
            score += 12
        elif age_hours <= 24:
            score += 8
        elif age_hours <= 36:
            score += 4
    except Exception:
        score += 1
    # Prefer concise, information-rich titles.
    if 18 <= len(item.title) <= 120:
        score += 3
    return score


def collect_items(config: dict[str, Any], now: datetime) -> tuple[list[NewsItem], list[str]]:
    errors: list[str] = []
    briefing = config.get("briefing", {})
    lookback = timedelta(hours=int(briefing.get("lookback_hours", 36)))
    keywords = briefing.get("keywords_boost", [])
    cutoff = now - lookback
    seen: set[str] = set()
    all_items: list[NewsItem] = []

    for section in config.get("sections", []):
        section_name = section.get("name", "未分类")
        for source in section.get("sources", []):
            source_name = source.get("name", source.get("url", "Unknown"))
            url = source.get("url", "")
            if not url:
                continue
            try:
                content = fetch_url(url)
                parsed = parse_feed(content, section_name, source_name, now.tzinfo or timezone.utc)  # type: ignore[arg-type]
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                errors.append(f"{source_name}: {exc}")
                continue
            for item in parsed:
                key = item_key(item)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    published = datetime.fromisoformat(item.published)
                except Exception:
                    published = now
                if published < cutoff:
                    continue
                item.score = score_item(item, keywords, now)
                all_items.append(item)
            time.sleep(0.2)

    return all_items, errors


def truncate(value: str, length: int = 180) -> str:
    value = clean_text(value)
    if len(value) <= length:
        return value
    return value[: length - 1].rstrip() + "…"


def group_and_select(items: list[NewsItem], config: dict[str, Any]) -> dict[str, list[NewsItem]]:
    max_per = int(config.get("briefing", {}).get("max_items_per_section", 8))
    max_total = int(config.get("briefing", {}).get("max_items_total", 30))
    section_order = [s.get("name", "未分类") for s in config.get("sections", [])]
    grouped: dict[str, list[NewsItem]] = {name: [] for name in section_order}
    for item in sorted(items, key=lambda x: (x.score, x.published), reverse=True):
        grouped.setdefault(item.section, [])
        if len(grouped[item.section]) < max_per:
            grouped[item.section].append(item)
    # Enforce total cap by round-robin section order.
    selected: dict[str, list[NewsItem]] = {name: [] for name in grouped}
    total = 0
    while total < max_total:
        added = False
        for section in section_order:
            src = grouped.get(section, [])
            dst = selected.setdefault(section, [])
            if len(dst) < len(src) and total < max_total:
                dst.append(src[len(dst)])
                total += 1
                added = True
        if not added:
            break
    return {k: v for k, v in selected.items() if v}


def render_html(config: dict[str, Any], date_str: str, now: datetime, grouped: dict[str, list[NewsItem]], errors: list[str]) -> str:
    title = config.get("site", {}).get("title", "Daily News Briefing")
    total_items = sum(len(v) for v in grouped.values())
    cards = []
    for section, items in grouped.items():
        item_html = []
        for idx, item in enumerate(items, 1):
            published = datetime.fromisoformat(item.published).strftime("%m-%d %H:%M")
            summary = truncate(item.summary, 220) or "暂无摘要，点击原文查看。"
            item_html.append(f"""
            <article class=\"item\">
              <div class=\"rank\">{idx}</div>
              <div class=\"item-body\">
                <h3><a href=\"{html.escape(item.url)}\" target=\"_blank\" rel=\"noopener noreferrer\">{html.escape(item.title)}</a></h3>
                <p class=\"summary\">{html.escape(summary)}</p>
                <p class=\"meta\">{html.escape(item.source)} · {published} · relevance {item.score}</p>
              </div>
            </article>
            """)
        cards.append(f"""
        <section class=\"section-card\">
          <h2>{html.escape(section)}</h2>
          {''.join(item_html)}
        </section>
        """)

    error_block = ""
    if errors:
        compact = "；".join(errors[:6])
        error_block = f"<p class=\"errors\">部分来源抓取失败：{html.escape(compact)}</p>"

    empty_block = "" if total_items else "<p class=\"empty\">今天暂未抓取到符合时间窗口的新闻。请稍后重试或检查 RSS 来源。</p>"

    return f"""<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <meta name=\"color-scheme\" content=\"light dark\" />
  <title>{html.escape(title)} · {date_str}</title>
  <style>
    :root {{ --bg:#0b1020; --panel:#111936; --muted:#92a0bd; --text:#eef3ff; --accent:#78a6ff; --line:#263354; }}
    @media (prefers-color-scheme: light) {{ :root {{ --bg:#f5f7fb; --panel:#ffffff; --muted:#60708f; --text:#172033; --accent:#245bd8; --line:#dce3f2; }} }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,"Noto Sans SC",sans-serif; background: var(--bg); color: var(--text); line-height: 1.65; }}
    .wrap {{ max-width: 980px; margin: 0 auto; padding: 28px 18px 56px; }}
    header {{ padding: 26px 0 18px; }}
    h1 {{ margin: 0 0 8px; font-size: clamp(28px, 5vw, 44px); letter-spacing: -0.03em; }}
    .subtitle {{ color: var(--muted); margin: 0; }}
    .toolbar {{ display:flex; gap:12px; flex-wrap:wrap; margin-top:18px; }}
    .btn {{ border:1px solid var(--line); border-radius:999px; padding:8px 13px; color:var(--text); text-decoration:none; background:rgba(255,255,255,.04); }}
    .section-card {{ border:1px solid var(--line); background:var(--panel); border-radius:22px; padding:18px; margin:18px 0; box-shadow: 0 18px 50px rgba(0,0,0,.16); }}
    h2 {{ margin: 0 0 14px; font-size: 22px; }}
    .item {{ display:grid; grid-template-columns: 34px 1fr; gap:13px; padding:15px 0; border-top:1px solid var(--line); }}
    .item:first-of-type {{ border-top:0; }}
    .rank {{ width:30px; height:30px; border-radius:50%; background:rgba(120,166,255,.18); color:var(--accent); display:flex; align-items:center; justify-content:center; font-weight:700; }}
    h3 {{ margin: 0 0 6px; font-size: 18px; line-height:1.35; }}
    a {{ color: var(--accent); }}
    .summary {{ margin: 0 0 5px; }}
    .meta, .errors, .empty {{ color: var(--muted); font-size: 14px; }}
    footer {{ color: var(--muted); border-top:1px solid var(--line); padding-top:18px; margin-top:26px; font-size:14px; }}
  </style>
</head>
<body>
  <main class=\"wrap\">
    <header>
      <h1>每日新闻简报</h1>
      <p class=\"subtitle\">{date_str} · 生成时间 {now.strftime('%Y-%m-%d %H:%M %Z')} · 共 {total_items} 条</p>
      <nav class=\"toolbar\">
        <a class=\"btn\" href=\"../index.html\">今日最新</a>
        <a class=\"btn\" href=\"../archive.html\">历史归档</a>
      </nav>
    </header>
    {error_block}
    {empty_block}
    {''.join(cards)}
    <footer>
      <p>自动生成，仅作信息筛选，不构成投资建议。新闻版权归原媒体所有。</p>
    </footer>
  </main>
</body>
</html>
"""


def render_index(config: dict[str, Any], date_str: str, page_path: str) -> str:
    title = config.get("site", {}).get("title", "Daily News Briefing")
    return f"""<!doctype html>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>{html.escape(title)}</title>
<meta http-equiv=\"refresh\" content=\"0; url={html.escape(page_path)}\">
<style>body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:720px;margin:64px auto;padding:0 20px;line-height:1.7}}</style>
<h1>每日新闻简报</h1>
<p>最新日报：<a href=\"{html.escape(page_path)}\">{date_str}</a></p>
<p><a href=\"archive.html\">查看历史归档</a></p>
"""


def render_archive(config: dict[str, Any]) -> str:
    title = config.get("site", {}).get("title", "Daily News Briefing")
    pages = sorted(BRIEFINGS_DIR.glob("*.html"), reverse=True)
    links = "\n".join(f"<li><a href=\"briefings/{p.name}\">{p.stem}</a></li>" for p in pages)
    return f"""<!doctype html>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>{html.escape(title)} · Archive</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:760px;margin:48px auto;padding:0 20px;line-height:1.7}} li{{margin:8px 0}}</style>
<h1>每日新闻简报归档</h1>
<p><a href=\"index.html\">返回最新日报</a></p>
<ul>{links}</ul>
"""


def run(cmd: list[str], cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=check)


def commit_and_push(date_str: str, do_push: bool) -> None:
    run(["git", "add", "README.md", ".gitignore", ".nojekyll", "config", "scripts", "briefings", "data", "index.html", "archive.html"])
    status = run(["git", "status", "--porcelain"], check=False).stdout.strip()
    if status:
        run(["git", "commit", "-m", f"chore: publish daily briefing {date_str}"])
    if do_push:
        run(["git", "push", "-u", "origin", "main"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Briefing date YYYY-MM-DD; defaults to site timezone today")
    parser.add_argument("--commit", action="store_true", help="Commit generated files if changed")
    parser.add_argument("--push", action="store_true", help="Push committed changes to origin/main")
    args = parser.parse_args()

    config = load_config()
    tz = ZoneInfo(config.get("site", {}).get("timezone", "Asia/Shanghai"))
    now = datetime.now(tz)
    date_str = args.date or now.strftime("%Y-%m-%d")
    page_name = f"{date_str}.html"
    page_rel = f"briefings/{page_name}"
    base_url = config.get("site", {}).get("base_url", "").rstrip("/")
    page_url = f"{base_url}/{page_rel}" if base_url else page_rel

    BRIEFINGS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    items, errors = collect_items(config, now)
    grouped = group_and_select(items, config)

    (BRIEFINGS_DIR / page_name).write_text(render_html(config, date_str, now, grouped, errors), encoding="utf-8")
    (ROOT / "index.html").write_text(render_index(config, date_str, page_rel), encoding="utf-8")
    (ROOT / "archive.html").write_text(render_archive(config), encoding="utf-8")
    latest = {
        "date": date_str,
        "generated_at": now.isoformat(),
        "url": page_url,
        "items": [asdict(item) for section_items in grouped.values() for item in section_items],
        "errors": errors,
    }
    (DATA_DIR / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.commit or args.push:
        commit_and_push(date_str, args.push)

    print(page_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
