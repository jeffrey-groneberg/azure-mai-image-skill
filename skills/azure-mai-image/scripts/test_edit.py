"""Offline checks for reference-image editing; no live Azure requests."""

from email import policy
from email.parser import BytesParser
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import generate as mai
from test_generate import png, response


class EditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.reference = self.root / 'private "reference".png'
        self.reference.write_bytes(png(1344, 768))
        self.config = {
            "endpoint": "https://images.example.test",
            "deployment": "example-image",
            "model": "MAI-Image-2.6",
        }

    def tearDown(self):
        self.temp.cleanup()

    def arguments(self, *extra):
        return mai.parser().parse_args([
            "--prompt", "Make the arrows clearer.",
            "--output", str(self.root / "result.png"), *extra,
        ])

    def test_generation_keeps_default_dimensions(self):
        with patch.object(mai, "load_config", return_value=self.config), \
             patch.object(mai, "get_token", return_value="fixture-token"), \
             patch.object(mai, "post_image", return_value=response(png(1024, 1024))) as post:
            summary = mai.generate(self.arguments())
        payload = post.call_args.args[1]
        self.assertEqual((payload["width"], payload["height"]), (1024, 1024))
        self.assertIsNone(post.call_args.args[5])
        self.assertEqual(summary["operation"], "generate")

    def test_edit_omits_generation_dimensions_and_preserves_input(self):
        edited = png(1024, 1024)
        with patch.object(mai, "load_config", return_value=self.config), \
             patch.object(mai, "get_token", return_value="fixture-token"), \
             patch.object(mai, "post_image", return_value=response(edited)) as post:
            summary = mai.generate(self.arguments("--input-image", str(self.reference)))
        self.assertNotIn("width", post.call_args.args[1])
        self.assertNotIn("height", post.call_args.args[1])
        self.assertEqual(post.call_args.args[5], (self.reference.read_bytes(), "image/png"))
        self.assertEqual(summary["operation"], "edit")
        self.assertEqual((summary["width"], summary["height"]), (1024, 1024))
        self.assertEqual(summary["input_sha256"], mai.hashlib.sha256(self.reference.read_bytes()).hexdigest())
        self.assertEqual(self.reference.read_bytes(), png(1344, 768))
        self.assertEqual((self.root / "result.png").read_bytes(), edited)

    def test_multipart_binary_and_fixed_filename(self):
        payload = {"model": "example-image", "prompt": "Keep café, change arrow.\nNo extra text.", "auto_aspect_ratio": False}
        body, content_type = mai.edit_body(payload, mai.read_reference(self.reference))
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
        )
        fields = {part.get_param("name", header="content-disposition"): part for part in message.iter_parts()}
        self.assertEqual(set(fields), {"model", "prompt", "auto_aspect_ratio", "image"})
        self.assertEqual(fields["prompt"].get_payload(decode=True).decode(), payload["prompt"])
        self.assertEqual(fields["auto_aspect_ratio"].get_payload(decode=True), b"false")
        self.assertEqual(fields["image"].get_filename(), "reference.png")
        self.assertEqual(fields["image"].get_content_type(), "image/png")
        self.assertEqual(fields["image"].get_payload(decode=True), self.reference.read_bytes())
        self.assertNotIn(str(self.reference).encode(), body)

    def test_routes_and_redirect_guard(self):
        requests = []

        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                return io.BytesIO(json.dumps(response()).encode())

        with patch.object(mai.urllib.request, "build_opener", return_value=Opener()) as build:
            mai.post_image(self.config, {"model": "example-image"}, "fixture-token", 30, 0)
            mai.post_image(self.config, {"model": "example-image"}, "fixture-token", 30, 0,
                           mai.read_reference(self.reference))
        self.assertTrue(requests[0].full_url.endswith("/images/generations"))
        self.assertEqual(requests[0].get_header("Content-type"), "application/json")
        self.assertTrue(requests[1].full_url.endswith("/images/edits"))
        self.assertTrue(requests[1].get_header("Content-type").startswith("multipart/form-data; boundary="))
        self.assertIsInstance(build.call_args.args[0], mai.NoRedirect)

    def test_bad_input_never_acquires_a_token(self):
        self.reference.write_bytes(b"RIFFnot-a-PNG")
        with patch.object(mai, "load_config", return_value=self.config), \
             patch.object(mai, "get_token") as token:
            with self.assertRaisesRegex(mai.ImageError, "PNG or JPEG"):
                mai.generate(self.arguments("--input-image", str(self.reference)))
        token.assert_not_called()
        self.assertFalse((self.root / "result.png").exists())

    def test_input_limit(self):
        with patch.object(mai, "MAX_INPUT_BYTES", 32):
            with self.assertRaisesRegex(mai.ImageError, "upload limit"):
                mai.read_reference(self.reference)

    def test_incomplete_or_corrupt_png(self):
        for data in (png()[:-12], png()[:29] + b"\0\0\0\0" + png()[33:]):
            self.reference.write_bytes(data)
            with self.assertRaises(mai.ImageError):
                mai.read_reference(self.reference)

    def test_jpeg_type_comes_from_content(self):
        self.reference.write_bytes(b"\xff\xd8\xff\xe0jpeg-fixture\xff\xd9")
        image, content_type = mai.read_reference(self.reference)
        self.assertEqual(content_type, "image/jpeg")
        body, _ = mai.edit_body({"model": "x"}, (image, content_type))
        self.assertIn(b'filename="reference.jpg"', body)

    def test_edit_dimensions_rejected(self):
        with self.assertRaisesRegex(mai.ImageError, "does not accept width/height"):
            mai.generate(self.arguments("--input-image", str(self.reference), "--width", "1344"))

    def test_saved_response_cannot_edit(self):
        args = mai.parser().parse_args([
            "--response-json", str(self.root / "response.json"),
            "--input-image", str(self.reference), "--output", str(self.root / "result.png"),
        ])
        with self.assertRaisesRegex(mai.ImageError, "cannot be combined"):
            mai.generate(args)

    def test_existing_output_preserved(self):
        (self.root / "result.png").write_bytes(b"original")
        with patch.object(mai, "get_token") as token:
            with self.assertRaisesRegex(mai.ImageError, "already exists"):
                mai.generate(self.arguments("--input-image", str(self.reference)))
        self.assertEqual((self.root / "result.png").read_bytes(), b"original")
        token.assert_not_called()

    def test_multiple_references_repeat_the_image_field_in_order(self):
        second = self.root / "second.jpg"
        second.write_bytes(b"\xff\xd8\xff\xe0jpeg-fixture\xff\xd9")
        references = [mai.read_reference(self.reference), mai.read_reference(second)]
        body, content_type = mai.edit_body({"model": "example-image", "prompt": "Combine both."}, references)
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
        )
        images = [part for part in message.iter_parts()
                  if part.get_param("name", header="content-disposition") == "image"]
        self.assertEqual([part.get_filename() for part in images], ["reference-1.png", "reference-2.jpg"])
        self.assertEqual([part.get_content_type() for part in images], ["image/png", "image/jpeg"])
        self.assertEqual(images[0].get_payload(decode=True), self.reference.read_bytes())
        self.assertEqual(images[1].get_payload(decode=True), second.read_bytes())
        self.assertNotIn(str(self.root).encode(), body)

    def test_generate_sends_all_references_in_order(self):
        second = self.root / "second.png"
        second.write_bytes(png(768, 768))
        with patch.object(mai, "load_config", return_value=self.config), \
             patch.object(mai, "get_token", return_value="fixture-token"), \
             patch.object(mai, "post_image", return_value=response(png(1024, 1024))) as post:
            summary = mai.generate(self.arguments("--input-image", str(self.reference),
                                                  "--input-image", str(second)))
        self.assertEqual(post.call_args.args[5], [(self.reference.read_bytes(), "image/png"),
                                                  (second.read_bytes(), "image/png")])
        self.assertEqual(summary["input_sha256"],
                         [mai.hashlib.sha256(path.read_bytes()).hexdigest() for path in (self.reference, second)])

    def test_more_than_five_references_rejected_before_token(self):
        repeated = []
        for _ in range(mai.MAX_REFERENCES + 1):
            repeated += ["--input-image", str(self.reference)]
        with patch.object(mai, "load_config", return_value=self.config), \
             patch.object(mai, "get_token") as token:
            with self.assertRaisesRegex(mai.ImageError, "at most five"):
                mai.generate(self.arguments(*repeated))
        token.assert_not_called()
        with self.assertRaisesRegex(mai.ImageError, "1 to 5"):
            mai.edit_body({"model": "x"}, [mai.read_reference(self.reference)] * 6)


if __name__ == "__main__":
    unittest.main()
