"""Color contract for native photo pixels, shared by Windows and macOS.

A PNG sRGB marker alone is insufficient: the producer must have performed the
conversion. Unknown/old native bridges fail rather than relabel camera RGB.
Masks are coverage and deliberately do not use this photo color contract.
"""
from __future__ import annotations

import io
from PIL import Image, ImageCms

NATIVE_COLOR_POLICY = 'native-srgb-v1'
_SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
_CONVERSIONS = {'c1_output_profile', 'coregraphics_icc_to_srgb'}


def require_native_srgb(encoding):
    if (not isinstance(encoding, dict) or encoding.get('policy') != NATIVE_COLOR_POLICY
            or encoding.get('space') != 'srgb' or encoding.get('conversion') not in _CONVERSIONS):
        raise ValueError('Native photo color encoding is unverified; update and rebuild the native bridge')
    return dict(encoding)


def tagged_native_png(data, encoding):
    """Embed sRGB only after conversion is attested; preserve the pixel values."""
    require_native_srgb(encoding)
    with Image.open(io.BytesIO(data)) as image:
        if image.format != 'PNG':
            raise ValueError('Native photo preview must be PNG')
        if image.info.get('icc_profile'):
            return data
        image.load()
        output = io.BytesIO()
        image.save(output, format='PNG', compress_level=1, icc_profile=_SRGB_ICC)
        return output.getvalue()
