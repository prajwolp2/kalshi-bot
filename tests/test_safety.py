"""Static checks that the package cannot trade. A scan, not a proof: read http.py too."""

import re
import unittest
from pathlib import Path

from kalshi_paper import http

PKG = Path(__file__).resolve().parent.parent / "kalshi_paper"

FORBIDDEN = [
    r"portfolio",        # Kalshi's authenticated endpoints (orders, balance, positions) live under /portfolio
    r"KALSHI-ACCESS",    # Kalshi's request-signing headers
    r"private_key", r"PRIVATE KEY", r"api_key", r"cryptography",
    r"\bPOST\b", r"\bPUT\b", r"\bDELETE\b", r"\bPATCH\b",
    r"create_order", r"place_order",
]


class SafetyTests(unittest.TestCase):
    def test_no_trading_or_signing_code(self):
        for f in sorted(PKG.glob("*.py")):
            text = f.read_text()
            for pat in FORBIDDEN:
                with self.subTest(file=f.name, pattern=pat):
                    self.assertIsNone(re.search(pat, text))

    def test_only_one_request_builder_and_it_is_get(self):
        text = (PKG / "http.py").read_text()
        self.assertEqual(text.count("urllib.request.Request("), 1)
        self.assertIn('method="GET"', text)
        for f in PKG.glob("*.py"):
            if f.name != "http.py":
                self.assertNotIn("urlopen", f.read_text(), f.name)
                self.assertNotIn("urllib.request", f.read_text(), f.name)

    def test_refuses_other_hosts_and_plain_http(self):
        def transport(url, timeout):
            self.fail("transport must not be reached")

        for url in ("https://example.com/x",
                    "http://api.elections.kalshi.com/trade-api/v2/markets",
                    "https://api.elections.kalshi.com.evil.example/x"):
            with self.subTest(url), self.assertRaises(ValueError):
                http.get(url, transport)


if __name__ == "__main__":
    unittest.main()
