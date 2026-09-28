"""Offline checks for sizing, deployment selection, edit output handling and upscaling; no live Azure requests."""

import importlib.util
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

import generate as mai
from test_generate import png, response

HAVE_UPSCALER = importlib.util.find_spec("PIL") is not None or (
    sys.platform == "darwin" and shutil.which("sips") is not None
)


def jpeg_header(width, height):
    """Just enough JPEG structure (SOI, APP0, SOF0, EOI) for dimension parsing."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {
            "endpoint": "https://images.example.test",
            "deployment": "mai-image",
            "model": "MAI-Image-2.6",
            "model_version": "2026-07-31",
            "deployments": {
                "mai-image": {"model": "MAI-Image-2.6", "model_version": "2026-07-31"},
                "mai-image-25": {"model": "MAI-Image-2.5", "model_version": "2026-06-02"},
            },
        }

    def tearDown(self):
        self.temp.cleanup()

    def arguments(self, *extra, output="result.png"):
        return mai.parser().parse_args(["--prompt", "A calm glass sculpture.",
                                        "--output", str(self.root / output), *extra])

    def run_generate(self, args, image):
        with patch.object(mai, "load_config", return_value=self.config), \
             patch.object(mai, "get_token", return_value="fixture-token"), \
             patch.object(mai, "post_image", return_value=response(image)) as post:
            return mai.generate(args), post

    # --- limits and sizes -------------------------------------------------------------
    def test_documented_pixel_limit_for_every_listed_model(self):
        for model in ("MAI-Image-2.6", "MAI-Image-2.6-Flash"):
            self.assertEqual(mai.pixel_limit(model), 2_359_296)
            self.assertTrue(mai.supports_26_options(model))
        for model in ("MAI-Image-2.5", "MAI-Image-2.5-Flash", "MAI-Image-2.5-Pro"):
            self.assertEqual(mai.pixel_limit(model), 1_048_576)
            self.assertFalse(mai.supports_26_options(model))

    def test_native_4k_is_outside_every_documented_limit(self):
        for model in mai.MODEL_PIXEL_LIMITS:
            with self.subTest(model=model), self.assertRaises(mai.ImageError):
                mai.validate_dimensions(3840, 2160, model)

    def test_largest_size_for_common_aspect_ratios(self):
        expected = {"16:9": (2048, 1152), "9:16": (1152, 2048), "1:1": (1536, 1536)}
        for aspect, size in expected.items():
            self.assertEqual(mai.largest_size(mai.parse_aspect(aspect), "MAI-Image-2.6"), size)
        self.assertEqual(mai.largest_size(mai.parse_aspect("16:9"), "MAI-Image-2.5"), (1360, 768))
        self.assertEqual(mai.largest_size(mai.parse_aspect("1:1"), "MAI-Image-2.5"), (1024, 1024))
        for aspect in ("2:1", "3:2", "4:3", "21:9", "2.39:1", "4:1", "3:4"):
            width, height = mai.largest_size(mai.parse_aspect(aspect), "MAI-Image-2.6")
            with self.subTest(aspect=aspect, size=(width, height)):
                mai.validate_dimensions(width, height, "MAI-Image-2.6")
                self.assertEqual((width % 16, height % 16), (0, 0))
                self.assertLessEqual(abs(width / height - mai.parse_aspect(aspect)) / mai.parse_aspect(aspect), 0.01)
                self.assertGreater(width * height, 0.97 * 2_359_296)

    def test_impossible_aspects_and_bad_syntax(self):
        with self.assertRaises(mai.ImageError):
            mai.largest_size(mai.parse_aspect("5:1"), "MAI-Image-2.6")
        for text in ("wide", "16:", "0:9", "-1:2", ""):
            with self.subTest(text=text), self.assertRaises(mai.ImageError):
                mai.parse_aspect(text)
        self.assertAlmostEqual(mai.parse_aspect("16x9"), 16 / 9)

    def test_aspect_sets_the_generation_size(self):
        summary, post = self.run_generate(self.arguments("--aspect", "16:9"), png(2048, 1152))
        payload = post.call_args.args[1]
        self.assertEqual((payload["width"], payload["height"]), (2048, 1152))
        self.assertEqual((summary["width"], summary["height"]), (2048, 1152))

    def test_aspect_conflicts_are_rejected_before_token(self):
        reference = self.root / "ref.png"
        reference.write_bytes(png(1024, 1024))
        for extra in (("--aspect", "16:9", "--width", "2048"),
                      ("--aspect", "16:9", "--input-image", str(reference))):
            with self.subTest(extra=extra), patch.object(mai, "get_token") as token:
                with self.assertRaises(mai.ImageError):
                    mai.generate(self.arguments(*extra))
                token.assert_not_called()

    # --- deployments and model-specific options ----------------------------------------
    def test_mai_25_deployment_omits_26_only_fields_and_uses_its_limit(self):
        summary, post = self.run_generate(self.arguments("--deployment", "mai-image-25", "--aspect", "16:9"),
                                          png(1360, 768))
        payload = post.call_args.args[1]
        self.assertEqual(payload["model"], "mai-image-25")
        self.assertNotIn("auto_aspect_ratio", payload)
        self.assertNotIn("web_grounding", payload)
        self.assertEqual((payload["width"], payload["height"]), (1360, 768))
        self.assertEqual(summary["model"], "MAI-Image-2.5")

    def test_26_only_flags_and_unknown_deployments_fail_before_token(self):
        for extra in (("--deployment", "mai-image-25", "--web-grounding"),
                      ("--deployment", "mai-image-25", "--auto-aspect-ratio"),
                      ("--deployment", "not-configured")):
            with self.subTest(extra=extra), \
                 patch.object(mai, "load_config", return_value=self.config), \
                 patch.object(mai, "get_token") as token:
                with self.assertRaises(mai.ImageError):
                    mai.generate(self.arguments(*extra))
                token.assert_not_called()

    # --- edits ---------------------------------------------------------------------------
    def test_edit_results_with_a_side_below_768_are_kept(self):
        reference = self.root / "wide-sketch.png"
        reference.write_bytes(png(1568, 768))
        summary, _ = self.run_generate(self.arguments("--input-image", str(reference)), png(1440, 704))
        self.assertEqual((summary["width"], summary["height"]), (1440, 704))
        self.assertEqual(mai.image_size((self.root / "result.png").read_bytes()), (1440, 704))

    def test_generation_still_requires_exact_dimensions(self):
        with self.assertRaises(mai.ImageError):
            self.run_generate(self.arguments("--width", "2048", "--height", "1152"), png(1440, 704))
        self.assertFalse((self.root / "result.png").exists())

    # --- upscaling -----------------------------------------------------------------------
    def test_upscale_targets(self):
        self.assertEqual(mai.upscale_target("4k", 2048, 1152), (3840, 2160))
        self.assertEqual(mai.upscale_target("4K", 1152, 2048), (2160, 3840))
        self.assertEqual(mai.upscale_target("4k", 1536, 1536), (3840, 3840))
        self.assertEqual(mai.upscale_target("2x", 2048, 1152), (4096, 2304))
        self.assertEqual(mai.upscale_target("3840x2160", 2048, 1152), (3840, 2160))
        for spec, size in (("3840x2000", (2048, 1152)), ("1024x576", (2048, 1152)), ("0.5x", (2048, 1152)),
                           ("16k", (2048, 1152)), ("5x", (2048, 1152)), ("huge", (2048, 1152))):
            with self.subTest(spec=spec), self.assertRaises(mai.ImageError):
                mai.upscale_target(spec, *size)

    def test_image_size_reads_png_and_jpeg(self):
        self.assertEqual(mai.image_size(png(800, 450)), (800, 450))
        self.assertEqual(mai.image_size(jpeg_header(1920, 1080)), (1920, 1080))
        with self.assertRaises(mai.ImageError):
            mai.image_size(b"GIF89a")

    def test_bad_upscale_spec_fails_before_the_billable_request(self):
        with patch.object(mai, "load_config", return_value=self.config), \
             patch.object(mai, "get_token") as token:
            with self.assertRaises(mai.ImageError):
                mai.generate(self.arguments("--aspect", "16:9", "--upscale", "1024x576"))
            token.assert_not_called()

    def test_existing_upscale_output_is_never_overwritten(self):
        (self.root / "result-4k.png").write_bytes(b"keep")
        with patch.object(mai, "get_token") as token:
            with self.assertRaises(mai.ImageError):
                mai.generate(self.arguments("--upscale", "4k"))
            token.assert_not_called()
        self.assertEqual((self.root / "result-4k.png").read_bytes(), b"keep")

    @unittest.skipUnless(HAVE_UPSCALER, "needs Pillow or macOS sips")
    def test_generation_with_upscale_writes_native_and_upscaled_files(self):
        summary, _ = self.run_generate(self.arguments("--width", "1024", "--height", "768", "--upscale", "2x"),
                                       png(1024, 768))
        self.assertEqual(mai.image_size((self.root / "result.png").read_bytes()), (1024, 768))
        upscaled = Path(summary["upscaled"]["image"])
        self.assertEqual(upscaled.name, "result-2x.png")
        self.assertEqual(mai.image_size(upscaled.read_bytes()), (2048, 1536))
        self.assertIn("no new detail", summary["upscaled"]["note"])

    @unittest.skipUnless(HAVE_UPSCALER, "needs Pillow or macOS sips")
    def test_upscale_existing_file_needs_no_azure(self):
        source = self.root / "native.png"
        source.write_bytes(png(1024, 576))
        args = mai.parser().parse_args(["--upscale-from", str(source), "--upscale", "4k",
                                        "--output", str(self.root / "native-4k.png")])
        with patch.object(mai, "get_token") as token, patch.object(mai, "load_config") as config:
            summary = mai.generate(args)
            token.assert_not_called()
            config.assert_not_called()
        self.assertEqual((summary["width"], summary["height"]), (3840, 2160))
        self.assertEqual(mai.image_size((self.root / "native-4k.png").read_bytes()), (3840, 2160))
        self.assertEqual(source.read_bytes(), png(1024, 576))

    def test_upscale_existing_rejects_generation_options(self):
        source = self.root / "native.png"
        source.write_bytes(png(1024, 576))
        for extra in (("--width", "2048"), ("--aspect", "16:9"), ("--web-grounding",), ()):
            args = mai.parser().parse_args(["--upscale-from", str(source), "--output", str(self.root / "x.png"),
                                            *(("--upscale", "4k") if extra else ()), *extra])
            with self.subTest(extra=extra), self.assertRaises(mai.ImageError):
                mai.generate(args)


if __name__ == "__main__":
    unittest.main()
