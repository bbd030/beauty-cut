"""Run from the project root with python -m beauty_cut."""

import argparse
import json
from pathlib import Path

from beauty_cut.integrity import ManifestError, verify_manifest
from beauty_cut.probe import ProbeError, probe_videos


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="BeautyCut local video preparation")
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="Verify a batch against SHA-256 hashes")
    verify.add_argument("--manifest", type=Path, required=True)
    probe = commands.add_parser("probe", help="Inspect and fully decode local videos")
    probe.add_argument("files", nargs="+", type=Path)
    probe.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable name or path")
    probe.add_argument("--ffprobe", default="ffprobe", help="FFprobe executable name or path")
    probe.add_argument("--timeout", type=float, default=600,
                       help="Seconds per probe/decode process (default: 600)")
    args = parser.parse_args(argv)

    try:
        if args.command == "verify":
            report = verify_manifest(args.manifest)
        else:
            report = probe_videos(args.files, ffmpeg=args.ffmpeg,
                                  ffprobe=args.ffprobe, timeout=args.timeout)
    except (ManifestError, ProbeError) as exc:
        print(json.dumps({"schema_version": 1, "error": str(exc)}, ensure_ascii=True))
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 1 if report["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
