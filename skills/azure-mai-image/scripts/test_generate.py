import argparse
import base64
import contextlib
import io
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import zlib

import generate


CONFIG = {
    "subscription_id": "00000000-0000-0000-0000-000000000000",
    "tenant_id": "00000000-0000-0000-0000-000000000000",
    "account_name": "your-foundry-resource",
    "endpoint": "https://your-foundry-resource.services.ai.azure.com",
    "deployment": "mai-image",
    "model": "MAI-Image-2.6",
    "model_version": "2026-07-31",
}
FAKE_TOKEN = "test-token-not-a-credential"


def png(width=768, height=768):
    def chunk(kind, content):
        return (
            struct.pack(">I", len(content)) + kind + content
            + struct.pack(">I", zlib.crc32(kind + content) & 0xFFFFFFFF)
        )
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = zlib.compress((b"\0" + b"\x20\x60\xa0" * width) * height)
    return generate.PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


def response(image=None):
    return {"data": [{"b64_json": base64.b64encode(image or png()).decode("ascii")}]}


def http_error(code, headers=None, message="test failure"):
    return urllib.error.HTTPError(
        CONFIG["endpoint"], code, "Test", headers or {},
        io.BytesIO(json.dumps({"error": {"code": str(code), "message": message}}).encode()),
    )


class ImageTests(unittest.TestCase):
    def test_installed_configuration(self):
        self.assertEqual(generate.load_config()["endpoint"], CONFIG["endpoint"])

    def test_endpoint_rejects_credentials_paths_and_foreign_hosts(self):
        for endpoint in (
            "https://example.com", CONFIG["endpoint"] + "/extra",
            CONFIG["endpoint"] + "?query=yes",
            "http://" + CONFIG["endpoint"].removeprefix("https://"),
            "https://user:password@" + CONFIG["endpoint"].removeprefix("https://"),
        ):
            with self.subTest(endpoint=endpoint), patch.object(
                generate, "read_json", return_value={**CONFIG, "endpoint": endpoint},
            ):
                with self.assertRaises(generate.ImageError):
                    generate.load_config()

    def test_dimension_boundaries(self):
        for width, height in ((768, 768), (1024, 1024), (1365, 768), (768, 1365)):
            generate.validate_dimensions(width, height)
        for width, height in ((767, 768), (768, 767), (1366, 768), (1024, 1025), (-1, 1024)):
            with self.subTest(size=(width, height)), self.assertRaises(generate.ImageError):
                generate.validate_dimensions(width, height)

    def test_mai_26_allows_its_documented_maximum(self):
        for width, height in ((2048, 1152), (1152, 2048), (1536, 1536)):
            generate.validate_dimensions(width, height, "MAI-Image-2.6")
        for width, height in ((2048, 1153), (2049, 1152)):
            with self.subTest(size=(width, height)), self.assertRaises(generate.ImageError):
                generate.validate_dimensions(width, height, "MAI-Image-2.6")
        for model in (None, "MAI-Image-2.5"):
            with self.subTest(model=model), self.assertRaises(generate.ImageError):
                generate.validate_dimensions(2048, 1152, model)

    def test_online_generation_at_2048x1152_on_mai_26(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "wide.png"
            args = generate.parser().parse_args([
                "--prompt", "A wide original landscape", "--output", str(output),
                "--width", "2048", "--height", "1152",
            ])
            with patch.object(generate, "get_token", return_value=FAKE_TOKEN):
                with patch.object(generate, "post_image", return_value=response(png(2048, 1152))) as post:
                    summary = generate.generate(args)
            self.assertEqual((post.call_args.args[1]["width"], post.call_args.args[1]["height"]), (2048, 1152))
            self.assertEqual((summary["width"], summary["height"]), (2048, 1152))

    def test_valid_png(self):
        image = png()
        self.assertEqual(generate.decode_image(response(image)), (image, 768, 768))

    def test_bad_response_shapes_and_images(self):
        cases = [
            {}, {"data": []}, {"data": [{}, {}]}, {"data": ["not an object"]},
            {"data": [{}]}, {"data": [{"b64_json": "%%%"}]},
            response(b"not a PNG"), response(png()[:-12]),
            response(png(767, 768)),
        ]
        corrupt = bytearray(png())
        corrupt[29] ^= 1
        cases.append(response(corrupt))
        for value in cases:
            with self.subTest(value=str(value)[:100]), self.assertRaises(generate.ImageError):
                generate.decode_image(value)

    def test_token_pins_subscription_audience_and_never_prints_token(self):
        auth = {
            "accessToken": FAKE_TOKEN, "tenant": CONFIG["tenant_id"],
            "subscription": CONFIG["subscription_id"],
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(auth), "")
        captured = io.StringIO()
        with patch.object(generate.subprocess, "run", return_value=completed) as run:
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                self.assertEqual(generate.get_token(CONFIG), FAKE_TOKEN)
        self.assertEqual(captured.getvalue(), "")
        command = run.call_args.args[0]
        self.assertIn(CONFIG["subscription_id"], command)
        self.assertIn(generate.TOKEN_RESOURCE, command)
        self.assertNotIn(FAKE_TOKEN, command)

    def test_token_rejects_wrong_identity_and_missing_token(self):
        for auth in (
            {"accessToken": FAKE_TOKEN, "tenant": "wrong", "subscription": CONFIG["subscription_id"]},
            {"tenant": CONFIG["tenant_id"], "subscription": CONFIG["subscription_id"]},
        ):
            completed = subprocess.CompletedProcess([], 0, json.dumps(auth), "")
            with patch.object(generate.subprocess, "run", return_value=completed):
                with self.assertRaises(generate.ImageError):
                    generate.get_token(CONFIG)

    def test_cli_failure_does_not_disclose_stdout(self):
        completed = subprocess.CompletedProcess([], 1, FAKE_TOKEN, "Login required")
        with patch.object(generate.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(generate.ImageError, "Login required") as error:
                generate.get_token(CONFIG)
        self.assertNotIn(FAKE_TOKEN, str(error.exception))

    def test_429_retry_is_bounded_and_keeps_authorization_in_memory(self):
        opener = Mock()
        opener.open.side_effect = [
            http_error(429, {"Retry-After": "1"}),
            io.BytesIO(json.dumps(response()).encode()),
        ]
        with patch.object(generate.urllib.request, "build_opener", return_value=opener):
            with patch.object(generate.time, "sleep") as sleep, contextlib.redirect_stderr(io.StringIO()):
                result = generate.post_image(CONFIG, {"model": "mai-image"}, FAKE_TOKEN, 10, 1)
        self.assertIn("data", result)
        sleep.assert_called_once_with(1.0)
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual(opener.open.call_args.args[0].get_header("Authorization"), f"Bearer {FAKE_TOKEN}")

    def test_long_retry_after_and_5xx_are_not_retried(self):
        for error in (
            http_error(429, {"Retry-After": "121"}),
            http_error(429, {"Retry-After": "NaN"}),
            http_error(503),
            http_error(403, message=f"Do not expose {FAKE_TOKEN}"),
            urllib.error.URLError("connection dropped"),
        ):
            opener = Mock()
            opener.open.side_effect = error
            with patch.object(generate.urllib.request, "build_opener", return_value=opener):
                with patch.object(generate.time, "sleep") as sleep:
                    with self.assertRaises(generate.ImageError) as caught:
                        generate.post_image(CONFIG, {}, FAKE_TOKEN, 10, 1)
            self.assertNotIn(FAKE_TOKEN, str(caught.exception))
            self.assertEqual(opener.open.call_count, 1)
            sleep.assert_not_called()

    def test_redirects_are_refused(self):
        self.assertIsNone(generate.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_offline_decode_no_network_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "response.json"
            source.write_text(json.dumps(response()))
            output = root / "image.png"
            args = generate.parser().parse_args([
                "--response-json", str(source), "--output", str(output),
            ])
            with patch.object(generate, "get_token") as token:
                result = generate.generate(args)
                self.assertEqual(output.read_bytes(), png())
                self.assertEqual(result["width"], 768)
                with self.assertRaisesRegex(generate.ImageError, "already exists"):
                    generate.generate(args)
                token.assert_not_called()
            self.assertEqual(list(root.glob(".mai-image-*")), [])

    def test_online_generation_payload_and_exact_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "image.png"
            args = generate.parser().parse_args([
                "--prompt", "A calm original landscape", "--output", str(output),
                "--width", "768", "--height", "768",
            ])
            with patch.object(generate, "get_token", return_value=FAKE_TOKEN):
                with patch.object(generate, "post_image", return_value=response()) as post:
                    summary = generate.generate(args)
            payload = post.call_args.args[1]
            self.assertFalse(payload["web_grounding"])
            self.assertFalse(payload["auto_aspect_ratio"])
            self.assertEqual(payload["model"], "mai-image")
            self.assertEqual(summary["model"], "MAI-Image-2.6")
            self.assertNotIn(FAKE_TOKEN, json.dumps(summary))

    def test_failed_requests_leave_no_output_or_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "image.png"
            args = generate.parser().parse_args(["--prompt", "An island", "--output", str(output)])
            with patch.object(generate, "get_token", return_value=FAKE_TOKEN):
                with patch.object(generate, "post_image", side_effect=generate.ImageError("test failure")):
                    with self.assertRaises(generate.ImageError):
                        generate.generate(args)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_rejects_empty_prompts_and_missing_output_directories_before_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            for prompt, output in (
                (" ", Path(directory) / "image.png"),
                ("A mountain", Path(directory) / "missing" / "image.png"),
            ):
                args = generate.parser().parse_args(["--prompt", prompt, "--output", str(output)])
                with patch.object(generate, "get_token") as token:
                    with self.assertRaises(generate.ImageError):
                        generate.generate(args)
                    token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
