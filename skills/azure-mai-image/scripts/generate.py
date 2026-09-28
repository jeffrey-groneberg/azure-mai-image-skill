#!/usr/bin/env python3
"""Generate or edit one MAI image using an in-memory Azure CLI access token.

Also computes the largest valid size for an aspect ratio and can write a clearly labelled,
interpolated upscale (for example 4K for full-bleed slides) from a generated or existing image."""

import argparse
import base64
import binascii
import datetime
import email.utils
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zlib


SKILL_ROOT = Path(__file__).resolve().parent.parent
TOKEN_RESOURCE = "https://cognitiveservices.azure.com"
MAX_PIXELS = 1_048_576
MAX_PIXELS_MAI_26 = 2_359_296  # documented limit for MAI-Image-2.6 models, e.g. 2048x1152
# Documented maximum total pixels per image (Microsoft Learn, "Deploy and use MAI image models", 2026-09-28).
# The service enforces it: 3840x2160 returns HTTP 400 "exceeds the maximum of 2359296".
MODEL_PIXEL_LIMITS = {
    "MAI-Image-2.6": MAX_PIXELS_MAI_26,
    "MAI-Image-2.6-Flash": MAX_PIXELS_MAI_26,
    "MAI-Image-2.5-Pro": MAX_PIXELS,
    "MAI-Image-2.5-Flash": MAX_PIXELS,
    "MAI-Image-2.5": MAX_PIXELS,
}
MIN_SIDE = 768
SIZE_STEP = 16  # sizes computed by --aspect are multiples of 16 pixels
MAX_UPSCALE_PIXELS = 7680 * 4320
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_REFERENCES = 5  # documented limit of the MAI image edits API
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_END = b"\x00\x00\x00\x00IEND\xaeB\x60\x82"


class ImageError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a bearer token to a redirected destination.
        return None


def safe_diagnostic(text: str, token: str = "") -> str:
    if token:
        text = text.replace(token, "[redacted]")
    text = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
        "[redacted]",
        text,
    )
    return text[:1500]


def parse_json(raw: str | bytes, source: str) -> dict:
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ImageError(f"{source} did not contain valid JSON.") from exc
    if not isinstance(result, dict):
        raise ImageError(f"{source} must contain a JSON object.")
    return result


def read_json(path: Path) -> dict:
    with path.open("rb") as handle:
        raw = handle.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ImageError(f"JSON file exceeds the 32 MiB safety limit: {path}")
    return parse_json(raw, str(path))


def load_config() -> dict:
    config = read_json(SKILL_ROOT / "config.json")
    for key in (
        "subscription_id", "tenant_id", "account_name",
        "endpoint", "deployment", "model", "model_version",
    ):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ImageError(f"Missing or invalid config.json field: {key}")
    try:
        uuid.UUID(config["subscription_id"])
        uuid.UUID(config["tenant_id"])
    except ValueError as exc:
        raise ImageError("Subscription and tenant IDs must be valid UUIDs.") from exc
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]", config["account_name"]):
        raise ImageError("Invalid Azure account name in config.json.")
    expected = f"https://{config['account_name']}.services.ai.azure.com"
    if config["endpoint"].rstrip("/") != expected:
        raise ImageError("Endpoint must be the configured account's HTTPS Azure endpoint.")
    config["endpoint"] = expected
    return config


def select_deployment(config: dict, name: str | None = None) -> dict:
    """Pick a configured deployment (default: config["deployment"]); its model sets the limits and options."""
    deployments = config.get("deployments") or {
        config["deployment"]: {"model": config.get("model"), "model_version": config.get("model_version", "")},
    }
    if not isinstance(deployments, dict):
        raise ImageError("config.json field 'deployments' must be an object.")
    name = name or config["deployment"]
    if name not in deployments:
        raise ImageError(f"Unknown deployment {name!r}; configured: {', '.join(sorted(deployments))}.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        raise ImageError("Invalid deployment name in config.json.")
    entry = deployments[name]
    model = entry.get("model") if isinstance(entry, dict) else None
    if not isinstance(model, str) or not model.startswith("MAI-Image-"):
        raise ImageError(f"Deployment {name!r} needs a MAI image 'model' in config.json.")
    return {**config, "deployment": name, "model": model, "model_version": entry.get("model_version", "")}


def image_size(data: bytes) -> tuple[int, int]:
    """Width and height of a PNG or baseline/progressive JPEG, without third-party packages."""
    if data.startswith(PNG_SIGNATURE) and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            length = struct.unpack(">H", data[index + 2:index + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height, width = struct.unpack(">HH", data[index + 5:index + 9])
                return width, height
            index += 2 + length
    raise ImageError("Could not read the image dimensions (expected PNG or JPEG).")


def upscale_target(spec: str, width: int, height: int) -> tuple[int, int]:
    """'4k' (long side 3840), '8k' (long side 7680), a factor such as '2x', or an exact 'WxH' that keeps the aspect."""
    text = (spec or "").strip().lower()
    long_sides = {"4k": 3840, "uhd": 3840, "8k": 7680}
    if text in long_sides:
        side = long_sides[text]
        target = (side, round(side * height / width)) if width >= height else (round(side * width / height), side)
    elif re.fullmatch(r"\d+(?:\.\d+)?x", text):
        factor = float(text[:-1])
        target = (round(width * factor), round(height * factor))
    elif re.fullmatch(r"\d+x\d+", text):
        target = tuple(int(v) for v in text.split("x"))
        if abs(target[0] / target[1] - width / height) / (width / height) > 0.01:
            raise ImageError(f"Upscale size {text} changes the aspect ratio of the {width}x{height} image.")
    else:
        raise ImageError("Upscale must be 4k, 8k, a factor such as 2x, or an exact WxH.")
    if target[0] <= width or target[1] <= height:
        raise ImageError(f"Upscale target {target[0]}x{target[1]} must be larger than {width}x{height}.")
    if target[0] * target[1] > MAX_UPSCALE_PIXELS:
        raise ImageError("Upscale target exceeds the helper's 8K (33.2 MP) limit.")
    return target


def upscale_image(source: Path, target: Path, width: int, height: int) -> str:
    """Write an interpolated upscale of `source` to the new PNG `target`; returns the method used.
    Interpolation adds sharpness headroom for large displays but no new image detail."""
    if os.path.lexists(target):
        raise ImageError(f"Upscale output already exists; choose a new filename: {target}")
    with tempfile.NamedTemporaryFile(prefix=".mai-upscale-", suffix=".png", dir=target.parent) as temporary:
        try:
            from PIL import Image, ImageFilter  # optional; generation itself needs no packages
        except ImportError:
            Image = None
        if Image is not None:
            with Image.open(source) as picture:
                picture.load()
                mode = "RGBA" if picture.mode in ("RGBA", "LA", "P", "PA") else "RGB"
                bigger = picture.convert(mode).resize((width, height), Image.Resampling.LANCZOS)
            bigger = bigger.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=2))
            bigger.save(temporary.name, "PNG")
            method = "Pillow Lanczos resampling with a light unsharp mask"
        elif sys.platform == "darwin" and shutil.which("sips"):
            result = subprocess.run(
                ["sips", "-s", "format", "png", "--resampleHeightWidth", str(height), str(width),
                 str(source), "--out", temporary.name],
                capture_output=True, text=True, timeout=180, check=False,
            )
            if result.returncode:
                raise ImageError(f"sips upscaling failed: {safe_diagnostic(result.stderr.strip())}")
            method = "macOS sips resampling"
        else:
            raise ImageError("Upscaling needs Pillow (pip install pillow) or macOS sips; no file was written.")
        with open(temporary.name, "rb") as handle:
            head = handle.read(32)
        if image_size(head) != (width, height):
            raise ImageError("Upscaled PNG has unexpected dimensions; no file was written.")
        os.fsync(temporary.fileno())
        os.link(temporary.name, target)
    return method


def upscale_path(output: Path, spec: str) -> Path:
    label = re.sub(r"[^0-9a-z.]+", "-", spec.strip().lower()).strip("-")
    return output.with_name(f"{output.stem}-{label}.png")


def get_token(config: dict) -> str:
    command = [
        "az", "account", "get-access-token",
        "--subscription", config["subscription_id"],
        "--resource", TOKEN_RESOURCE,
        "--output", "json", "--only-show-errors",
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=60, check=False,
        )
    except FileNotFoundError as exc:
        raise ImageError("Azure CLI is missing. Install az and sign in before generating.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ImageError("Azure CLI token acquisition timed out; no image request was sent.") from exc
    if result.returncode:
        detail = safe_diagnostic(result.stderr.strip()) or "No diagnostic returned."
        raise ImageError(
            f"Azure CLI authentication failed (exit {result.returncode}): {detail}\n"
            f"Sign in with az login --tenant {config['tenant_id']} and retry."
        )
    auth = parse_json(result.stdout, "Azure CLI token response")
    if (
        auth.get("tenant") != config["tenant_id"]
        or auth.get("subscription") != config["subscription_id"]
    ):
        raise ImageError("Azure CLI returned a token for a different tenant or subscription.")
    token = auth.get("accessToken")
    if not isinstance(token, str) or not token or any(c.isspace() for c in token):
        raise ImageError("Azure CLI returned an invalid access token.")
    return token


def pixel_limit(model: str | None) -> int:
    if model in MODEL_PIXEL_LIMITS:
        return MODEL_PIXEL_LIMITS[model]
    return MAX_PIXELS_MAI_26 if (model or "").startswith("MAI-Image-2.6") else MAX_PIXELS


def supports_26_options(model: str | None) -> bool:
    """auto_aspect_ratio and web_grounding exist only on MAI-Image-2.6 and MAI-Image-2.6-Flash."""
    return (model or "").startswith("MAI-Image-2.6")


def parse_aspect(text: str) -> float:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[:x/]\s*(\d+(?:\.\d+)?)\s*", text or "")
    if not match or float(match.group(1)) <= 0 or float(match.group(2)) <= 0:
        raise ImageError("Aspect ratio must look like 16:9, 1:1, 9:16 or 2.39:1.")
    return float(match.group(1)) / float(match.group(2))


def largest_size(aspect: float, model: str | None = None, step: int = SIZE_STEP,
                 tolerance: float = 0.01) -> tuple[int, int]:
    """Largest width x height (multiples of `step`, each >= 768) within the model's pixel limit whose
    aspect ratio is within `tolerance` of the requested one."""
    limit = pixel_limit(model)
    best = None
    height = MIN_SIDE
    while height * MIN_SIDE <= limit:
        width = int(round(aspect * height / step)) * step
        if width >= MIN_SIDE and width * height <= limit and abs(width / height - aspect) / aspect <= tolerance:
            key = (width * height, -abs(width / height - aspect))
            if best is None or key > best[0]:
                best = (key, (width, height))
        height += step
    if best is None:
        raise ImageError(
            f"No valid size for aspect ratio {aspect:.4g} on {model or 'this model'}: each side must be at least "
            f"{MIN_SIDE} px and the total at most {limit:,} px."
        )
    return best[1]


def validate_dimensions(width: int, height: int, model: str | None = None) -> None:
    limit = pixel_limit(model)
    if width < MIN_SIDE or height < MIN_SIDE or width * height > limit:
        sizes = "1024x1024, 1344x768, or 768x1344"
        if limit > MAX_PIXELS:
            sizes = "1024x1024, 1344x768, 768x1344, 2048x1152, or 1152x2048"
        raise ImageError(
            "Width and height must each be at least 768; their product must be "
            f"at most {limit:,}. Use {sizes}."
        )


def retry_delay(headers) -> float:
    value = headers.get("Retry-After") if headers else None
    if not value:
        return 60.0
    try:
        seconds = float(value)
    except ValueError:
        try:
            stamp = email.utils.parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=datetime.timezone.utc)
            seconds = (stamp - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError) as exc:
            raise ImageError("Azure returned an invalid Retry-After header; retry manually.") from exc
    if not math.isfinite(seconds):
        raise ImageError("Azure returned a non-finite Retry-After header; retry manually.")
    return max(1.0, seconds)


def http_message(exc: urllib.error.HTTPError, token: str) -> str:
    try:
        error = json.loads(exc.read(8192)).get("error", {})
    except (ValueError, UnicodeError, AttributeError):
        return f"HTTP {exc.code} ({exc.reason})"
    if not isinstance(error, dict):
        return f"HTTP {exc.code} ({exc.reason})"
    detail = safe_diagnostic(str(error.get("message", exc.reason)), token)
    return f"HTTP {exc.code} [{error.get('code', exc.code)}]: {detail}"


def read_reference(path: Path) -> tuple[bytes, str]:
    with path.expanduser().open("rb") as source:
        image = source.read(MAX_INPUT_BYTES + 1)
    if len(image) > MAX_INPUT_BYTES:
        raise ImageError("Reference image exceeds the helper's 20 MiB upload limit.")
    if (
        len(image) >= 57 and image.startswith(PNG_SIGNATURE)
        and image[8:16] == b"\x00\x00\x00\x0dIHDR" and image.endswith(PNG_END)
    ):
        expected_crc = struct.unpack(">I", image[29:33])[0]
        if zlib.crc32(image[12:29]) & 0xFFFFFFFF != expected_crc:
            raise ImageError("Reference PNG header checksum is invalid.")
        return image, "image/png"
    if image.startswith(b"\xff\xd8\xff") and image.endswith(b"\xff\xd9"):
        return image, "image/jpeg"
    raise ImageError("Reference image must be a complete PNG or JPEG, not a renamed WebP/GIF.")


def edit_body(payload: dict, reference) -> tuple[bytes, str]:
    """Multipart body with one repeated `image` part per reference, in the given order."""
    references = [reference] if isinstance(reference, tuple) else list(reference)
    if not 1 <= len(references) <= MAX_REFERENCES:
        raise ImageError(f"The MAI edits API accepts 1 to {MAX_REFERENCES} reference images.")
    fields = [
        (name, (json.dumps(value) if isinstance(value, bool) else str(value)).encode("utf-8"))
        for name, value in payload.items()
    ]
    boundary = "mai-image-" + uuid.uuid4().hex
    while any(boundary.encode() in image for image, _ in references) or any(
        boundary.encode() in value for _, value in fields
    ):
        boundary = "mai-image-" + uuid.uuid4().hex
    parts = []
    for name, value in fields:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            + value + b"\r\n"
        )
    for index, (image, content_type) in enumerate(references, 1):
        extension = "png" if content_type == "image/png" else "jpg"
        stem = "reference" if len(references) == 1 else f"reference-{index}"
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
            f'filename="{stem}.{extension}"\r\nContent-Type: {content_type}\r\n\r\n'.encode()
            + image + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def post_image(
    config: dict, payload: dict, token: str, timeout: float, retries: int,
    reference: tuple[bytes, str] | list[tuple[bytes, str]] | None = None,
) -> dict:
    if reference is None:
        route = "/mai/v1/images/generations"
        body, content_type = json.dumps(payload).encode("utf-8"), "application/json"
    else:
        route = "/mai/v1/images/edits"
        body, content_type = edit_body(payload, reference)
    request = urllib.request.Request(
        config["endpoint"] + route,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
            "Accept": "application/json",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(NoRedirect())
    for attempt in range(retries + 1):
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ImageError("Azure response exceeded the 32 MiB safety limit.")
            return parse_json(raw, "Azure image response")
        except urllib.error.HTTPError as exc:
            try:
                message = http_message(exc, token)
                if exc.code == 429 and attempt < retries:
                    delay = retry_delay(exc.headers)
                    if delay > 120:
                        raise ImageError(
                            f"{message}\nRetry-After exceeds 120 seconds; retry manually later."
                        ) from exc
                    print(
                        f"Azure rate limit: retry {attempt + 1}/{retries} in {delay:g}s.",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                advice = {
                    401: "Check your Azure sign-in, token audience, and inference RBAC.",
                    403: "Check inference RBAC and network policy. Do not enable API keys.",
                    404: "Check the configured endpoint, deployment, and model retirement.",
                    429: "The deployment allows 1 request/minute; retry later.",
                }.get(exc.code, "Request was not retried automatically.")
                raise ImageError(f"{message}\n{advice}") from exc
            finally:
                exc.close()
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError) as exc:
            detail = safe_diagnostic(str(exc), token)
            raise ImageError(
                f"Network request failed: {detail}\n"
                "Outcome may be unknown; not retried to avoid duplicate image charges."
            ) from exc
    raise ImageError("No image response was returned.")


def decode_image(result: dict, model: str | None = None, strict: bool = True) -> tuple[bytes, int, int]:
    data = result.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ImageError("Expected exactly one image in response.data; no file was saved.")
    encoded = data[0].get("b64_json")
    if not isinstance(encoded, str) or not encoded:
        raise ImageError("Response is missing data[0].b64_json; no file was saved.")
    try:
        image = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ImageError("Response contained invalid base64 image data.") from exc
    if (
        len(image) < 57
        or not image.startswith(PNG_SIGNATURE)
        or image[8:16] != b"\x00\x00\x00\x0dIHDR"
        or not image.endswith(PNG_END)
    ):
        raise ImageError("Response did not contain a complete PNG image.")
    expected_crc = struct.unpack(">I", image[29:33])[0]
    if zlib.crc32(image[12:29]) & 0xFFFFFFFF != expected_crc:
        raise ImageError("PNG header checksum is invalid.")
    width, height = struct.unpack(">II", image[16:24])
    if strict:
        validate_dimensions(width, height, model)
    elif not (0 < width and 0 < height and width * height <= pixel_limit("MAI-Image-2.6")):
        raise ImageError(f"Response image has implausible dimensions {width}x{height}; no file was saved.")
    return image, width, height


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    source = cli.add_mutually_exclusive_group(required=True)
    source.add_argument("--prompt", help="Description of the image to generate.")
    source.add_argument("--prompt-file", type=Path, help="UTF-8 prompt file.")
    source.add_argument("--response-json", type=Path, help="Decode a saved az rest response; no API call.")
    source.add_argument("--upscale-from", type=Path,
                        help="Upscale an existing PNG/JPEG to --output with --upscale; no API call.")
    cli.add_argument("--input-image", type=Path, action="append",
                     help="PNG/JPEG reference for the MAI edits API. Repeat for up to five references; "
                          "the first is the image being edited.")
    cli.add_argument("--output", required=True, type=Path, help="New PNG path; existing files are never overwritten.")
    cli.add_argument("--width", type=int, help="Generation width; default 1024. Not an edit parameter.")
    cli.add_argument("--height", type=int, help="Generation height; default 1024. Not an edit parameter.")
    cli.add_argument("--aspect", help="Generation only: largest valid size for this aspect ratio, e.g. 16:9 "
                                      "(2048x1152 on MAI-Image-2.6). Cannot combine with --width/--height.")
    cli.add_argument("--upscale", help="Also write an interpolated upscale: 4k, 8k, a factor such as 2x, or WxH. "
                                       "Saved as <output>-<spec>.png; no new detail is created.")
    cli.add_argument("--deployment", help="Configured deployment name (config.json 'deployments'); "
                                          "default: the configured deployment.")
    cli.add_argument("--auto-aspect-ratio", action="store_true", help="Let MAI 2.6 choose framing.")
    cli.add_argument("--web-grounding", action="store_true", help="Opt in to Bing-grounded generation.")
    cli.add_argument("--timeout", type=float, default=300, help="Network I/O timeout in seconds (1-600).")
    cli.add_argument("--retries", type=int, choices=(0, 1, 2), default=1, help="Retries of HTTP 429 only.")
    return cli


def upscale_only(args: argparse.Namespace) -> dict:
    """Interpolated upscale of an existing PNG/JPEG; no Azure call and no token."""
    extras = [args.input_image, args.width, args.height, getattr(args, "aspect", None),
              getattr(args, "deployment", None)]
    if any(value not in (None, []) for value in extras) or args.auto_aspect_ratio or args.web_grounding:
        raise ImageError("--upscale-from accepts only --upscale and --output.")
    if not getattr(args, "upscale", None):
        raise ImageError("--upscale-from needs --upscale, for example 4k.")
    source = args.upscale_from.expanduser().absolute()
    data, _ = read_reference(source)
    width, height = image_size(data)
    output = args.output.expanduser().absolute()
    if output.suffix.lower() != ".png":
        raise ImageError("Output must have a .png extension.")
    if not output.parent.is_dir():
        raise ImageError(f"Output directory does not exist: {output.parent}")
    target_width, target_height = upscale_target(args.upscale, width, height)
    method = upscale_image(source, output, target_width, target_height)
    return {
        "image": str(output), "width": target_width, "height": target_height,
        "source": str(source), "source_width": width, "source_height": height,
        "method": method, "note": "Interpolated upscale: no new detail; label as upscaled.",
    }


def generate(args: argparse.Namespace) -> dict:
    if getattr(args, "upscale_from", None) is not None:
        return upscale_only(args)
    inputs = args.input_image or []
    aspect = getattr(args, "aspect", None)
    upscale = getattr(args, "upscale", None)
    deployment = getattr(args, "deployment", None)
    if not 1 <= args.timeout <= 600:
        raise ImageError("Timeout must be between 1 and 600 seconds.")
    if inputs and args.response_json is not None:
        raise ImageError("--input-image cannot be combined with --response-json.")
    if inputs and (args.width is not None or args.height is not None):
        raise ImageError("The MAI edits API does not accept width/height; omit those options.")
    if inputs and aspect is not None:
        raise ImageError("The MAI edits API does not accept a size; --aspect is for generation only.")
    if aspect is not None and (args.width is not None or args.height is not None):
        raise ImageError("Use either --aspect or --width/--height, not both.")
    if args.response_json is not None and (aspect is not None or deployment is not None):
        raise ImageError("--response-json decodes a saved response; --aspect and --deployment do not apply.")
    if len(inputs) > MAX_REFERENCES:
        raise ImageError(f"Pass at most five --input-image files; the MAI edits API accepts up to {MAX_REFERENCES}.")
    width = args.width if args.width is not None else 1024
    height = args.height if args.height is not None else 1024
    output = args.output.expanduser().absolute()
    if output.suffix.lower() != ".png":
        raise ImageError("Output must have a .png extension.")
    if os.path.lexists(output):
        raise ImageError(f"Output already exists; choose a new filename: {output}")
    if not output.parent.is_dir():
        raise ImageError(f"Output directory does not exist: {output.parent}")
    upscale_output = upscale_path(output, upscale) if upscale is not None else None
    if upscale_output is not None and os.path.lexists(upscale_output):
        raise ImageError(f"Upscale output already exists; choose a new filename: {upscale_output}")

    config = None
    payload = None
    reference = None
    if args.response_json is None:
        prompt = args.prompt_file.read_text(encoding="utf-8") if args.prompt_file else args.prompt
        if not prompt or not prompt.strip():
            raise ImageError("Prompt must not be empty.")
        config = select_deployment(load_config(), deployment)
        model = config["model"]
        if (args.auto_aspect_ratio or args.web_grounding) and not supports_26_options(model):
            raise ImageError("--auto-aspect-ratio and --web-grounding apply only to MAI-Image-2.6 models.")
        if aspect is not None:
            width, height = largest_size(parse_aspect(aspect), model)
        if not inputs:
            validate_dimensions(width, height, model)
            if upscale is not None and not args.auto_aspect_ratio:
                upscale_target(upscale, width, height)  # reject a bad spec before the billable request
        payload = {"model": config["deployment"], "prompt": prompt}
        if supports_26_options(model):
            payload.update(auto_aspect_ratio=args.auto_aspect_ratio, web_grounding=args.web_grounding)
        if not inputs:
            payload.update(width=width, height=height)
        else:
            loaded = [read_reference(path) for path in inputs]
            reference = loaded[0] if len(loaded) == 1 else loaded

    exact_size = config is not None and reference is None and not args.auto_aspect_ratio
    # Reserve writable temporary storage before making a billable request.
    with tempfile.NamedTemporaryFile(prefix=".mai-image-", dir=output.parent) as temporary:
        if args.response_json is not None:
            result = read_json(args.response_json)
        else:
            token = get_token(config)
            result = post_image(config, payload, token, args.timeout, args.retries, reference)
        # Edits and model-chosen framing may return other sizes (edits: about 1 MP at the input's aspect).
        image, actual_width, actual_height = decode_image(
            result, config["model"] if config is not None else "MAI-Image-2.6", strict=exact_size,
        )
        if exact_size and (actual_width, actual_height) != (width, height):
            raise ImageError("PNG dimensions do not match the requested dimensions.")
        temporary.write(image)
        temporary.flush()
        os.fsync(temporary.fileno())
        # An atomic hard link refuses to replace existing files, including symlinks.
        os.link(temporary.name, output)

    summary = {
        "image": str(output), "width": actual_width, "height": actual_height,
        "bytes": len(image), "sha256": hashlib.sha256(image).hexdigest(),
    }
    if config is not None:
        summary.update(deployment=config["deployment"], model=config["model"])
        summary["operation"] = "edit" if reference is not None else "generate"
        if reference is not None:
            hashes = [hashlib.sha256(image).hexdigest()
                      for image, _ in ([reference] if isinstance(reference, tuple) else reference)]
            summary["input_sha256"] = hashes[0] if len(hashes) == 1 else hashes
    else:
        summary["source"] = str(args.response_json)
    if upscale_output is not None:
        try:
            target_width, target_height = upscale_target(upscale, actual_width, actual_height)
            method = upscale_image(output, upscale_output, target_width, target_height)
        except (ImageError, OSError) as exc:
            raise ImageError(f"The image was saved to {output}, but upscaling failed: {exc}") from exc
        summary["upscaled"] = {
            "image": str(upscale_output), "width": target_width, "height": target_height, "method": method,
            "note": "Interpolated from the native image: no new detail; label as upscaled.",
        }
    return summary


def main() -> int:
    args = parser().parse_args()
    try:
        result = generate(args)
    except (ImageError, OSError, UnicodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
