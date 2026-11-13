import contextlib
from fractions import Fraction
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from beauty_cut.__main__ import main
from beauty_cut.detect import _shots
from beauty_cut.preview import _image_plan, PreviewError, preview_videos
from beauty_cut.report import write_video


FFMPEG = os.environ.get("BEAUTYCUT_FFMPEG") or shutil.which("ffmpeg")
FFPROBE = os.environ.get("BEAUTYCUT_FFPROBE") or shutil.which("ffprobe")


class SelectionTests(unittest.TestCase):
    def make_item(self, times, cuts):
        frames = [{"pts": Fraction(t), "duration": Fraction(1, 10)} for t in times]
        boundaries = [{"frame": i} for i in cuts]
        shots, _, _ = _shots(frames, boundaries)
        return {"shots": shots, "analysis": {"boundaries": [
            {"frame": i, "time_seconds": shots[n + 1]["start_seconds"]} for n, i in enumerate(cuts)]}}, frames

    def test_temporal_midpoint_not_middle_frame_index(self):
        item, frames = self.make_item([5, Fraction(51, 10), Fraction(52, 10), 10], [])
        previews, boundaries, indexes = _image_plan(item, frames)
        self.assertEqual(previews[0]["frame"], 2)
        self.assertEqual(previews[0]["time_seconds"], 0.2)
        self.assertEqual(boundaries, [])
        self.assertEqual(indexes, [2])

    def test_single_frame_insert_and_clipped_boundaries(self):
        item, frames = self.make_item([0, Fraction(1, 10), Fraction(2, 10)], [1, 2])
        previews, boundaries, indexes = _image_plan(item, frames)
        self.assertEqual([r["frame"] for r in previews], [0, 1, 2])
        self.assertEqual([[r["frame"] for r in b["images"]] for b in boundaries], [[0, 1, 2], [0, 1, 2]])
        self.assertEqual(indexes, [0, 1, 2])
        self.assertEqual(previews[1]["image"], boundaries[0]["images"][1]["image"])

    def test_midpoint_exactly_at_frame_chooses_new_frame(self):
        item, frames = self.make_item([0, Fraction(1, 10)], [])
        self.assertEqual(_image_plan(item, frames)[0][0]["frame"], 1)

    def test_empty_input_and_unwritable_output(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PreviewError):
                preview_videos([], output_dir=directory)
            file = Path(directory) / "file"
            file.write_text("x")
            with self.assertRaises(PreviewError):
                preview_videos(["a.mp4"], output_dir=file)


@unittest.skipUnless(FFMPEG and FFPROBE, "Install tools or set BEAUTYCUT_FFMPEG/BEAUTYCUT_FFPROBE")
class PreviewIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / "中文 video.mkv"

    def generate(self, source=None, extra=()):
        source = source or ("color=red:s=160x120:r=10:d=0.5[a];color=blue:s=160x120:r=10:d=0.1[b];"
                            "color=green:s=160x120:r=10:d=0.4[c];[a][b][c]concat=n=3:v=1:a=0,setpts=N/(10*TB)")
        subprocess.run([FFMPEG, "-v", "error", "-nostdin", "-f", "lavfi", "-i", source,
                        *extra, "-fps_mode", "passthrough", "-c:v", "ffv1", str(self.video)],
                       capture_output=True, check=True, timeout=30)

    def preview(self, *files):
        return preview_videos(files or [self.video], output_dir=self.root / "报告 output",
                              ffmpeg=FFMPEG, ffprobe=FFPROBE, timeout=30)

    def test_images_correspond_to_known_shots_and_source_unchanged(self):
        self.generate()
        original = self.video.read_bytes()
        report = self.preview()
        self.assertEqual(report["summary"], {"total": 1, "passed": 1, "failed": 0})
        item = report["results"][0]
        self.assertEqual([s["preview"]["frame"] for s in item["shots"]], [2, 5, 8])
        root = Path(report["report_file"]).parent
        self.assertFalse((root / "INCOMPLETE.txt").exists())
        for shot, dominant in zip(item["shots"], [0, 2, 1]):
            image = root / Path(item["report"]).parent / shot["preview"]["image"]
            pixels = subprocess.run([FFMPEG, "-v", "error", "-i", str(image), "-vf", "scale=1:1",
                                     "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                                    check=True, capture_output=True, timeout=10).stdout
            self.assertEqual(len(pixels), 3)
            self.assertEqual(pixels.index(max(pixels)), dominant)
        self.assertEqual(self.video.read_bytes(), original)
        self.assertEqual(len(list((root / "video-001/images").glob("*.jpg"))), 7)
        self.assertEqual(item["playback"]["offset_seconds"], 0)
        self.assertFalse(item["playback"]["multiple_video_streams"])
        self.assertTrue((root / item["report"]).is_file())

    def test_nonzero_origin_variable_timing(self):
        self.generate("color=red:s=64x48:r=10:d=1,settb=1/1000,setpts=N*N*100",
                      extra=("-output_ts_offset", "5"))
        item = self.preview()["results"][0]
        self.assertEqual(item["status"], "passed", item["error"])
        self.assertEqual(item["analysis"]["origin_seconds"], 5)
        # Last frame begins at 8.1; midpoint of the full interval is around 4.1.
        self.assertEqual(item["shots"][0]["preview"]["frame"], 6)
        self.assertEqual(item["playback"]["offset_seconds"], 0)

    def test_one_frame_and_no_boundaries(self):
        self.generate("color=red:s=64x48:r=10:d=0.1")
        item = self.preview()["results"][0]
        self.assertEqual(item["status"], "passed", item["error"])
        self.assertEqual(item["shots"][0]["preview"]["frame"], 0)
        self.assertEqual(item["boundary_previews"], [])

    def test_hundreds_of_images_do_not_exceed_expression_parser_depth(self):
        self.generate("color=s=64x48:r=30:d=8,geq=lum=100:cb='if(mod(N,2),30,220)':cr=128")
        report = self.preview()
        item = report["results"][0]
        self.assertEqual(item["status"], "passed", item["error"])
        self.assertEqual(len(item["shots"]), 240)
        root = Path(report["report_file"]).parent
        self.assertEqual(len(list((root / "video-001/images").glob("*.jpg"))), 240)

    def test_bad_input_continues_same_names_isolated_runs_no_overwrite(self):
        self.generate()
        other = self.root / "other"
        other.mkdir()
        same = other / self.video.name
        shutil.copyfile(self.video, same)
        report = self.preview(self.root / "missing", self.video, same)
        self.assertEqual(report["summary"], {"total": 3, "passed": 2, "failed": 1})
        self.assertEqual(report["results"][0]["preview_status"], "not_run")
        self.assertEqual([r["report"] for r in report["results"]][1:],
                         ["video-002/index.html", "video-003/index.html"])
        self.assertNotEqual(self.preview()["report_file"], report["report_file"])
        self.assertTrue(Path(report["report_file"]).is_file())

    def test_extraction_error_keeps_detection_continues_and_removes_partial_output(self):
        self.generate()
        from beauty_cut.preview import _extract
        calls = []
        def fail_once(*args):
            calls.append(1)
            if len(calls) == 1:
                (args[1] / "partial.jpg").write_bytes(b"partial")
                raise subprocess.TimeoutExpired("ffmpeg", 30)
            return _extract(*args)
        with patch("beauty_cut.preview._extract", side_effect=fail_once):
            report = self.preview(self.video, self.video)
        self.assertEqual(report["summary"], {"total": 2, "passed": 1, "failed": 1})
        failed = report["results"][0]
        self.assertEqual(failed["detection_status"], "passed")
        self.assertEqual(failed["preview_status"], "failed")
        self.assertEqual(len(failed["shots"]), 3)
        self.assertNotIn("preview", failed["shots"][0])
        root = Path(report["report_file"]).parent
        self.assertFalse((root / "video-001").exists())
        self.assertFalse(list(root.glob(".working-*")))

    def test_html_escapes_untrusted_filename_and_has_no_remote_assets(self):
        self.generate()
        item = self.preview()["results"][0]
        item["source"]["name"] = '</script><script>alert("x")</script>&'
        page = self.root / "escape.html"
        write_video(page, item)
        text = page.read_text(encoding="utf-8")
        self.assertNotIn('</script><script>alert', text)
        self.assertIn("&lt;/script&gt;", text)
        self.assertIn("\\u003c/script\\u003e", text)
        self.assertNotIn('src="http', text)
        self.assertNotIn("fetch(", text)

    def test_timestamp_disagreement_rejects_image_delivery(self):
        self.generate()
        from beauty_cut.preview import _extract
        def wrong_time(path, folder, indexes, frames, ffmpeg, timeout):
            frames[indexes[0]]["pts"] += Fraction(1, 100)
            return _extract(path, folder, indexes, frames, ffmpeg, timeout)
        with patch("beauty_cut.preview._extract", side_effect=wrong_time):
            report = self.preview()
        item = report["results"][0]
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["detection_status"], "passed")
        self.assertIn("timestamps differ", item["error"])
        self.assertIsNone(item["report"])

    def test_source_change_during_extraction_rejected(self):
        self.generate()
        from beauty_cut.preview import _extract
        def change_source(*args):
            _extract(*args)
            with self.video.open("ab") as source:
                source.write(b"changed")
        with patch("beauty_cut.preview._extract", side_effect=change_source):
            item = self.preview()["results"][0]
        self.assertEqual(item["status"], "failed")
        self.assertIn("Source changed", item["error"])

    def test_audio_leading_video_offset_and_multiple_video_streams(self):
        offset = self.root / "offset.mp4"
        subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono:d=2",
                        "-itsoffset", "0.5", "-f", "lavfi", "-i", "color=red:s=64x48:r=10:d=1.5",
                        "-map", "1:v", "-map", "0:a", "-c:v", "libx264", "-c:a", "aac", str(offset)],
                       check=True, capture_output=True, timeout=30)
        item = self.preview(offset)["results"][0]
        self.assertEqual(item["status"], "passed", item["error"])
        self.assertAlmostEqual(item["playback"]["offset_seconds"], 0.5)
        multiple = self.root / "multiple.mp4"
        subprocess.run([FFMPEG, "-v", "error", "-i", str(offset), "-map", "0:v", "-map", "0:v",
                        "-c", "copy", str(multiple)], check=True, capture_output=True, timeout=30)
        item = self.preview(multiple)["results"][0]
        self.assertEqual(item["status"], "passed", item["error"])
        self.assertTrue(item["playback"]["multiple_video_streams"])

    def test_rotated_preview_matches_ffmpeg_display_orientation(self):
        source = self.root / "base.mp4"
        rotated = self.root / "rotated.mp4"
        subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x96:r=5:d=1",
                        "-c:v", "libx264", str(source)], check=True, capture_output=True, timeout=30)
        subprocess.run([FFMPEG, "-v", "error", "-display_rotation:v:0", "90", "-i", str(source),
                        "-c", "copy", str(rotated)],
                       check=True, capture_output=True, timeout=30)
        report = self.preview(rotated)
        item = report["results"][0]
        self.assertEqual(item["status"], "passed", item["error"])
        self.assertEqual(abs(item["metadata"]["rotation_degrees"]), 90)
        image = Path(report["report_file"]).parent / "video-001" / item["shots"][0]["preview"]["image"]
        output = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "stream=width,height",
                                 "-of", "json", str(image)], check=True, capture_output=True, timeout=10)
        stream = json.loads(output.stdout)["streams"][0]
        self.assertEqual((stream["width"], stream["height"]), (96, 160))

    def test_cli_report_and_exit_codes(self):
        self.generate()
        args = ["preview", str(self.video), "--output-dir", str(self.root / "cli"),
                "--ffmpeg", FFMPEG, "--ffprobe", FFPROBE]
        for files, expected in (([], 0), ([str(self.root / "missing")], 1)):
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                result = main(args[:2] + files + args[2:])
            self.assertEqual(result, expected)
            self.assertTrue(Path(json.loads(stream.getvalue())["report_file"]).is_file())
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            result = main(args + ["--threshold", "nan"])
        self.assertEqual(result, 2)
        self.assertIn("error", json.loads(stream.getvalue()))


if __name__ == "__main__":
    unittest.main()
