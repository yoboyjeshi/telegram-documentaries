"""Validate actual image bytes, not just a filename or model MIME claim."""
from __future__ import annotations

import warnings
from io import BytesIO

from PIL import Image

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000


def image_suffix(data: bytes) -> str:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise RuntimeError("Invalid image size")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format not in ("PNG", "JPEG"):
                    raise RuntimeError("Unsupported image format")
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise RuntimeError("Image dimensions too large")
                suffix = ".png" if image.format == "PNG" else ".jpg"
                image.verify()
            with Image.open(BytesIO(data)) as image:
                image.load()
        return suffix
    except Exception:  # noqa: BLE001 - untrusted decoder errors become safe metadata
        raise RuntimeError("Invalid generated image") from None
