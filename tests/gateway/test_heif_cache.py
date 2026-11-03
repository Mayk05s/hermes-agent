"""Exercise real HEIF decoder and provider-compatible cache output."""
import io
from pathlib import Path
import pytest
from PIL import Image
from gateway.platforms.base import cache_image_from_bytes
from tools.image_formats import normalize_heif

@pytest.mark.parametrize('ext', ['.HEIC', '.heif', '.jpg'])
def test_heif_pixels_reach_jpeg_cache(tmp_path, monkeypatch, ext):
    heif = pytest.importorskip('pillow_heif')
    heif.register_heif_opener()
    original=Image.new('RGB',(64,32),(200,30,10))
    raw=io.BytesIO()
    original.save(raw,format='HEIF')
    monkeypatch.setattr('gateway.platforms.base.get_image_cache_dir',lambda:tmp_path)
    cached=Path(cache_image_from_bytes(raw.getvalue(),ext))
    assert cached.suffix=='.jpg'
    with Image.open(cached) as image:
        assert image.format=='JPEG'
        assert image.size==(64,32)
        assert image.getpixel((10,10))[0]>180

def test_bad_heif_is_not_accepted():
    with pytest.raises(ValueError,match='Cannot decode'):
        normalize_heif(b'not a photo','.heic')
