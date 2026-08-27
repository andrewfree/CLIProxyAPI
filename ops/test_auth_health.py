import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ops.auth_health import format_text, scan_auth_directory


class AuthHealthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.auth_dir = self.root / "auths"
        self.auth_dir.mkdir()
        self.log_path = self.root / "main.log"
        self.now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_auth(self, name: str, **overrides: object) -> Path:
        payload: dict[str, object] = {
            "type": "codex",
            "email": "person@example.com",
            "expired": (self.now + timedelta(days=10)).isoformat(),
            "last_refresh": (self.now - timedelta(hours=1)).isoformat(),
            "disabled": False,
            "access_token": "SECRET_ACCESS_SENTINEL",
            "refresh_token": "SECRET_REFRESH_SENTINEL",
            "id_token": "SECRET_ID_SENTINEL",
        }
        payload.update(overrides)
        path = self.auth_dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_invalid_grant_is_critical_without_exposing_secrets(self) -> None:
        name = "claude-person@example.com.json"
        self.write_auth(
            name,
            type="claude",
            expired=(self.now - timedelta(days=2)).isoformat(),
        )
        self.log_path.write_text(
            "[time] refreshed claude, "
            f"{name}, token refresh failed after 3 attempts: invalid_grant\n",
            encoding="utf-8",
        )

        statuses = scan_auth_directory(
            self.auth_dir, self.log_path, now=self.now, warn_days=3
        )

        self.assertEqual(statuses[0].level, "critical")
        self.assertEqual(statuses[0].refresh_state, "invalid_grant")
        self.assertEqual(statuses[0].refresh_failures, 1)
        rendered = format_text(statuses, now=self.now)
        self.assertNotIn("SECRET_ACCESS_SENTINEL", rendered)
        self.assertNotIn("SECRET_REFRESH_SENTINEL", rendered)
        self.assertNotIn("SECRET_ID_SENTINEL", rendered)

    def test_latest_success_clears_old_failure_for_short_lived_token(self) -> None:
        name = "kimi-device.json"
        self.write_auth(
            name,
            type="kimi",
            email=None,
            expired=(self.now + timedelta(minutes=12)).isoformat(),
            last_refresh=(self.now - timedelta(minutes=3)).isoformat(),
        )
        self.log_path.write_text(
            "\n".join(
                [
                    f"[old] refreshed kimi, {name}, invalid_grant",
                    f"[new] refreshed kimi, {name}, <nil>",
                ]
            ),
            encoding="utf-8",
        )

        status = scan_auth_directory(
            self.auth_dir, self.log_path, now=self.now, warn_days=3
        )[0]

        self.assertEqual(status.level, "ok")
        self.assertEqual(status.refresh_state, "ok")
        self.assertEqual(status.refresh_failures, 0)
        self.assertIn("short_lived_auto_refresh_ok", status.reasons)

    def test_refresh_write_event_does_not_erase_success(self) -> None:
        name = "kimi-device.json"
        self.write_auth(
            name,
            type="kimi",
            email=None,
            expired=(self.now + timedelta(minutes=12)).isoformat(),
            last_refresh=(self.now - timedelta(minutes=3)).isoformat(),
        )
        self.log_path.write_text(
            "\n".join(
                [
                    f"[refresh] refreshed kimi, {name}, <nil>",
                    f"[write] auth file changed (WRITE): {name}, processing incrementally",
                ]
            ),
            encoding="utf-8",
        )

        status = scan_auth_directory(
            self.auth_dir, self.log_path, now=self.now, warn_days=3
        )[0]

        self.assertEqual(status.level, "ok")
        self.assertEqual(status.refresh_state, "ok")
        self.assertEqual(status.refresh_failures, 0)
        self.assertIn("short_lived_auto_refresh_ok", status.reasons)

    def test_reauthenticated_claude_clears_pre_login_invalid_grant(self) -> None:
        name = "claude-person@example.com.json"
        self.write_auth(
            name,
            type="claude",
            expired=(self.now + timedelta(hours=8)).isoformat(),
            last_refresh=(self.now - timedelta(minutes=1)).isoformat(),
        )
        self.log_path.write_text(
            "\n".join(
                [
                    f"[old] refreshed claude, {name}, invalid_grant",
                    f"[new] auth file changed (WRITE): {name}, processing incrementally",
                ]
            ),
            encoding="utf-8",
        )

        status = scan_auth_directory(
            self.auth_dir, self.log_path, now=self.now, warn_days=3
        )[0]

        self.assertEqual(status.level, "ok")
        self.assertEqual(status.refresh_state, "credential_reloaded")
        self.assertEqual(status.refresh_failures, 0)
        self.assertIn("short_lived_recent_grant", status.reasons)

    def test_invalid_grant_after_reauthentication_remains_critical(self) -> None:
        name = "claude-person@example.com.json"
        self.write_auth(
            name,
            type="claude",
            expired=(self.now + timedelta(hours=8)).isoformat(),
            last_refresh=(self.now - timedelta(minutes=1)).isoformat(),
        )
        self.log_path.write_text(
            "\n".join(
                [
                    f"[old] refreshed claude, {name}, invalid_grant",
                    f"[new] auth file changed (WRITE): {name}, processing incrementally",
                    f"[newer] refreshed claude, {name}, invalid_grant",
                ]
            ),
            encoding="utf-8",
        )

        status = scan_auth_directory(
            self.auth_dir, self.log_path, now=self.now, warn_days=3
        )[0]

        self.assertEqual(status.level, "critical")
        self.assertEqual(status.refresh_state, "invalid_grant")
        self.assertEqual(status.refresh_failures, 1)

    def test_disabled_auth_is_critical(self) -> None:
        self.write_auth("codex-disabled.json", disabled=True)

        status = scan_auth_directory(
            self.auth_dir, self.log_path, now=self.now, warn_days=3
        )[0]

        self.assertEqual(status.level, "critical")
        self.assertIn("disabled", status.reasons)

    def test_long_lived_auth_expiring_within_window_warns(self) -> None:
        self.write_auth(
            "codex-expiring.json",
            expired=(self.now + timedelta(days=2)).isoformat(),
        )

        status = scan_auth_directory(
            self.auth_dir, self.log_path, now=self.now, warn_days=3
        )[0]

        self.assertEqual(status.level, "warning")
        self.assertIn("expires_soon", status.reasons)


if __name__ == "__main__":
    unittest.main()
