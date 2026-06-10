import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import generate_daily  # noqa: E402


class RenderingTests(unittest.TestCase):
    def test_render_html_hides_raw_fetch_exception_details(self):
        config = {"site": {"title": "Test Briefing"}}
        now = generate_daily.datetime(2026, 6, 10, 8, 0, tzinfo=generate_daily.timezone.utc)
        errors = [
            "Reuters Business: <urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1016)>"
        ]

        html = generate_daily.render_html(config, "2026-06-10", now, {}, errors)

        self.assertIn("部分来源暂时不可用", html)
        self.assertIn("Reuters Business", html)
        self.assertNotIn("UNEXPECTED_EOF_WHILE_READING", html)
        self.assertNotIn("urlopen error", html)
        self.assertNotIn("_ssl.c", html)


if __name__ == "__main__":
    unittest.main()
