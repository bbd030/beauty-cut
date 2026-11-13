"""Streaming checksum verification with per-file failure isolation."""

import hashlib
import json
from pathlib import Path
import re


class ManifestError(ValueError):
    """The manifest cannot be read or does not contain a nonempty object."""


def verify_manifest(manifest_path: Path) -> dict:
    """Verify files relative to a manifest; never read outside its directory.

    A broken manifest raises ManifestError. A broken entry becomes a failed
    result, allowing the rest of the batch to complete. Files are read in
    bounded chunks; this verifies bytes, not video decoding or shot quality.
    """
    manifest_path = Path(manifest_path)
    try:
        entries = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ManifestError(f"Cannot read manifest: {exc}") from exc
    if not isinstance(entries, dict) or not entries:
        raise ManifestError("Manifest must be a nonempty JSON object")

    root = manifest_path.resolve().parent
    results = []
    for filename, expected in entries.items():
        result = {
            "file": filename,
            "status": "failed",
            "expected_sha256": expected if isinstance(expected, str) else None,
            "actual_sha256": None,
            "error": None,
        }
        try:
            if not isinstance(expected, str) or not re.fullmatch(
                r"[0-9a-fA-F]{64}", expected
            ):
                raise ValueError("Expected SHA-256 must contain 64 hexadecimal digits")
            if not filename:
                raise ValueError("File name must not be empty")
            target = (root / filename).resolve()
            if not target.is_relative_to(root):
                raise ValueError("File path escapes the manifest directory")
            if not target.is_file():
                raise ValueError("File is missing or is not a regular file")
            digest = hashlib.sha256()
            with target.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            actual = digest.hexdigest()
            result["actual_sha256"] = actual
            if actual != expected.lower():
                raise ValueError("SHA-256 mismatch")
            result["status"] = "passed"
        except (OSError, ValueError, RuntimeError) as exc:
            result["error"] = str(exc)
        results.append(result)

    passed = sum(item["status"] == "passed" for item in results)
    return {
        "schema_version": 1,
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
        },
        "results": results,
    }
