"""Local shot images and offline review reports; no third-party Python packages."""

from copy import deepcopy
from datetime import datetime
from fractions import Fraction
import json
from pathlib import Path
import re
import subprocess
import tempfile

from beauty_cut.detect import DEFAULT_THRESHOLD, DetectionError, detect_videos
from beauty_cut.probe import _run
from beauty_cut.report import write_index, write_video


class PreviewError(ValueError):
    """Batch configuration or output publication failed."""


def _image_plan(item, frames):
    """Choose the frame covering the exact temporal midpoint, never outside a shot."""
    origin = frames[0]["pts"]
    selected = set()
    previews, boundaries = [], []

    def reference(index):
        selected.add(index)
        return {"frame": index, "time_seconds": round(float(frames[index]["pts"] - origin), 9)}

    for shot in item["shots"]:
        start, end = shot["start_frame"], shot["end_frame_exclusive"]
        stop = frames[end]["pts"] if end < len(frames) else frames[-1]["pts"] + frames[-1]["duration"]
        middle = (frames[start]["pts"] + stop) / 2
        lo, hi = start, end
        while lo + 1 < hi:
            candidate = (lo + hi) // 2
            if frames[candidate]["pts"] <= middle:
                lo = candidate
            else:
                hi = candidate
        previews.append(reference(lo))
    for boundary in item["analysis"]["boundaries"]:
        cut = boundary["frame"]
        boundaries.append({"frame": cut, "time_seconds": boundary["time_seconds"],
                           "images": [reference(i) for i in range(max(0, cut - 2), min(len(frames), cut + 2))]})
    ordered = sorted(selected)
    paths = {index: f"images/frame-{number:06d}.jpg" for number, index in enumerate(ordered, 1)}
    for ref in previews + [ref for boundary in boundaries for ref in boundary["images"]]:
        ref["image"] = paths[ref["frame"]]
    return previews, boundaries, ordered


def _selection_expression(indexes):
    # FFmpeg's expression parser has a recursion limit. A flat sum of hundreds
    # of eq() terms exceeds it; balanced if branches also avoid O(N) per frame.
    if len(indexes) == 1:
        return f"eq(n,{indexes[0]})"
    middle = len(indexes) // 2
    return (f"if(lt(n,{indexes[middle]}),{_selection_expression(indexes[:middle])},"
            f"{_selection_expression(indexes[middle:])})")


def _extract(path, folder, indexes, frames, ffmpeg, timeout):
    images = folder / "images"
    images.mkdir()
    # A script avoids OS command-line length limits for many requested frames.
    expression = _selection_expression(indexes)
    scale = ("scale=w='max(2,trunc(iw*sar*min(1,min(640/(iw*sar),640/ih))/2)*2)':"
             "h='max(2,trunc(ih*min(1,min(640/(iw*sar),640/ih))/2)*2)',setsar=1")
    script = folder / "extract.filter"
    script.write_text(f"select='{expression}',showinfo,{scale}", encoding="utf-8")
    with tempfile.TemporaryFile() as log:
        process = subprocess.run([
            str(ffmpeg), "-hide_banner", "-loglevel", "info", "-nostdin", "-nostats",
            "-xerror", "-max_error_rate", "0", "-protocol_whitelist", "file",
            "-hwaccel", "none", "-threads", "2", "-err_detect", "explode", "-copyts",
            "-i", str(path), "-map", "0:V:0", "-an", "-sn", "-dn",
            "-filter_threads", "1", "-/filter:v", str(script),
            "-fps_mode", "passthrough", "-c:v", "mjpeg", "-q:v", "3", "-threads", "1",
            "-start_number", "1", str(images / "frame-%06d.jpg"),
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
            timeout=timeout, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log.seek(0)
        time_base = None
        timestamps = []
        tail = ""
        for raw in log:
            line = raw.decode("utf-8", "replace")
            tail = (tail + line)[-4096:]
            if "Parsed_showinfo_" in line:
                match = re.search(r"config in time_base:\s*(\d+/\d+)", line)
                if match:
                    time_base = Fraction(match[1])
                match = re.search(r"\bn:\s*(\d+)\s+pts:\s*(-?\d+)", line)
                if match:
                    if time_base is None or int(match[1]) != len(timestamps):
                        raise ValueError("Incomplete extraction frame records")
                    timestamps.append(int(match[2]) * time_base)
        if process.returncode:
            raise ValueError(f"FFmpeg image extraction failed ({process.returncode}): {tail}")
    script.unlink()
    if timestamps != [frames[i]["pts"] for i in indexes]:
        raise ValueError("Extracted image timestamps differ from detection records")
    actual = sorted(images.glob("*.jpg"))
    if len(actual) != len(indexes) or any(p.stat().st_size == 0 for p in actual):
        raise ValueError("Image output count or size is invalid")


def _playback(path, item, ffprobe, timeout):
    """Map first-video-frame-relative time to container time; expose uncertainty."""
    output, detail = _run([
        str(ffprobe), "-v", "error", "-protocol_whitelist", "file", "-select_streams", "V",
        "-show_entries", "format=start_time:stream=index", "-of", "json", str(path),
    ], timeout)
    if detail:
        raise ValueError(detail)
    data = json.loads(output)
    result = {"offset_seconds": None, "basis": "unavailable", "stream_index": None}
    streams = data.get("streams", [])
    if streams:
        result["stream_index"] = streams[0].get("index")
    # Browsers need not choose the same video track as our detector.
    result["multiple_video_streams"] = len(streams) > 1
    try:
        start = Fraction(str(data["format"]["start_time"]))
        offset = Fraction(str(item["analysis"]["origin_seconds"])) - start
        if offset >= 0:
            result.update(offset_seconds=float(offset), basis="video_origin_minus_container_start")
    except (KeyError, ValueError, TypeError, ZeroDivisionError):
        pass
    result["note"] = "Browser seeking is approximate; use boundary images for frame checks."
    return result


def preview_videos(files, *, output_dir, ffmpeg="ffmpeg", ffprobe="ffprobe", timeout=600,
                   threshold=DEFAULT_THRESHOLD):
    files = list(files)
    if not files:
        raise PreviewError("Specify at least one local video file")
    try:
        parent = Path(output_dir).resolve()
        parent.mkdir(parents=True, exist_ok=True)
        run = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("run-%Y%m%d-%H%M%S-"), dir=parent))
        (run / "INCOMPLETE.txt").write_text("Report generation has not completed.\n", encoding="utf-8")
    except OSError as exc:
        raise PreviewError(f"Cannot create output directory: {exc}") from exc
    results = []

    def consume(detected, frames):
        item = deepcopy(detected)
        item["detection_status"] = detected["status"]
        item["preview_status"] = "not_run"
        item["report"] = None
        item["boundary_previews"] = []
        results.append(item)
        if frames is None:
            return
        stage = "preview"
        try:
            path = Path(item["file"]).resolve()
            before = path.stat()
            item["source"] = {"name": path.name, "size_bytes": before.st_size}
            previews, boundaries, indexes = _image_plan(item, frames)
            name = f"video-{len(results):03d}"
            with tempfile.TemporaryDirectory(prefix=".working-", dir=run) as temporary:
                folder = Path(temporary) / name
                folder.mkdir()
                _extract(path, folder, indexes, frames, ffmpeg, float(timeout))
                stage = "playback_metadata"
                item["playback"] = _playback(path, item, ffprobe, float(timeout))
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("Source changed during image extraction")
                for shot, preview in zip(item["shots"], previews):
                    shot["preview"] = preview
                item["boundary_previews"] = boundaries
                item["preview_status"] = "passed"
                item["report"] = f"{name}/index.html"
                stage = "report"
                write_video(folder / "index.html", item)
                folder.rename(run / name)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            item["status"] = "failed"
            item["preview_status"] = "failed"
            item["report"] = None
            item["boundary_previews"] = []
            for shot in item["shots"]:
                shot.pop("preview", None)
            item["error"] = f"{stage}: {exc}"

    try:
        detection = detect_videos(files, ffmpeg=ffmpeg, ffprobe=ffprobe, timeout=timeout,
                                  threshold=threshold, _on_result=consume)
        passed = sum(item["status"] == "passed" for item in results)
        report = {"schema_version": 1, "tools": detection["tools"],
                  "config": {**detection["config"], "preview_selection": "temporal_midpoint",
                             "boundary_frames_each_side": 2, "image_max_side": 640},
                  "summary": {"total": len(results), "passed": passed, "failed": len(results) - passed},
                  "results": results, "report_file": str(run / "index.html")}
        pending = run / "results.json.tmp"
        pending.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
        pending.replace(run / "results.json")
        write_index(run / "index.html", report)
        (run / "INCOMPLETE.txt").unlink()
        return report
    except DetectionError:
        raise
    except OSError as exc:
        raise PreviewError(f"Cannot publish report (incomplete output at {run}): {exc}") from exc
