"""Run from the project root with python -m beauty_cut."""

import argparse
import json
from pathlib import Path

from beauty_cut.integrity import ManifestError, verify_manifest
from beauty_cut.probe import ProbeError, probe_videos
from beauty_cut.detect import DetectionError, DEFAULT_THRESHOLD, detect_videos
from beauty_cut.preview import PreviewError, preview_videos


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="BeautyCut local video analysis")
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="Verify a batch against SHA-256 hashes")
    verify.add_argument("--manifest", type=Path, required=True)
    probe = commands.add_parser("probe", help="Inspect and fully decode local videos")
    probe.add_argument("files", nargs="+", type=Path)
    probe.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable name or path")
    probe.add_argument("--ffprobe", default="ffprobe", help="FFprobe executable name or path")
    probe.add_argument("--timeout", type=float, default=600,
                       help="Seconds per probe/decode process (default: 600)")
    detect = commands.add_parser("detect", help="Detect visual cuts and output shot times")
    detect.add_argument("files", nargs="+", type=Path)
    detect.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable name or path")
    detect.add_argument("--ffprobe", default="ffprobe", help="FFprobe executable name or path")
    detect.add_argument("--timeout", type=float, default=600,
                        help="Seconds per probe/analysis process (default: 600)")
    detect.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="Change sensitivity in (0,100]; lower detects more cuts (default: 5)")
    preview = commands.add_parser("preview", help="Generate local shot images and offline review report")
    preview.add_argument("files", nargs="+", type=Path)
    preview.add_argument("--output-dir", type=Path, required=True)
    preview.add_argument("--ffmpeg", default="ffmpeg")
    preview.add_argument("--ffprobe", default="ffprobe")
    preview.add_argument("--timeout", type=float, default=600)
    preview.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = parser.parse_args(argv)

    try:
        if args.command == "verify":
            report = verify_manifest(args.manifest)
        elif args.command == "probe":
            report = probe_videos(args.files, ffmpeg=args.ffmpeg,
                                  ffprobe=args.ffprobe, timeout=args.timeout)
        elif args.command == "detect":
            report = detect_videos(args.files, ffmpeg=args.ffmpeg,
                                   ffprobe=args.ffprobe, timeout=args.timeout,
                                   threshold=args.threshold)
        else:
            report = preview_videos(args.files, output_dir=args.output_dir, ffmpeg=args.ffmpeg,
                                    ffprobe=args.ffprobe, timeout=args.timeout,
                                    threshold=args.threshold)
    except (ManifestError, ProbeError, DetectionError, PreviewError) as exc:
        print(json.dumps({"schema_version": 1, "error": str(exc)}, ensure_ascii=True))
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 1 if report["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
