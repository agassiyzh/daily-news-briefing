# Daily News Briefing

Personal daily news briefing published as static GitHub Pages.

- Site: https://agassiyzh.github.io/daily-news-briefing/
- Daily pages: `briefings/YYYY-MM-DD.html`
- Generator: `scripts/generate_daily.py`

## Local run

```bash
python3 scripts/generate_daily.py
```

The generator fetches RSS/Atom sources from `config/news_sources.json`, builds a daily HTML page, updates `index.html`, commits changes when run with `--commit`, and prints the GitHub Pages URL.
