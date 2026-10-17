"""Small real images; sentinel strings are not valid image fixtures."""
from io import BytesIO

from PIL import Image


def image_bytes(fmt='PNG', color='red'):
    out = BytesIO()
    Image.new('RGB', (2, 2), color).save(out, format=fmt)
    return out.getvalue()


PNG = image_bytes()
JPEG = image_bytes('JPEG')
