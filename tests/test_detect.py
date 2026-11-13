import contextlib
from fractions import Fraction
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from beauty_cut.__main__ import main
from beauty_cut.detect import DetectionError, _analyse, _boundaries, _parse_frames, _shots, detect_videos


def frame(t, score=0, mean=(100, 120, 130), duration=Fraction(1, 10)):
    return {"pts": Fraction(str(t)), "duration": duration, "score": score,
            "mafd": score, "mean": list(mean), "stdev": [20, 10, 10]}


def reply(command, timeout):
    if command[-1] == "-version":
        return f"{Path(command[0]).stem} version synthetic\n", ""
    if command[-1] == "-filters":
        return "\n".join(f" ... {name} V->V" for name in
                         ("split", "scale", "format", "scdet", "metadata", "showinfo")), ""
    return json.dumps({"streams": [{"codec_name": "h264", "width": 160, "height": 120}],
                       "format": {}}), ""


class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.video = Path(self.temp.name) / "中文 video.mp4"
        self.video.write_bytes(b"mocked subprocess input")
        self.runner = patch("beauty_cut.detect._run", side_effect=reply).start()
        self.analysis = patch("beauty_cut.detect._analyse", return_value=[
            frame(5), frame(5.1), frame(5.2, 20), frame(5.3)]).start()
        self.addCleanup(patch.stopall)

    def test_report_and_relative_half_open_intervals(self):
        report = detect_videos([self.video])
        item = report["results"][0]
        self.assertEqual(report["summary"], {"total": 1, "passed": 1, "failed": 0})
        self.assertEqual(item["analysis"]["origin_seconds"], 5)
        self.assertEqual(item["analysis"]["duration_seconds"], 0.4)
        self.assertEqual([(s["start_seconds"], s["end_seconds"]) for s in item["shots"]],
                         [(0, 0.2), (0.2, 0.4)])
        self.assertEqual(item["shots"][1]["start_frame"], 2)
        self.assertEqual(item["shots"][-1]["end_frame_exclusive"], 4)

    def test_missing_and_bad_file_continue(self):
        self.analysis.side_effect = [ValueError("damaged"), [frame(0)]]
        report = detect_videos([self.video.parent / "absent", self.video.parent,
                                self.video, self.video])
        self.assertEqual(report["summary"], {"total": 4, "passed": 1, "failed": 3})
        for item in report["results"][:-1]:
            self.assertEqual(item["shots"], [])
            self.assertIsNone(item["analysis"])

    def test_invalid_config(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(DetectionError):
                    detect_videos([self.video], timeout=value)
                with self.assertRaises(DetectionError):
                    detect_videos([self.video], threshold=value)
        with self.assertRaises(DetectionError):
            detect_videos([self.video], threshold=101)
        with self.assertRaises(DetectionError):
            detect_videos([])

    def test_unavailable_or_wrong_tool(self):
        for error in (OSError("missing"), ValueError("bad"),
                      subprocess.TimeoutExpired("ffmpeg", 1)):
            self.runner.side_effect = error
            with self.assertRaises(DetectionError):
                detect_videos([self.video])
        self.runner.side_effect = lambda c, t: ("wrong version", "")
        with self.assertRaises(DetectionError):
            detect_videos([self.video])

    def test_missing_filter_is_configuration_error(self):
        self.runner.side_effect = lambda c, t: ((" ... scale V->V", "")
                                               if c[-1] == "-filters" else reply(c, t))
        with self.assertRaisesRegex(DetectionError, "Required FFmpeg filter"):
            detect_videos([self.video])

    def test_timeout_keeps_metadata_without_partial_result(self):
        self.analysis.side_effect = subprocess.TimeoutExpired("ffmpeg", 0.1)
        item = detect_videos([self.video], timeout=0.1)["results"][0]
        self.assertIn("analysis: timed out", item["error"])
        self.assertIsNotNone(item["metadata"])
        self.assertEqual(item["shots"], [])

    def test_bad_probe_json_is_per_file_error(self):
        self.runner.side_effect = lambda c, t: (("[]", "") if "-of" in c else reply(c, t))
        item = detect_videos([self.video])["results"][0]
        self.assertIn("probe:", item["error"])
        self.analysis.assert_not_called()

    def test_no_cuts_single_frame_and_final_duration_required(self):
        for frames in ([frame(0)], [frame(0), frame(0.1)]):
            shots, origin, end = _shots(frames, [])
            self.assertEqual(len(shots), 1)
            self.assertGreater(end, 0)
        for duration in (None, Fraction(0), Fraction(-1)):
            with self.assertRaisesRegex(ValueError, "Final frame duration"):
                _shots([frame(0, duration=duration)], [])

    def test_short_insert_is_not_merged(self):
        frames = [frame(0), frame(0.1, 40, mean=(110, 180, 80)), frame(0.2, 30), frame(0.3)]
        cuts, suppressed = _boundaries(frames, 5)
        self.assertEqual([c["frame"] for c in cuts], [1, 2])
        self.assertEqual(suppressed, [])

    def test_isoluminant_chroma_cut(self):
        cuts, _ = _boundaries([frame(0), frame(0.1, mean=(100, 180, 60))], 5)
        self.assertEqual(cuts[0]["reason"], "chroma_change")

    def test_luma_pulse_suppressed_and_reported(self):
        frames = [frame(0), frame(0.1, 20, mean=(160, 120, 130)), frame(0.2, 20), frame(0.3)]
        cuts, suppressed = _boundaries(frames, 5)
        self.assertEqual(cuts, [])
        self.assertEqual(suppressed, [1, 2])

    def test_adjacent_duplicate_removed_but_single_frame_insert_kept(self):
        frames = [frame(0), frame(0.1, 20), frame(0.2, 7), frame(0.3)]
        for f, value in zip(frames, (20, 100, 102, 102)):
            f["signature"] = bytes([value]) * 1024
        self.assertEqual([b["frame"] for b in _boundaries(frames, 5)[0]], [1])
        frames[2]["signature"] = bytes([20]) * 1024
        self.assertEqual([b["frame"] for b in _boundaries(frames, 5)[0]], [1, 2])
        for f, value in zip(frames, (20, 22, 100, 100)):
            f["signature"] = bytes([value]) * 1024
        self.assertEqual([b["frame"] for b in _boundaries(frames, 5)[0]], [2])

    def test_luma_insert_with_different_structure_is_preserved(self):
        frames = [frame(0), frame(0.1, 20, mean=(160, 120, 130)), frame(0.2, 20), frame(0.3)]
        for f in frames:
            f["signature"] = bytes([100, 140]) * 512
        frames[1]["signature"] = bytes([200, 160]) * 512
        self.assertEqual([b["frame"] for b in _boundaries(frames, 5)[0]], [1, 2])

    def test_cli_exit_codes_and_json(self):
        for files, expected in (([self.video], 0), ([self.video.parent / "missing", self.video], 1)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["detect", *map(str, files)])
            self.assertEqual(code, expected)
            self.assertEqual(json.loads(out.getvalue())["summary"]["passed"], 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["detect", str(self.video), "--threshold", "nan"])
        self.assertEqual(code, 2)
        self.assertIn("error", json.loads(out.getvalue()))


class FrameLogTests(unittest.TestCase):
    def log(self, index=0, pts=1001, duration=1001):
        return ["[Parsed_showinfo_3] config in time_base: 1/30000, frame_rate: 30/1",
                "[Parsed_metadata_2] lavfi.scd.mafd=20.100",
                "[Parsed_metadata_2] lavfi.scd.score=20.100",
                f"[Parsed_showinfo_3] n: {index} pts: {pts} pts_time:0.0333667 "
                f"duration: {duration} duration_time:0.0333667 mean:[100 120 130] stdev:[20 10 10]"]

    def test_exact_integer_timestamp_and_duration(self):
        frames = _parse_frames(iter(self.log()))
        self.assertEqual(frames[0]["pts"], Fraction(1001, 30000))
        self.assertEqual(frames[0]["duration"], Fraction(1001, 30000))

    def test_bad_or_missing_records_fail(self):
        logs = [[], self.log(index=1), self.log()[1:], self.log()[:1] + self.log()[3:],
                self.log() + self.log(index=1),
                self.log() + self.log(index=1, pts=1000),
                self.log() + ["[Parsed_showinfo_3] config in time_base: 1/25, frame_rate: 25/1"]]
        for log in logs:
            with self.subTest(log=log), self.assertRaises(ValueError):
                _parse_frames(log)


class AnalysisProcessTests(unittest.TestCase):
    def run_analysis(self, *, progress="frame=1\nprogress=end\n", features=1024,
                     extra_log="", returncode=0):
        def run(command, **kwargs):
            Path(command[command.index("-progress") + 1]).write_text(progress)
            kwargs["stdout"].write(bytes(features))
            lines = FrameLogTests().log() + [extra_log]
            kwargs["stderr"].write("\n".join(lines).encode())
            return subprocess.CompletedProcess(command, returncode)
        with patch("beauty_cut.detect.subprocess.run", side_effect=run):
            return _analyse(Path("synthetic.mp4"), "ffmpeg", 5)

    def test_complete_process_output(self):
        self.assertEqual(len(self.run_analysis()[0]["signature"]), 1024)

    def test_incomplete_or_misaligned_output_is_failure(self):
        for args in ({"progress": "frame=1\nprogress=continue\n"},
                     {"progress": "frame=2\nprogress=end\n"},
                     {"features": 1000}, {"features": 1025}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.run_analysis(**args)

    def test_process_error_never_yields_partial_frames(self):
        for args in ({"returncode": 1}, {"extra_log": "[decoder] [error] corrupt frame"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.run_analysis(**args)


FFMPEG = os.environ.get("BEAUTYCUT_FFMPEG") or shutil.which("ffmpeg")
FFPROBE = os.environ.get("BEAUTYCUT_FFPROBE") or shutil.which("ffprobe")


@unittest.skipUnless(FFMPEG and FFPROBE, "Install tools or set BEAUTYCUT_FFMPEG/BEAUTYCUT_FFPROBE")
class DetectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / "中文 video.mkv"

    def generate(self, source, extra=()):
        result = subprocess.run([FFMPEG, "-v", "error", "-nostdin", "-f", "lavfi", "-i", source,
                                 *extra, "-fps_mode", "passthrough", "-c:v", "ffv1", str(self.video)],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))

    def detect(self, *files):
        return detect_videos(files or [self.video], ffmpeg=FFMPEG, ffprobe=FFPROBE, timeout=30)

    def item(self):
        item = self.detect()["results"][0]
        self.assertEqual(item["status"], "passed", item["error"])
        return item

    def test_three_known_hard_cuts_and_short_insert(self):
        self.generate("color=red:s=160x120:r=10:d=0.5[a];color=blue:s=160x120:r=10:d=0.1[b];"
                      "color=red:s=160x120:r=10:d=0.4[c];[a][b][c]concat=n=3:v=1:a=0,setpts=N/(10*TB)")
        item = self.item()
        self.assertEqual(item["analysis"]["frames"], 10)
        self.assertEqual([b["frame"] for b in item["analysis"]["boundaries"]], [5, 6])
        self.assertEqual([s["start_seconds"] for s in item["shots"]], [0, 0.5, 0.6])
        self.assertEqual(item["shots"][-1]["end_seconds"], 1)

    def test_continuous_motion_and_small_subtitle_are_not_cuts(self):
        self.generate("testsrc2=s=160x120:r=30:d=2,drawbox=x=10:y=100:w=60:h=8:"
                      "color=white:t=fill:enable='between(t,0.5,1)'")
        self.assertEqual(self.item()["analysis"]["shot_count"], 1)

    def test_smooth_brightness_ramp_is_not_cut(self):
        self.generate("color=gray:s=160x120:r=30:d=2,geq=lum='80+N':cb=128:cr=128")
        self.assertEqual(self.item()["analysis"]["shot_count"], 1)

    def test_single_frame_video(self):
        self.generate("color=red:s=64x48:r=10:d=0.1")
        item = self.item()
        self.assertEqual(item["analysis"]["frames"], 1)
        self.assertEqual(item["shots"][0]["end_seconds"], 0.1)

    def test_isoluminant_color_change(self):
        self.generate("color=s=64x48:r=10:d=1,geq=lum=100:cb='if(lt(N,5),70,180)':cr=128")
        item = self.item()
        self.assertEqual([b["frame"] for b in item["analysis"]["boundaries"]], [5])
        self.assertEqual(item["analysis"]["boundaries"][0]["reason"], "chroma_change")

    def test_audio_only_continues(self):
        audio = self.root / "audio.m4a"
        result = subprocess.run([FFMPEG, "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                                 "anullsrc=r=8000:cl=mono", "-t", "0.2", "-c:a", "aac", str(audio)],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.generate("color=red:s=64x48:r=5:d=1")
        report = self.detect(audio, self.video)
        self.assertEqual(report["summary"]["passed"], 1)
        self.assertIn("No video stream", report["results"][0]["error"])

    def test_single_frame_flash_is_suppressed(self):
        self.generate("color=gray:s=160x120:r=10:d=1,drawbox=color=white:t=fill:enable='eq(n,5)'")
        item = self.item()
        self.assertEqual(item["analysis"]["shot_count"], 1)
        self.assertEqual(item["analysis"]["transient_suppressed_frames"], [5, 6])

    def test_short_blurred_occlusion_returns_to_original_shot(self):
        self.generate("testsrc2=s=160x120:r=10:d=1,drawbox=color=tan:t=fill:enable='eq(n,5)'")
        item = self.item()
        self.assertEqual(item["analysis"]["shot_count"], 1)
        self.assertIn(5, item["analysis"]["transient_suppressed_frames"])

    def test_variable_timing_cut_uses_actual_pts(self):
        self.generate("color=red:s=64x48:r=10:d=0.5[a];color=blue:s=64x48:r=10:d=0.5[b];"
                      "[a][b]concat=n=2:v=1:a=0,settb=1/1000,setpts=N*N*100")
        result = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "V:0",
                                 "-show_entries", "frame=best_effort_timestamp_time,duration_time",
                                 "-of", "json", str(self.video)], capture_output=True, check=True, timeout=10)
        frames = json.loads(result.stdout)["frames"]
        times = [float(f["best_effort_timestamp_time"]) for f in frames]
        self.assertGreater(len({round(b-a, 3) for a,b in zip(times,times[1:])}), 1)
        item = self.item()
        self.assertEqual(item["analysis"]["frames"], len(times))
        self.assertEqual(item["shots"][1]["start_seconds"], times[5])
        self.assertAlmostEqual(item["shots"][-1]["end_seconds"],
                               times[-1] + float(frames[-1]["duration_time"]))

    def test_nonzero_origin(self):
        self.generate("color=red:s=64x48:r=10:d=0.5[a];color=blue:s=64x48:r=10:d=0.5[b];"
                      "[a][b]concat=n=2:v=1:a=0,setpts=N/(10*TB)",
                      extra=("-output_ts_offset", "5"))
        item = self.item()
        self.assertEqual(item["analysis"]["origin_seconds"], 5)
        self.assertEqual(item["shots"][1]["start_seconds"], 0.5)
        self.assertEqual(item["analysis"]["duration_seconds"], 1)

    def test_bad_inputs_continue_and_no_partial_shots(self):
        self.generate("testsrc2=s=64x48:r=10:d=1")
        broken = self.root / "bad.mp4"
        broken.write_bytes(b"not video")
        report = self.detect(broken, self.root / "missing", self.video)
        self.assertEqual(report["summary"], {"total": 3, "passed": 1, "failed": 2})
        self.assertTrue(all(not i["shots"] for i in report["results"][:-1]))

    def test_damaged_later_frames_keep_metadata_and_continue(self):
        mp4 = self.root / "damaged.mp4"
        result = subprocess.run([FFMPEG, "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                                 "testsrc2=s=160x120:r=10:d=2", "-c:v", "mpeg4", str(mp4)],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = bytearray(mp4.read_bytes())
        start = data.index(b"mdat")
        end = start - 4 + int.from_bytes(data[start - 4:start], "big")
        data[start + (end - start) * 2 // 3:end] = b"\xff" * (end - start - (end - start) * 2 // 3)
        mp4.write_bytes(data)
        self.generate("color=red:s=64x48:r=5:d=1")
        report = self.detect(mp4, self.video)
        self.assertEqual(report["summary"], {"total": 2, "passed": 1, "failed": 1})
        self.assertIsNotNone(report["results"][0]["metadata"])
        self.assertIn("analysis:", report["results"][0]["error"])
        self.assertEqual(report["results"][0]["shots"], [])

    def test_first_stream_only(self):
        result = subprocess.run([FFMPEG, "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                                 "color=red:s=64x48:r=5:d=1", "-f", "lavfi", "-i",
                                 "testsrc2=s=128x96:r=10:d=1", "-map", "0:v", "-map", "1:v",
                                 "-c:v", "ffv1", str(self.video)], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        item = self.item()
        self.assertEqual(item["analysis"]["frames"], 5)
        self.assertEqual(item["metadata"]["width"], 64)

    def test_real_cli_batch_and_bad_configuration(self):
        self.generate("color=red:s=64x48:r=5:d=1")
        args = [sys.executable, "-m", "beauty_cut", "detect", str(self.root / "missing"),
                str(self.video), "--ffmpeg", FFMPEG, "--ffprobe", FFPROBE]
        result = subprocess.run(args, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(json.loads(result.stdout)["summary"]["passed"], 1)
        result = subprocess.run(args + ["--threshold", "0"], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
