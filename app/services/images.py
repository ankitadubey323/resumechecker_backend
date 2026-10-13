"""Validate and normalize uploaded profile photos."""

import io

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_PHOTO_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 40_000_000  # reject decompression bombs before decoding
AVATAR_SIZE = (400, 400)
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


class PhotoError(ValueError):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


def process_avatar(data: bytes) -> bytes:
    """Return a square 400x400 WebP. Re-encoding drops EXIF (e.g. GPS) and any
    non-image payload hidden in the original file."""
    if not data:
        raise PhotoError("The file is empty.")
    if len(data) > MAX_PHOTO_BYTES:
        raise PhotoError("Photos must be 2 MB or smaller.", 413)

    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in ALLOWED_FORMATS:
            raise PhotoError("Upload a JPG, PNG or WebP image.", 415)
        if img.width * img.height > MAX_PIXELS:
            raise PhotoError("This image's dimensions are too large.")
        img.load()
    except PhotoError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError):
        raise PhotoError("Upload a JPG, PNG or WebP image.", 415)

    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.getchannel("A"))
        img = background
    else:
        img = img.convert("RGB")

    img = ImageOps.fit(img, AVATAR_SIZE, Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="WEBP", quality=85)
    return out.getvalue()
