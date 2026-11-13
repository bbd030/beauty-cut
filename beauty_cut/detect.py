"""CPU shot detection using FFmpeg frame scores and presentation timestamps."""

from fractions import Fraction
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile

from beauty_cut.probe import _metadata, _positive_number, _run


class DetectionError(ValueError):
    """Invalid configuration or unavailable tools."""


ALGORITHM = "scdet-neighbours-v1"
DEFAULT_THRESHOLD = 5.0
_NUMBER = r"[-+0-9.eE]+"
_FRAME = re.compile(r"\bn:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:\S+")
_TIME_BASE = re.compile(r"config in time_base:\s*(\d+/\d+)")


def _parse_frames(lines):
    """Read disk-backed FFmpeg logs incrementally; never keep the log in RAM."""
    frames = []
    time_base = None
    pending = {}
    for line in lines:
        if "Parsed_showinfo_" in line:
            match = _TIME_BASE.search(line)
            if match:
                value = Fraction(match[1])
                if value <= 0 or (time_base is not None and value != time_base):
                    raise ValueError("Changing or invalid frame time base")
                time_base = value
            match = _FRAME.search(line)
            if not match:
                continue
            if time_base is None or int(match[1]) != len(frames):
                raise ValueError("Missing time base or discontinuous frame records")
            pts = int(match[2]) * time_base
            if frames and pts <= frames[-1]["pts"]:
                raise ValueError("Frame timestamps are not strictly increasing")
            duration = re.search(r"\bduration:\s*(-?\d+)", line)
            mean = re.search(r"\bmean:\[([^]]+)\]", line)
            stdev = re.search(r"\bstdev:\[([^]]+)\]", line)
            if set(pending) != {"score", "mafd"} or not mean or not stdev:
                raise ValueError("Missing scdet/showinfo frame signals")
            means = [float(v) for v in mean[1].split()]
            deviations = [float(v) for v in stdev[1].split()]
            if len(means) != 3 or len(deviations) != 3:
                raise ValueError("Expected three YUV analysis planes")
            frames.append({"pts": pts,
                           "duration": int(duration[1]) * time_base if duration else None,
                           "mean": means, "stdev": deviations, **pending})
            pending = {}
        elif "Parsed_metadata_" in line:
            for name in ("score", "mafd"):
                match = re.search(r"lavfi\.scd\." + name + "=(" + _NUMBER + ")", line)
                if match:
                    value = float(match[1])
                    if not math.isfinite(value) or not 0 <= value <= 100:
                        raise ValueError("Invalid scene change signal")
                    pending[name] = value
    if not frames or pending:
        raise ValueError("No complete video frames")
    return frames


def _analyse(path, ffmpeg, timeout):
    # showinfo preserves integer PTS/duration. No fps filter, no frame sampling,
    # no full-resolution image/video output. Tiny signatures go to a temporary
    # binary buffer; they are removed together with diagnostics after each file.
    filters = ("[0:V:0]split[scene][small];"
               "[scene]scale=w='min(320,iw)':h=-2,format=yuv420p,"
               "scdet=threshold=100,metadata=print,showinfo[analysis];"
               "[small]scale=32:32,format=gray[signature]")
    with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryFile() as features, \
            tempfile.TemporaryFile() as log:
        progress_path = Path(folder) / "progress.txt"
        process = subprocess.run([
            str(ffmpeg), "-hide_banner", "-loglevel", "level+info", "-nostdin",
            "-nostats", "-xerror", "-max_error_rate", "0", "-progress", str(progress_path),
            "-protocol_whitelist", "file", "-hwaccel", "none", "-threads", "2",
            "-err_detect", "explode", "-noautorotate", "-copyts", "-i", str(path),
            "-filter_complex_threads", "1", "-filter_complex", filters,
            "-map", "[analysis]", "-an", "-sn", "-dn",
            "-fps_mode", "passthrough", "-f", "null", "-",
            "-map", "[signature]", "-c:v", "rawvideo", "-threads", "1",
            "-fps_mode", "passthrough", "-enc_time_base", "demux", "-f", "rawvideo", "-",
        ], stdin=subprocess.DEVNULL, stdout=features, stderr=log, timeout=timeout,
            check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log.seek(0, 2)
        size = log.tell()
        log.seek(max(0, size - 4096))
        detail = log.read().decode("utf-8", "replace")
        if process.returncode:
            raise ValueError(f"FFmpeg exited with code {process.returncode}: {detail}")
        state = {}
        with progress_path.open(encoding="utf-8") as progress:
            for raw in progress:
                line = raw.strip()
                if "=" in line:
                    key, value = line.split("=", 1)
                    state[key] = value
        if state.get("progress") != "end":
            raise ValueError("Video analysis did not finish")
        log.seek(0)
        # Reject decode errors even if a particular tool build returned success.
        def checked_lines():
            for raw in log:
                line = raw.decode("utf-8", "replace")
                if "[error]" in line or "[fatal]" in line:
                    raise ValueError(line.strip())
                yield line
        frames = _parse_frames(checked_lines())
        if int(state.get("frame", "0")) != len(frames):
            raise ValueError("Decoded frame count differs from analysis records")
        features.seek(0)
        for frame in frames:
            frame["signature"] = features.read(1024)
            if len(frame["signature"]) != 1024:
                raise ValueError("Incomplete frame signature output")
        if features.read(1):
            raise ValueError("Frame signature count differs from timestamp records")
        return frames


def _distance(a, b):
    return sum(abs(x - y) for x, y in zip(a["signature"], b["signature"])) / 1024


def _detail(frame):
    pixels = frame["signature"]
    return (sum(abs(pixels[i] - pixels[i + 1]) for i in range(1023) if i % 32 != 31)
            + sum(abs(pixels[i] - pixels[i + 32]) for i in range(992))) / 1984


def _boundaries(frames, threshold):
    candidates = []
    suppressed = set()
    # A one-frame luma pulse returning to similar surrounding statistics is an
    # exposure/flash candidate, not a blanket ban on short product inserts.
    for i in range(1, len(frames) - 1):
        before, current, after = frames[i - 1:i + 2]
        close = max(abs(a - b) for a, b in zip(before["mean"], after["mean"])) <= 3
        pulse = abs(current["mean"][0] - before["mean"][0]) >= 20
        chroma = max(abs(current["mean"][j] - before["mean"][j]) for j in (1, 2)) <= 3
        spread = max(abs(a - b) for a, b in zip(before["stdev"], current["stdev"])) <= 5
        same_structure = True
        if "signature" in current:
            offsets = [b - a for a, b in zip(before["signature"], current["signature"])]
            shift = sum(offsets) / 1024
            same_structure = (_distance(before, after) <= 6 and
                              sum(abs(v - shift) for v in offsets) / 1024 <= 5)
        if close and pulse and chroma and spread and same_structure:
            suppressed.update((i, i + 1))
        # A blurred hand briefly covering the lens can produce a very strong
        # score. Require both a large loss of detail and a return to the old
        # image; ordinary sharp, one-frame inserts are retained.
        if "signature" in current:
            entry = _distance(before, current)
            if entry <= 10:
                continue
            detail = _detail(before)
            for stop in range(i + 1, min(i + 4, len(frames))):
                covered = frames[i:stop]
                if (detail > 3 and _distance(before, frames[stop]) < min(24, entry * 0.5)
                        and all(_detail(f) < detail * 0.8 for f in covered)):
                    suppressed.update(range(i, stop + 1))
                    break
    for i in range(1, len(frames)):
        frame, previous = frames[i], frames[i - 1]
        # scdet is luma-based; this extra signal covers strong isoluminant color cuts.
        color_delta = max(abs(frame["mean"][j] - previous["mean"][j]) for j in (1, 2))
        strength = max(frame["score"], color_delta / 2.56)
        if strength >= threshold and i not in suppressed:
            candidates.append({"frame": i, "strength": strength,
                               "reason": "scene_score" if frame["score"] >= threshold
                               else "chroma_change"})
    # When two adjacent signals describe old -> new -> nearly same new, keep
    # the dominant change. Old -> insert -> old remains two separate cuts.
    duplicates = set()
    for left, right in zip(candidates, candidates[1:]):
        i = left["frame"]
        if right["frame"] != i + 1 or "signature" not in frames[i]:
            continue
        before, middle, after = frames[i - 1:i + 2]
        ab, bc, ac = (_distance(before, middle), _distance(middle, after),
                      _distance(before, after))
        if bc < ab * 0.5 and ac >= ab * 0.65:
            duplicates.add(i + 1)
        elif ab < bc * 0.5 and ac >= bc * 0.65:
            duplicates.add(i)
    return [c for c in candidates if c["frame"] not in duplicates], sorted(suppressed)


def _shots(frames, boundaries):
    origin = frames[0]["pts"]
    last_duration = frames[-1]["duration"]
    if last_duration is None or last_duration <= 0:
        raise ValueError("Final frame duration unavailable; cannot determine video end reliably")
    end = frames[-1]["pts"] + last_duration - origin
    indexes = [0] + [b["frame"] for b in boundaries] + [len(frames)]
    times = [frames[i]["pts"] - origin for i in indexes[:-1]] + [end]
    shots = []
    for n, (start, stop) in enumerate(zip(times, times[1:]), 1):
        start_seconds, end_seconds = round(float(start), 9), round(float(stop), 9)
        if end_seconds <= start_seconds:
            raise ValueError("Shot times collapse at nanosecond output precision")
        shots.append({"id": n, "start_seconds": start_seconds,
                      "end_seconds": end_seconds,
                      "duration_seconds": round(end_seconds - start_seconds, 9),
                      "start_frame": indexes[n - 1], "end_frame_exclusive": indexes[n]})
    return shots, float(origin), round(float(end), 9)


def detect_videos(files, *, ffmpeg="ffmpeg", ffprobe="ffprobe", timeout=600,
                  threshold=DEFAULT_THRESHOLD):
    """Detect the first non-cover video stream; failures never emit partial shots."""
    files = list(files)
    if not files:
        raise DetectionError("Specify at least one local video file")
    if _positive_number(timeout) is None:
        raise DetectionError("Timeout must be a finite positive number of seconds")
    if _positive_number(threshold) is None or float(threshold) > 100:
        raise DetectionError("Threshold must be finite and in (0, 100]")
    timeout, threshold = float(timeout), float(threshold)
    versions = {}
    for name, executable in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        try:
            output, _ = _run([str(executable), "-version"], min(timeout, 10))
            version = output.splitlines()[0] if output.splitlines() else ""
            if not version.startswith(f"{name} version "):
                raise ValueError("Unexpected tool version response")
            versions[name] = version
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise DetectionError(f"{name} unavailable: {exc}. Use --{name} to specify its path.") from exc
    try:
        output, _ = _run([str(ffmpeg), "-hide_banner", "-filters"], min(timeout, 10))
        for name in ("split", "scale", "format", "scdet", "metadata", "showinfo"):
            if not re.search(r"^\s*\S+\s+" + name + r"\s", output, re.MULTILINE):
                raise ValueError(f"Required FFmpeg filter unavailable: {name}")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise DetectionError(str(exc)) from exc
    results = []
    for filename in files:
        item = {"file": str(filename), "status": "failed", "metadata": None,
                "analysis": None, "shots": [], "warnings": [], "error": None}
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
                "stream_side_data=rotation:format=format_name,duration", "-of", "json", str(path),
            ], timeout)
            if detail:
                raise ValueError(detail)
            item["metadata"] = _metadata(json.loads(output))
            stage = "analysis"
            frames = _analyse(path, ffmpeg, timeout)
            stage = "boundaries"
            boundaries, suppressed = _boundaries(frames, threshold)
            shots, origin, end = _shots(frames, boundaries)
            item["analysis"] = {"frames": len(frames), "origin_seconds": origin,
                                "duration_seconds": end, "shot_count": len(shots),
                                "boundaries": [{"frame": b["frame"],
                                                "time_seconds": shots[n + 1]["start_seconds"],
                                                "strength": round(b["strength"], 6),
                                                "reason": b["reason"]}
                                               for n, b in enumerate(boundaries)],
                                "transient_suppressed_frames": suppressed}
            item["shots"] = shots
            item["warnings"] = ["Gradual transitions are not reliably detected; manual review required."]
            if suppressed:
                item["warnings"].append("Possible short flashes/blurred occlusions suppressed; review these frames.")
            item["status"] = "passed"
        except subprocess.TimeoutExpired:
            item["error"] = f"{stage}: timed out after {timeout:g} seconds"
        except (OSError, ValueError, RuntimeError) as exc:
            item["error"] = f"{stage}: {exc}"
        results.append(item)
    passed = sum(item["status"] == "passed" for item in results)
    return {"schema_version": 1, "tools": versions,
            "config": {"algorithm": ALGORITHM, "threshold": threshold,
                       "analysis_max_width": 320, "signature_size": [32, 32],
                       "time_origin": "first_video_frame",
                       "interval": "[start,end)", "frame_indexes": "zero_based"},
            "summary": {"total": len(results), "passed": passed, "failed": len(results) - passed},
            "results": results}
