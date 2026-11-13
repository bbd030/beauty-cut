import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from beauty_cut.__main__ import main
from beauty_cut.integrity import ManifestError, verify_manifest


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = self.root / "SHA256SUMS.json"

    def add_file(self, name, content=b"synthetic video bytes"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return hashlib.sha256(content).hexdigest()

    def write_manifest(self, entries):
        self.manifest.write_text(json.dumps(entries), encoding="utf-8")

    def test_valid_batch_and_unicode_nested_path(self):
        entries = {
            "sample.mp4": self.add_file("sample.mp4"),
            "子目录/素材.mp4": self.add_file("子目录/素材.mp4", b"second"),
        }
        self.write_manifest(entries)
        report = verify_manifest(self.manifest)
        self.assertEqual(report["summary"], {"total": 2, "passed": 2, "failed": 0})
        self.assertEqual([item["file"] for item in report["results"]], list(entries))
        for item in report["results"]:
            self.assertEqual(item["actual_sha256"], entries[item["file"]])
            self.assertIsNone(item["error"])

    def test_corrupt_and_missing_files_do_not_stop_later_file(self):
        expected = self.add_file("corrupt.mp4", b"original")
        (self.root / "corrupt.mp4").write_bytes(b"modified")
        good = self.add_file("good.mp4")
        self.write_manifest({"corrupt.mp4": expected, "missing.mp4": good, "good.mp4": good})
        report = verify_manifest(self.manifest)
        self.assertEqual(report["summary"], {"total": 3, "passed": 1, "failed": 2})
        self.assertIn("mismatch", report["results"][0]["error"])
        self.assertIn("missing", report["results"][1]["error"])
        self.assertEqual(report["results"][2]["status"], "passed")

    def test_bad_hash_values_do_not_stop_batch(self):
        digest = self.add_file("good.mp4")
        self.write_manifest({"bad.mp4": "not a hash", "null.mp4": None, "good.mp4": digest})
        report = verify_manifest(self.manifest)
        self.assertEqual(report["summary"]["failed"], 2)
        self.assertEqual(report["results"][-1]["status"], "passed")

    def test_uppercase_hash_is_accepted(self):
        self.write_manifest({"sample.mp4": self.add_file("sample.mp4").upper()})
        self.assertEqual(verify_manifest(self.manifest)["summary"]["passed"], 1)

    def test_utf8_bom_is_accepted(self):
        digest = self.add_file("sample.mp4")
        self.manifest.write_text(json.dumps({"sample.mp4": digest}), encoding="utf-8-sig")
        self.assertEqual(verify_manifest(self.manifest)["summary"]["passed"], 1)

    def test_malformed_manifest_is_explicit_error(self):
        for content in ("not json", "[]", "{}", "null"):
            with self.subTest(content=content):
                self.manifest.write_text(content, encoding="utf-8")
                with self.assertRaises(ManifestError):
                    verify_manifest(self.manifest)

    def test_missing_or_non_utf8_manifest_is_explicit_error(self):
        with self.assertRaises(ManifestError):
            verify_manifest(self.manifest)
        self.manifest.write_bytes(b"\xff\xff")
        with self.assertRaises(ManifestError):
            verify_manifest(self.manifest)

    def test_parent_escape_is_rejected_before_file_read(self):
        self.write_manifest({"../outside.mp4": "0" * 64})
        item = verify_manifest(self.manifest)["results"][0]
        self.assertIn("escapes", item["error"])
        self.assertIsNone(item["actual_sha256"])

    def test_directory_and_empty_name_are_rejected(self):
        self.write_manifest({".": "0" * 64, "": "0" * 64})
        report = verify_manifest(self.manifest)
        self.assertEqual(report["summary"]["failed"], 2)
        self.assertTrue(all(item["error"] for item in report["results"]))

    def test_unreadable_file_does_not_stop_batch(self):
        digest = self.add_file("blocked.mp4")
        self.write_manifest({"blocked.mp4": digest, "good.mp4": self.add_file("good.mp4")})
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.name == "blocked.mp4":
                raise PermissionError("Access denied for test")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", guarded_open):
            report = verify_manifest(self.manifest)
        self.assertIn("Access denied", report["results"][0]["error"])
        self.assertEqual(report["results"][1]["status"], "passed")

    def test_cli_json_and_exit_codes(self):
        for entries, expected_code in (({"missing.mp4": "0" * 64}, 1), ({}, 2)):
            with self.subTest(expected_code=expected_code):
                self.write_manifest(entries)
                stream = io.StringIO()
                with contextlib.redirect_stdout(stream):
                    code = main(["verify", "--manifest", str(self.manifest)])
                self.assertEqual(code, expected_code)
                self.assertEqual(json.loads(stream.getvalue())["schema_version"], 1)

    def test_module_entry_point_succeeds(self):
        self.write_manifest({"sample.mp4": self.add_file("sample.mp4")})
        process = subprocess.run(
            [sys.executable, "-m", "beauty_cut", "verify", "--manifest", str(self.manifest)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["summary"]["passed"], 1)


if __name__ == "__main__":
    unittest.main()
