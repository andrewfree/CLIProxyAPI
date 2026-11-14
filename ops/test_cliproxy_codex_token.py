from pathlib import Path
import subprocess
import tempfile
import unittest


HELPER = Path(__file__).with_name("cliproxy-codex-token")


class CodexTokenTest(unittest.TestCase):
    def test_config_formats(self):
        cases = (
            ("legacy", "api-keys: [legacy-first, legacy-second]", "legacy-first"),
            (
                "v8",
                "access:\n  api-keys: [v8-first, v8-second]\n"
                "api-keys:\n  openai-compatibility: [UPSTREAM_SENTINEL]",
                "v8-first",
            ),
            (
                "v8 takes precedence",
                "access:\n  api-keys: [v8-first]\napi-keys: [legacy-first]",
                "v8-first",
            ),
            (
                "legacy with unrelated access settings",
                "access:\n  policy: strict\napi-keys: [legacy-first]",
                "legacy-first",
            ),
            ("root provider map", "api-keys: {providers: [UPSTREAM_SENTINEL]}", None),
            (
                "malformed nested field",
                "access:\n  api-keys: {provider: UPSTREAM_SENTINEL}\n"
                "api-keys: [legacy-first]",
                None,
            ),
            ("null nested field", "access:\n  api-keys: null\napi-keys: [legacy-first]", None),
            ("malformed access block", "access: [UPSTREAM_SENTINEL]\napi-keys: [legacy-first]", None),
            ("empty nested list", "access:\n  api-keys: []\napi-keys: [legacy-first]", None),
            ("blank first key", "api-keys: ['  ', later-key]", None),
            ("nonstring first key", "api-keys: [123, later-key]", None),
            ("mixed list", "api-keys: [legacy-first, {provider: UPSTREAM_SENTINEL}]", None),
            ("malformed YAML", "api-keys: [UPSTREAM_SENTINEL", None),
            ("nonmapping document", "[UPSTREAM_SENTINEL]", None),
            ("missing file", None, None),
        )
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            for name, yaml, expected in cases:
                with self.subTest(name=name):
                    if yaml is None:
                        config.unlink(missing_ok=True)
                    else:
                        config.write_text(yaml + "\n", encoding="utf-8")
                    result = subprocess.run(
                        [str(HELPER), str(config)], capture_output=True, text=True, timeout=5
                    )
                    self.assertNotIn("UPSTREAM_SENTINEL", result.stdout + result.stderr)
                    if expected is None:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(result.stdout, "")
                    else:
                        self.assertEqual(result.returncode, 0)
                        self.assertEqual(result.stdout, expected)


if __name__ == "__main__":
    unittest.main()
