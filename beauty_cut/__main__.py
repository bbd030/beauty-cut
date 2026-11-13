"""Run from the project root with python -m beauty_cut."""

import argparse
import json
from pathlib import Path

from beauty_cut.integrity import ManifestError, verify_manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="BeautyCut local video preparation")
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="Verify a batch against SHA-256 hashes")
    verify.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        report = verify_manifest(args.manifest)
    except ManifestError as exc:
        print(json.dumps({"schema_version": 1, "error": str(exc)}, ensure_ascii=True))
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 1 if report["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
