import datetime
import hashlib
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ops import backup_log_archive as backup

AGE = shutil.which("age") or backup.AGE
ZSTD = shutil.which("zstd") or backup.ZSTD
KEYGEN = shutil.which("age-keygen")
TOOLS = all(Path(p).exists() for p in (AGE, ZSTD)) and KEYGEN is not None
TODAY = datetime.date(2026, 11, 25)


@unittest.skipUnless(TOOLS, "age, age-keygen and zstd are required")
class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.archive = root / "archive"
        self.backup_root = root / "innie" / "backups"
        self.identity = root / "identity.agekey"
        subprocess.run([KEYGEN, "-o", str(self.identity)], check=True, capture_output=True)
        recipient = subprocess.run([KEYGEN, "-y", str(self.identity)], check=True,
                                   capture_output=True, text=True).stdout.strip()
        self.config = backup.Config(self.archive, self.backup_root, root / "innie", recipient, AGE, ZSTD, 3)
        self.manifest = self.archive / "SHA256SUMS"
        self.archive.mkdir()
        (root / "innie").mkdir()  # the mounted volume exists before the backup folder
        self.manifest.write_text("")
        mounted = mock.patch.object(backup.os.path, "ismount", return_value=True)
        mounted.start()
        self.addCleanup(mounted.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, day, name, content, record=None):
        relative = f"requests/{day.replace('-', '/')}/{name}"
        path = self.archive / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        digest = record or hashlib.sha256(content).hexdigest()
        with self.manifest.open("a") as stream:
            stream.write(f"{digest}  {relative}\n")
        return relative

    def parts(self, day):
        return [p.name for p in backup.committed_parts(self.backup_root, day)]

    def test_closed_days_are_packed_and_restore_exactly(self):
        first = self.add("2026-11-22", "a.log", b"alpha" * 1000)
        second = self.add("2026-11-22", "b.log", b"beta")
        self.add("2026-11-24", "recent.log", b"still open")  # the last full day waits
        summary = backup.back_up(self.config, TODAY)
        self.assertEqual(summary, {"parts_written": 1, "files_written": 2})
        self.assertEqual(self.parts("2026-11-22"), ["2026-11-22.part01"])
        self.assertEqual(self.parts("2026-11-24"), [])
        base = self.backup_root / "2026-11-22.part01"
        self.assertEqual({r for _, r in backup.read_members(base)}, {first, second})
        self.assertEqual(backup.verify_all(self.config, self.identity), {"parts_verified": 1})
        self.assertTrue(Path(str(base) + ".verified").exists())
        self.assertNotIn(b"alpha", Path(str(base) + ".tar.zst.age").read_bytes())

    def test_a_file_arriving_later_goes_into_a_new_part(self):
        self.add("2026-11-22", "a.log", b"alpha")
        backup.back_up(self.config, TODAY)
        late = self.add("2026-11-22", "late.log", b"late")
        self.assertEqual(backup.back_up(self.config, TODAY), {"parts_written": 1, "files_written": 1})
        self.assertEqual(self.parts("2026-11-22"), ["2026-11-22.part01", "2026-11-22.part02"])
        self.assertEqual(backup.read_members(self.backup_root / "2026-11-22.part02"),
                         [(hashlib.sha256(b"late").hexdigest(), late)])
        self.assertEqual(backup.back_up(self.config, TODAY), {"parts_written": 0, "files_written": 0})

    def test_a_file_that_no_longer_matches_its_manifest_is_refused(self):
        self.add("2026-11-22", "a.log", b"changed", record=hashlib.sha256(b"original").hexdigest())
        with self.assertRaisesRegex(backup.BackupError, "does not match its manifest"):
            backup.back_up(self.config, TODAY)
        self.assertEqual(self.parts("2026-11-22"), [])

    def test_part_numbers_are_never_reused(self):
        self.add("2026-11-22", "a.log", b"alpha")
        self.backup_root.mkdir(parents=True)
        leftover = self.backup_root / "2026-11-22.part01.tar.zst.age.partial"
        leftover.write_bytes(b"from a crashed run")
        backup.back_up(self.config, TODAY)
        self.assertEqual(self.parts("2026-11-22"), ["2026-11-22.part02"])
        self.assertEqual(leftover.read_bytes(), b"from a crashed run")

    def test_verify_catches_a_changed_ciphertext(self):
        self.add("2026-11-22", "a.log", b"alpha")
        backup.back_up(self.config, TODAY)
        cipher = self.backup_root / "2026-11-22.part01.tar.zst.age"
        data = bytearray(cipher.read_bytes())
        data[-1] ^= 1
        cipher.write_bytes(bytes(data))
        with self.assertRaisesRegex(backup.BackupError, "does not match its .sha256"):
            backup.verify_all(self.config, self.identity)

    def test_verify_catches_a_members_list_that_disagrees(self):
        self.add("2026-11-22", "a.log", b"alpha")
        backup.back_up(self.config, TODAY)
        members = self.backup_root / "2026-11-22.part01.members"
        members.write_text(members.read_text().replace(hashlib.sha256(b"alpha").hexdigest(), "0" * 64))
        with self.assertRaisesRegex(backup.BackupError, "members differ"):
            backup.verify_all(self.config, self.identity)

    def test_an_unterminated_manifest_line_is_ignored(self):
        self.add("2026-11-22", "a.log", b"alpha")
        with self.manifest.open("a") as stream:
            stream.write("abc")
        self.assertEqual(backup.back_up(self.config, TODAY)["files_written"], 1)

    def test_a_malformed_recipient_is_refused_before_writing(self):
        self.add("2026-11-22", "a.log", b"alpha")
        config = backup.Config(self.archive, self.backup_root, self.config.backup_mount, "nope", AGE, ZSTD)
        with self.assertRaisesRegex(backup.BackupError, "recipient"):
            backup.back_up(config, TODAY)
        self.assertEqual(list(self.backup_root.iterdir()), [])

    def test_a_part_whose_ciphertext_vanished_is_packed_again(self):
        self.add("2026-11-22", "a.log", b"alpha")
        backup.back_up(self.config, TODAY)
        (self.backup_root / "2026-11-22.part01.tar.zst.age").unlink()
        self.assertEqual(backup.back_up(self.config, TODAY), {"parts_written": 1, "files_written": 1})
        self.assertEqual(self.parts("2026-11-22"), ["2026-11-22.part02"])

    def test_a_file_that_grows_while_packed_is_refused(self):
        relative = self.add("2026-11-22", "a.log", b"alpha")
        path = self.archive / relative
        real_read = backup.HashingReader.read

        def read_and_append(reader, size=-1):
            data = real_read(reader, size)
            with path.open("ab") as stream:
                stream.write(b"-late")
            return data

        with mock.patch.object(backup.HashingReader, "read", read_and_append):
            with self.assertRaisesRegex(backup.BackupError, "changed while it was packed"):
                backup.back_up(self.config, TODAY)
        self.assertEqual(self.parts("2026-11-22"), [])

    def test_a_wrong_identity_fails_cleanly(self):
        self.add("2026-11-22", "a.log", b"alpha")
        backup.back_up(self.config, TODAY)
        other = Path(self.tmp.name) / "other.agekey"
        subprocess.run([KEYGEN, "-o", str(other)], check=True, capture_output=True)
        with self.assertRaises(backup.BackupError):
            backup.verify_all(self.config, other)

    def test_verify_catches_a_duplicated_member(self):
        relative = self.add("2026-11-22", "a.log", b"alpha")
        backup.back_up(self.config, TODAY)
        base = self.backup_root / "2026-11-22.part01"
        cipher = Path(str(base) + ".tar.zst.age")
        tar_path = Path(self.tmp.name) / "dup.tar"
        import tarfile
        with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tar:
            for _ in range(2):
                tar.add(self.archive / relative, arcname=relative)
        packed = subprocess.run([ZSTD, "-q", "-c", str(tar_path)], check=True, capture_output=True).stdout
        subprocess.run([AGE, "-r", self.config.recipient, "-o", str(cipher)], input=packed, check=True)
        Path(str(base) + ".sha256").write_text(f"{backup.sha256_file(cipher)}  {cipher.name}\n")
        with self.assertRaisesRegex(backup.BackupError, "twice"):
            backup.verify_all(self.config, self.identity)

    def test_verifying_a_day_without_parts_fails(self):
        self.add("2026-11-22", "a.log", b"alpha")
        backup.back_up(self.config, TODAY)
        with self.assertRaisesRegex(backup.BackupError, "no committed part for 2026-11-21"):
            backup.verify_all(self.config, self.identity, ["2026-11-21"])
        self.assertEqual(backup.verify_all(self.config, self.identity, ["2026-11-22"]), {"parts_verified": 1})

    def test_a_refused_file_leaves_no_running_children(self):
        self.add("2026-11-22", "a.log", b"changed", record=hashlib.sha256(b"original").hexdigest())
        before = set(subprocess.run(["pgrep", "-P", str(os.getpid())], capture_output=True, text=True).stdout.split())
        with self.assertRaises(backup.BackupError):
            backup.back_up(self.config, TODAY)
        after = set(subprocess.run(["pgrep", "-P", str(os.getpid())], capture_output=True, text=True).stdout.split())
        self.assertEqual(after - before, set())


class DefaultsTest(unittest.TestCase):
    def test_defaults_match_the_shipped_launch_agent(self):
        plist = Path(__file__).resolve().parent / "com.rever.cliproxy-log-backup.plist"
        environment = plistlib.loads(plist.read_bytes())["EnvironmentVariables"]
        self.assertEqual(str(backup.DEFAULT_ARCHIVE_ROOT), environment["CLIPROXY_LOG_ARCHIVE_ROOT"])
        self.assertEqual(str(backup.DEFAULT_BACKUP_ROOT), environment["CLIPROXY_LOG_BACKUP_ROOT"])
        self.assertEqual(str(backup.DEFAULT_BACKUP_MOUNT), environment["CLIPROXY_LOG_BACKUP_MOUNT"])
        self.assertRegex(environment["CLIPROXY_LOG_BACKUP_RECIPIENT"], backup.RECIPIENT)


if __name__ == "__main__":
    unittest.main()
