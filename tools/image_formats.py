"""Normalize HEIF originals to provider-compatible, orientation-correct JPEG."""
import io

_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}

def normalize_heif(data: bytes, ext: str) -> tuple[bytes, str]:
    is_heif = len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS
    if ext.lower() not in {".heic", ".heif"} and not is_heif:
        return data, ext
    try:
        from pillow_heif import register_heif_opener
        from PIL import Image, ImageOps
        register_heif_opener()
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=95)
        return output.getvalue(), ".jpg"
    except Exception as exc:
        raise ValueError("Cannot decode HEIC/HEIF image") from exc
