"""Local video metadata and full, CPU-only decoding checks."""

from fractions import Fraction
import json
import math
from pathlib import Path
import subprocess
import tempfile


class ProbeError(ValueError):
    """Invalid batch configuration or unavailable video tools."""


def _run(command, timeout):
    # Files keep subprocess diagnostics bounded in memory, even on bad inputs.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
            timeout=timeout, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output.seek(0)
        raw = output.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Tool output exceeds 1 MiB")
        errors.seek(0, 2)
        errors.seek(max(0, errors.tell() - 4096))
        detail = errors.read().decode("utf-8", errors="replace").strip()
        if process.returncode:
            raise ValueError(f"Tool exited with code {process.returncode}: {detail}")
        return raw.decode("utf-8", errors="replace"), detail


def _positive_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _rate(value):
    try:
        rate = float(Fraction(value))
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None
    return rate if math.isfinite(rate) and rate > 0 else None


def _metadata(document):
    if not isinstance(document, dict):
        raise ValueError("FFprobe returned an invalid JSON object")
    streams = document.get("streams", [])
    if not isinstance(streams, list) or not streams:
        raise ValueError("No video stream (excluding cover art) found")
    stream = streams[0]
    if not isinstance(stream, dict):
        raise ValueError("Invalid video stream metadata")
    for key in ("width", "height"):
        if type(stream.get(key)) is not int or stream[key] <= 0:
            raise ValueError(f"Missing or invalid video {key}")
    codec = stream.get("codec_name")
    if not isinstance(codec, str) or not codec or codec == "unknown":
        raise ValueError("Missing or unknown video codec")
    container = document.get("format", {})
    if not isinstance(container, dict):
        raise ValueError("Invalid container metadata")
    rotation = None
    for side in stream.get("side_data_list", []):
        if isinstance(side, dict) and "rotation" in side:
            try:
                candidate = float(side["rotation"])
                if math.isfinite(candidate):
                    rotation = candidate
            except (ValueError, TypeError, OverflowError):
                pass
    return {
        "container": container.get("format_name"),
        "codec": codec,
        "width": stream["width"],
        "height": stream["height"],
        "duration_seconds": _positive_number(stream.get("duration"))
        or _positive_number(container.get("duration")),
        "average_fps": _rate(stream.get("avg_frame_rate")),
        "nominal_fps": _rate(stream.get("r_frame_rate")),
        "time_base": stream.get("time_base"),
        "rotation_degrees": rotation,
    }


def probe_videos(files, *, ffmpeg="ffmpeg", ffprobe="ffprobe", timeout=600):
    """Check the first non-cover video stream in each file; isolate failures.

    timeout limits each metadata/decode process, not the whole batch. Missing
    optional metadata stays null; frame rate is never assumed to be constant.
    """
    files = list(files)
    if not files:
        raise ProbeError("Specify at least one local video file")
    if _positive_number(timeout) is None:
        raise ProbeError("Timeout must be a finite positive number of seconds")
    timeout = float(timeout)
    tools = {}
    for name, executable in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        try:
            output, _ = _run([str(executable), "-version"], min(timeout, 10))
            version = output.splitlines()[0] if output.splitlines() else ""
            if not version.startswith(f"{name} version "):
                raise ValueError(f"Unexpected {name} version response")
            tools[name] = version
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise ProbeError(
                f"{name} unavailable: {exc}. Install FFmpeg/FFprobe and use "
                f"PATH or --{name} to specify the executable."
            ) from exc

    results = []
    for filename in files:
        item = {
            "file": str(filename), "status": "failed", "metadata": None,
            "decode": {"status": "not_run", "frames": None}, "error": None,
        }
        stage = "input"
        try:
            path = Path(filename).resolve()
            if not path.is_file():
                raise ValueError("Input is missing or is not a regular file")
            stage = "probe"
            output, detail = _run([
                str(ffprobe), "-v", "error", "-protocol_whitelist", "file",
                "-select_streams", "V:0", "-show_entries",
                "stream=codec_name,width,height,avg_frame_rate,r_frame_rate,time_base,duration:"
                "stream_side_data=rotation:format=format_name,duration",
                "-of", "json", str(path),
            ], timeout)
            if detail:
                raise ValueError(detail)
            item["metadata"] = _metadata(json.loads(output))
            stage = "decode"
            item["decode"]["status"] = "failed"
            output, detail = _run([
                str(ffmpeg), "-hide_banner", "-v", "error", "-nostdin", "-nostats",
                "-xerror", "-max_error_rate", "0", "-progress", "pipe:1",
                "-protocol_whitelist", "file", "-hwaccel", "none",
                "-threads", "2", "-err_detect", "explode", "-noautorotate",
                "-i", str(path), "-map", "0:V:0", "-an", "-sn", "-dn",
                "-fps_mode", "passthrough", "-f", "null", "-",
            ], timeout)
            if detail:
                raise ValueError(detail)
            progress = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
            frames = int(progress.get("frame", "0").strip())
            if progress.get("progress") != "end" or frames <= 0:
                raise ValueError("Decode did not finish with at least one video frame")
            item["decode"] = {"status": "passed", "frames": frames}
            item["status"] = "passed"
        except subprocess.TimeoutExpired:
            item["error"] = f"{stage}: timed out after {timeout:g} seconds"
        except (OSError, ValueError, RuntimeError) as exc:
            item["error"] = f"{stage}: {exc}"
        results.append(item)
    passed = sum(item["status"] == "passed" for item in results)
    return {
        "schema_version": 1,
        "tools": tools,
        "summary": {"total": len(results), "passed": passed, "failed": len(results) - passed},
        "results": results,
    }
