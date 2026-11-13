import contextlib
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
from beauty_cut.probe import ProbeError, _run, probe_videos


METADATA = {
    "streams": [{"codec_name": "mpeg4", "width": 160, "height": 120,
                 "avg_frame_rate": "30000/1001", "r_frame_rate": "30/1",
                 "time_base": "1/30000", "duration": "1.001"}],
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "1.1"},
}


def tool_reply(command, timeout):
    if command[-1] == "-version":
        return f"{Path(command[0]).stem} version synthetic-test\n", ""
    if "-of" in command:
        return json.dumps(METADATA), ""
    return "frame=1\nprogress=continue\nframe=30\nprogress=end\n", ""


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / "中文 素材.mp4"
        self.file.write_bytes(b"synthetic stub; subprocess boundary is mocked")
        self.runner = patch("beauty_cut.probe._run", side_effect=tool_reply).start()
        self.addCleanup(patch.stopall)

    def test_success_schema_and_metadata(self):
        report = probe_videos([self.file])
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["summary"], {"total": 1, "passed": 1, "failed": 0})
        item = report["results"][0]
        self.assertEqual(item["file"], str(self.file))
        self.assertEqual(item["metadata"]["duration_seconds"], 1.001)
        self.assertAlmostEqual(item["metadata"]["average_fps"], 30000 / 1001)
        self.assertEqual(item["decode"], {"status": "passed", "frames": 30})
        self.assertIsNone(item["error"])
        self.assertIn("ffprobe", report["tools"])

    def test_missing_and_directory_continue_to_valid_file(self):
        report = probe_videos([self.file.parent / "missing", self.file.parent, self.file])
        self.assertEqual(report["summary"], {"total": 3, "passed": 1, "failed": 2})
        self.assertEqual(report["results"][0]["decode"]["status"], "not_run")
        self.assertEqual(report["results"][-1]["status"], "passed")

    def test_missing_optional_values_are_null_and_rotation_is_preserved(self):
        document = {"streams": [{"codec_name": "h264", "width": 120, "height": 160,
                                 "avg_frame_rate": "0/0", "r_frame_rate": "N/A",
                                 "duration": "NaN", "side_data_list": [{"rotation": -90}]}],
                    "format": {"duration": "Infinity"}}
        self.runner.side_effect = lambda cmd, timeout: (
            (json.dumps(document), "") if "-of" in cmd else tool_reply(cmd, timeout))
        metadata = probe_videos([self.file])["results"][0]["metadata"]
        for key in ("duration_seconds", "average_fps", "nominal_fps", "time_base"):
            self.assertIsNone(metadata[key])
        self.assertEqual(metadata["rotation_degrees"], -90)

    def test_container_duration_fallback(self):
        document = json.loads(json.dumps(METADATA))
        document["streams"][0]["duration"] = "N/A"
        self.runner.side_effect = lambda cmd, timeout: (
            (json.dumps(document), "") if "-of" in cmd else tool_reply(cmd, timeout))
        self.assertEqual(probe_videos([self.file])["results"][0]["metadata"]["duration_seconds"], 1.1)

    def test_invalid_metadata_is_failure(self):
        cases = ["not JSON", "[]", '{"streams": []}',
                 '{"streams": [{"width": 0}]}',
                 '{"streams": [{"width": 10, "height": 10, "codec_name": "unknown"}]}']
        for output in cases:
            with self.subTest(output=output):
                self.runner.side_effect = lambda cmd, timeout: (
                    (output, "") if "-of" in cmd else tool_reply(cmd, timeout))
                item = probe_videos([self.file])["results"][0]
                self.assertEqual(item["status"], "failed")
                self.assertEqual(item["decode"]["status"], "not_run")
                self.assertIn("probe:", item["error"])

    def test_decode_failure_keeps_metadata_and_continues(self):
        calls = 0

        def reply(cmd, timeout):
            nonlocal calls
            if "-i" in cmd:
                calls += 1
                if calls == 1:
                    raise ValueError("damaged frame near end")
            return tool_reply(cmd, timeout)

        self.runner.side_effect = reply
        report = probe_videos([self.file, self.file])
        self.assertEqual(report["summary"]["failed"], 1)
        self.assertIsNotNone(report["results"][0]["metadata"])
        self.assertIn("decode:", report["results"][0]["error"])
        self.assertEqual(report["results"][1]["status"], "passed")

    def test_timeout_at_each_file_stage_continues(self):
        for marker in ("-of", "-i"):
            with self.subTest(stage=marker):
                failed = False

                def reply(cmd, timeout):
                    nonlocal failed
                    if marker in cmd and not failed:
                        failed = True
                        raise subprocess.TimeoutExpired(cmd, timeout)
                    return tool_reply(cmd, timeout)

                self.runner.side_effect = reply
                report = probe_videos([self.file, self.file], timeout=5)
                self.assertIn("timed out after 5 seconds", report["results"][0]["error"])
                self.assertEqual(report["results"][1]["status"], "passed")

    def test_empty_or_incomplete_decode_is_failure(self):
        for output in ("", "frame=0\nprogress=end", "frame=5\nprogress=continue"):
            with self.subTest(output=output):
                self.runner.side_effect = lambda cmd, timeout: (
                    (output, "") if "-i" in cmd else tool_reply(cmd, timeout))
                self.assertEqual(probe_videos([self.file])["summary"]["failed"], 1)

    def test_error_diagnostics_cannot_silently_pass(self):
        for marker in ("-of", "-i"):
            with self.subTest(stage=marker):
                self.runner.side_effect = lambda cmd, timeout: (
                    (tool_reply(cmd, timeout)[0], "decode error")
                    if marker in cmd else tool_reply(cmd, timeout))
                self.assertEqual(probe_videos([self.file])["summary"]["failed"], 1)

    def test_invalid_configuration_and_missing_tools(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=value), self.assertRaises(ProbeError):
                probe_videos([self.file], timeout=value)
        with self.assertRaises(ProbeError):
            probe_videos([])
        for failure in (FileNotFoundError("not installed"), PermissionError("blocked"),
                        subprocess.TimeoutExpired("ffmpeg", 10), ValueError("bad version")):
            self.runner.side_effect = failure
            with self.subTest(failure=failure), self.assertRaises(ProbeError):
                probe_videos([self.file])

    def test_unexpected_tool_identity_is_configuration_error(self):
        self.runner.return_value = ("unrelated program", "")
        self.runner.side_effect = None
        with self.assertRaises(ProbeError):
            probe_videos([self.file])

    def test_explicit_tool_paths_and_cpu_local_decode_command(self):
        def reply(cmd, timeout):
            if cmd[-1] == "-version":
                name = "ffprobe" if "probe" in cmd[0] else "ffmpeg"
                return f"{name} version synthetic-test", ""
            return tool_reply(cmd, timeout)

        self.runner.side_effect = reply
        probe_videos([self.file], ffmpeg="tools dir/custom-ffmpeg", ffprobe="tools dir/custom-ffprobe")
        commands = [call.args[0] for call in self.runner.call_args_list]
        decode = commands[-1]
        self.assertEqual(decode[0], "tools dir/custom-ffmpeg")
        self.assertEqual(decode[decode.index("-hwaccel") + 1], "none")
        self.assertEqual(decode[decode.index("-protocol_whitelist") + 1], "file")
        self.assertEqual(decode[decode.index("-i") + 1], str(self.file.resolve()))
        self.assertIn("-xerror", decode)
        self.assertNotIn("-t", decode)
        self.assertNotIn("-ss", decode)

    def test_cli_json_and_exit_codes(self):
        for files, expected in (([self.file], 0), ([self.file.parent / "missing"], 1)):
            with self.subTest(code=expected):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = main(["probe", *map(str, files)])
                self.assertEqual(code, expected)
                self.assertEqual(json.loads(output.getvalue())["schema_version"], 1)
        self.runner.side_effect = FileNotFoundError("missing tool")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["probe", str(self.file)])
        self.assertEqual(code, 2)
        self.assertIn("error", json.loads(output.getvalue()))


class ProcessRunnerTests(unittest.TestCase):
    def test_runner_reports_nonzero_exit_and_utf8_diagnostics(self):
        with self.assertRaisesRegex(ValueError, "code 7: broken"):
            _run([sys.executable, "-c", "import sys; sys.stderr.write('broken'); sys.exit(7)"], 5)

    def test_runner_timeout(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            _run([sys.executable, "-c", "import time; time.sleep(10)"], 0.1)

    def test_cli_missing_tool_returns_json_and_exit_two(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = str(Path(folder) / "missing-ffmpeg")
            result = subprocess.run(
                [sys.executable, "-m", "beauty_cut", "probe", "unused.mp4", "--ffmpeg", missing],
                capture_output=True, timeout=10,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("ffmpeg unavailable", json.loads(result.stdout)["error"])


FFMPEG = os.environ.get("BEAUTYCUT_FFMPEG") or shutil.which("ffmpeg")
FFPROBE = os.environ.get("BEAUTYCUT_FFPROBE") or shutil.which("ffprobe")


@unittest.skipUnless(FFMPEG and FFPROBE, "Install tools or set BEAUTYCUT_FFMPEG/BEAUTYCUT_FFPROBE")
class ProbeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / "中文 video.mp4"
        self.generate("-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10:duration=2",
                      "-c:v", "mpeg4", "-q:v", "2", str(self.video))

    def generate(self, *args):
        process = subprocess.run([FFMPEG, "-v", "error", "-nostdin", *args],
                                 capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", "replace"))

    def probe(self, *files):
        return probe_videos(files, ffmpeg=FFMPEG, ffprobe=FFPROBE, timeout=30)

    def test_actual_video_dimensions_and_all_frames(self):
        item = self.probe(self.video)["results"][0]
        self.assertEqual(item["status"], "passed", item["error"])
        self.assertEqual((item["metadata"]["width"], item["metadata"]["height"]), (160, 120))
        self.assertEqual(item["metadata"]["average_fps"], 10)
        self.assertAlmostEqual(item["metadata"]["duration_seconds"], 2)
        self.assertEqual(item["decode"]["frames"], 20)

    def test_audio_only_corrupt_and_missing_inputs_continue(self):
        audio = self.root / "audio.m4a"
        self.generate("-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "0.2",
                      "-c:a", "aac", str(audio))
        broken = self.root / "broken.mp4"
        broken.write_bytes(b"not a video")
        report = self.probe(audio, broken, self.root / "missing.mp4", self.video)
        self.assertEqual(report["summary"], {"total": 4, "passed": 1, "failed": 3})
        self.assertIn("No video stream", report["results"][0]["error"])

    def test_readable_metadata_with_damaged_later_frames(self):
        damaged = self.root / "damaged.mp4"
        data = bytearray(self.video.read_bytes())
        start = data.index(b"mdat")
        end = start - 4 + int.from_bytes(data[start - 4:start], "big")
        middle = start + (end - start) * 2 // 3
        data[middle:end] = b"\xff" * (end - middle)
        damaged.write_bytes(data)
        report = self.probe(damaged, self.video)
        item = report["results"][0]
        self.assertIsNotNone(item["metadata"], item["error"])
        self.assertEqual(item["decode"]["status"], "failed")
        self.assertIn("decode:", item["error"])
        self.assertEqual(report["results"][1]["status"], "passed")

    def test_variable_frame_timing_matroska(self):
        video = self.root / "variable.mkv"
        self.generate("-f", "lavfi", "-i", "testsrc2=size=64x48:rate=10:duration=1",
                      "-vf", "setpts=N*N/(10*TB)", "-fps_mode", "vfr", "-c:v", "ffv1", str(video))
        # Establish that the fixture really has unequal frame intervals.
        frames = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "V:0", "-show_entries",
             "frame=best_effort_timestamp_time", "-of", "json", str(video)],
            capture_output=True, timeout=10, check=True,
        )
        timestamps = [float(frame["best_effort_timestamp_time"])
                      for frame in json.loads(frames.stdout)["frames"]]
        intervals = {round(b - a, 3) for a, b in zip(timestamps, timestamps[1:])}
        self.assertGreater(len(intervals), 1)
        item = self.probe(video)["results"][0]
        self.assertEqual(item["status"], "passed", item["error"])
        self.assertEqual(item["decode"]["frames"], len(timestamps))
        self.assertEqual(item["metadata"]["codec"], "ffv1")

    def test_first_video_stream_is_selected(self):
        video = self.root / "two-streams.mkv"
        self.generate("-f", "lavfi", "-i", "color=size=64x48:rate=5:duration=1",
                      "-f", "lavfi", "-i", "color=size=128x96:rate=10:duration=1",
                      "-map", "0:v", "-map", "1:v", "-c:v", "ffv1", str(video))
        item = self.probe(video)["results"][0]
        self.assertEqual(item["status"], "passed", item["error"])
        self.assertEqual(item["metadata"]["width"], 64)
        self.assertEqual(item["decode"]["frames"], 5)

    def test_rotation_preserves_coded_dimensions(self):
        video = self.root / "rotated.mp4"
        self.generate("-display_rotation:v:0", "90", "-i", str(self.video), "-c", "copy", str(video))
        item = self.probe(video)["results"][0]
        self.assertEqual(item["status"], "passed", item["error"])
        self.assertEqual(item["metadata"]["width"], 160)
        self.assertEqual(abs(item["metadata"]["rotation_degrees"]), 90)

    def test_cli_real_process(self):
        result = subprocess.run(
            [sys.executable, "-m", "beauty_cut", "probe", str(self.video),
             "--ffmpeg", FFMPEG, "--ffprobe", FFPROBE],
            capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8", "replace"))
        self.assertEqual(json.loads(result.stdout)["results"][0]["decode"]["frames"], 20)

    def test_cli_real_batch_failure_returns_one_and_continues(self):
        result = subprocess.run(
            [sys.executable, "-m", "beauty_cut", "probe", str(self.root / "missing.mp4"),
             str(self.video), "--ffmpeg", FFMPEG, "--ffprobe", FFPROBE],
            capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["summary"], {"total": 2, "passed": 1, "failed": 1})


if __name__ == "__main__":
    unittest.main()
