"""Offline tests for the self-contained quota history chart."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from ops import fleet_quota_chart as chart


class ChartTests(unittest.TestCase):
    def test_filters_time_and_account_and_escapes_labels(self):
        end = datetime(2026, 11, 18, 10, tzinfo=timezone.utc)
        start = end - timedelta(days=1)

        def sample(at, percent, *, account_id="first", label="<script>alert(1)</script>"):
            return {"captured_at": at.isoformat(), "provider": "claude", "account_id": account_id,
                    "label": label, "windows": [{"window": "weekly", "used_percent": percent}]}

        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "history.jsonl"
            history.write_text("\n".join(json.dumps(row) for row in (
                sample(start - timedelta(minutes=1), 99),
                sample(start + timedelta(hours=3), 23),
                sample(start + timedelta(hours=3, minutes=5), 32),
                sample(end, 91),
                sample(end, 42, account_id="second", label="t3-demo"),
                sample(end + timedelta(minutes=1), 98),
            )) + "\npartial")
            html = chart.render(history, start, end, "first", "weekly")
            self.assertIn("&lt;script&gt;", html)
            self.assertNotIn("<script>", html)
            self.assertNotIn("t3-demo", html)
            self.assertIn("weekly · 91%", html)
            self.assertRegex(html, r'<path d="M[^M]+L[^M]+M[^M]+?"')
            self.assertIn("No quota readings", chart.render(history, start, end, window="absent"))

    def test_monthly_history_opens_only_months_in_range(self):
        start = datetime(2026, 11, 30, 23, tzinfo=timezone.utc)
        end = start + timedelta(hours=2)
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory)
            # An out-of-range shard raises if the reader ever opens it as a file.
            (history / "quota-history-2026-10.jsonl").mkdir()
            for at, used in ((start, 91), (end, 97)):
                path = history / f"quota-history-{at:%Y-%m}.jsonl"
                path.write_text(json.dumps({"provider": "claude", "label": "fixture", "account_id": "first",
                                            "captured_at": at.isoformat(),
                                            "windows": [{"window": "weekly", "used_percent": used}]}))
            series = chart.read_series(history, start, end)
            self.assertEqual(series, [("claude", "fixture", {"weekly": [(start, 91), (end, 97)]})])

    def test_cli_creates_private_html_at_selected_path(self):
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "history.jsonl"
            output = Path(directory) / "chart.html"
            history.touch()
            with patch("builtins.print") as receipt:
                self.assertEqual(chart.main(["--history", str(history), "--output", str(output), "--days", "2"]), 0)
            receipt.assert_called_once_with(output)
            self.assertIn("No quota readings", output.read_text())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
