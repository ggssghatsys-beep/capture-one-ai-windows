"""Color data and native-source contracts; no fake claim of a live C1 renderer."""
import hashlib
import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageCms

from c1ai.render_color import NATIVE_COLOR_POLICY, require_native_srgb, tagged_native_png
from c1ai.image_analysis import decode
from c1ai.analysis_service import AnalysisService
from c1ai.model_preview import preview
from c1ai.windows_backend import NativeError


def encoding(conversion='c1_output_profile'):
    return {'policy': NATIVE_COLOR_POLICY, 'space': 'srgb', 'conversion': conversion}


@pytest.mark.parametrize('receipt', [None, {}, {'space': 'srgb'},
    {'policy': 'old-camera-rgb', 'space': 'srgb', 'conversion': 'c1_output_profile'},
    {'policy': NATIVE_COLOR_POLICY, 'space': 'display_p3', 'conversion': 'c1_output_profile'},
    {'policy': NATIVE_COLOR_POLICY, 'space': 'srgb', 'conversion': 'assumed'}])
def test_old_or_unverified_native_colors_are_refused(receipt):
    with pytest.raises(ValueError, match='update and rebuild'):
        require_native_srgb(receipt)


@pytest.mark.parametrize('conversion', ['c1_output_profile', 'coregraphics_icc_to_srgb'])
def test_verified_png_tagging_preserves_every_channel(conversion):
    values = np.array([[[0, 1, 12, 255], [63, 128, 200, 255], [254, 255, 9, 190]]], dtype=np.uint8)
    source = io.BytesIO()
    Image.fromarray(values).save(source, format='PNG')
    tagged = tagged_native_png(source.getvalue(), encoding(conversion))
    with Image.open(io.BytesIO(tagged)) as image:
        assert np.array_equal(np.array(image), values)
        profile = ImageCms.ImageCmsProfile(io.BytesIO(image.info['icc_profile']))
        assert 'srgb' in ImageCms.getProfileDescription(profile).lower()
    assert tagged_native_png(tagged, encoding(conversion)) == tagged


def test_verified_raw_srgb_is_converted_once_for_wide_gamut_scopes():
    raw = {'width': 1, 'height': 1, 'stride': 4, 'format': 'BGRA8',
           'bit_depth': 8, 'row_order': 'top-down'}
    red = bytes([0, 0, 255, 255])
    original = decode(red, raw=raw, native_color=encoding())
    assert original.rgb[0, 0].tolist() == [255, 0, 0]
    assert original.metadata['color']['confirmation'] == 'verified_native_srgb_output'
    p3 = decode(red, 'display_p3', raw=raw, native_color=encoding())
    assert np.allclose(p3.rgb[0, 0], [234, 51, 35], atol=2)


class RenderReceipt:
    def __init__(self, root, color):
        self.state_dir = root
        folder = root / 'native-previews'
        folder.mkdir(parents=True)
        output = io.BytesIO()
        Image.new('RGB', (2, 2), (71, 128, 203)).save(output, format='PNG')
        data = output.getvalue()
        sha = hashlib.sha256(data).hexdigest()
        path = folder / (sha + '.png')
        path.write_bytes(data)
        self.artifact = {'path': str(path), 'sha256': sha, 'width': 2, 'height': 2,
                         'format': 'PNG', 'bytes': len(data)}
        if color is not None:
            self.artifact['color_encoding'] = color

    def _model_call(self, request, observation):
        return {'phase': 'ready', 'preview_id': 'receipt', 'artifact': self.artifact,
                'revision': '1:2:3', 'geometry': {}}


def test_windows_transport_refuses_old_bridge_and_tags_new_png(tmp_path):
    old = RenderReceipt(tmp_path / 'old', None)
    with pytest.raises(NativeError, match='update and rebuild'):
        preview(old, {}, {'target': {}})
    new = RenderReceipt(tmp_path / 'new', encoding())
    result = preview(new, {}, {'target': {}})
    data = Path(result['path']).read_bytes()
    assert result['image']['sha256'] == hashlib.sha256(data).hexdigest()
    assert result['color_encoding'] == encoding()
    with Image.open(io.BytesIO(data)) as image:
        assert image.getpixel((0, 0)) == (71, 128, 203)
        assert image.info['icc_profile']


@pytest.mark.parametrize('verified', [False, True])
def test_scopes_require_the_same_confirmed_photo_encoding(tmp_path, verified):
    path = tmp_path / 'pixels.bgra'
    data = bytes([0, 0, 255, 255]) * 4
    path.write_bytes(data)

    class Backend:
        def observe(self, fields):
            return {'target': {'document': {'path': 'session'}, 'image': {'path': 'photo'},
                               'variant': {'id': 'variant'}, 'layer': 'Background'},
                    'native_context': {'process_revision': '1', 'target': {'document_uuid': 'document'},
                                       'image_path': 'photo'}}

        def preview(self, request, observation):
            artifact = {'sha256': hashlib.sha256(data).hexdigest(), 'width': 2, 'height': 2,
                        'stride': 8, 'format': 'BGRA8', 'bit_depth': 8, 'row_order': 'top-down'}
            return {'status': 'completed', 'revision': '1', 'path': str(path), 'image': artifact,
                    'preview_id': 'render', 'geometry': {},
                    **({'color_encoding': encoding()} if verified else {})}

    service = AnalysisService(Backend(), tmp_path / 'analysis')
    result = service.dispatch({'source': 'native'}, lambda a, b: None)
    if verified:
        assert result['freshness']['state'] == 'ready'
        assert result['analysis']['color']['confirmation'] == 'verified_native_srgb_output'
        assert result['analysis']['region']['statistics']['luma']['mean_percent'] == pytest.approx(21.26)
    else:
        assert result['freshness']['state'] == 'failed'
        assert 'analysis' not in result


def test_mac_delivery_refuses_old_bridge_before_reading_pixels(tmp_path):
    pytest.importorskip('fcntl', reason='Mac delivery uses the POSIX file channel')
    from c1ai.contracts import Refused
    from c1ai.macos_bridge import NativeBridge
    bridge = NativeBridge(tmp_path)
    with pytest.raises(Refused) as failure:
        bridge._deliver_artifact({'artifact': {'color_profile': 'sRGB'}}, {}, {}, 'native', True, 0)
    assert failure.value.code == 'native_color_unverified'
