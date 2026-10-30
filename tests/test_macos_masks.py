"""One bounded parser test: constants, copy fingerprints and corrupt framing."""
import struct

import pytest

from c1ai.macos_masks import MaskReadError, read_mask_file


def _num(value):
    if value < 64:
        return bytes([value])
    parts = [value & 63]
    value >>= 6
    while value:
        parts.append(value & 63)
        value >>= 6
    parts.reverse()
    parts[0] |= 64
    parts[-1] |= 64
    return bytes(parts)


def _chunk(name, data=b"", children=(), *, flags=0, fmt=34):
    result = (_num(len(name)) + name.encode() + bytes([flags, fmt])
              + _num(len(data)) + data + b"".join(children) + b"\xfe")
    return result + _num(len(result))


def _file(identifier, raster, blocks=(), mask_type=0, fill=None, width=128, height=64, composition=None):
    fields = [
        _chunk("UserInfo", children=[_chunk("Info", identifier.encode()), _chunk("Name")]),
        _chunk("MaskType", struct.pack("<I", mask_type)),
        _chunk("CompParam", composition if composition is not None else struct.pack("<IfII", 1, 1., 0, 0))]
    if raster is not None:
        fields.append(_chunk("Map", raster, fmt=0))
    if fill is not None:
        fields.append(_chunk("ConstantFill", struct.pack("<f", fill)))
    mask = _chunk("MaskIndex", struct.pack("<I", 0), fields)
    info = _chunk("MaskInfo", children=[_chunk("Hdr", struct.pack("<6I", 1, width, height, 12, 0, 0)), mask])
    block = _chunk("Blk", children=[
        _chunk("Hdr", struct.pack("<4I", len(blocks), 0, 0, 0)),
        _chunk("Idx", b"".join(struct.pack("<H", len(b)) for b in blocks)),
        _chunk("dat", b"".join(blocks))])
    return _chunk("XCon", bytes.fromhex("bebafeca0002"), [
        _chunk("TYPE", b"CaptureOne Mask", flags=192, fmt=0),
        _chunk("VER", b"\x03\x00", fmt=0), info, block], flags=192, fmt=66)


def test_mask_saved_evidence_distinguishes_constant_copy_and_invalid_data(tmp_path):
    path = tmp_path / "synthetic.comask"
    first, second = "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"

    def read(data, identifier=first):
        path.write_bytes(data)
        return read_mask_file(path, identifier)

    full = read(_file(first, b"\x00\xff\x01"))
    assert full["constant"] == {"verified": True, "value_8bit": 255, "density": 1.0}
    assert read(_file(first, b"\x01\x01"))["constant"]["value_8bit"] == 0
    assert not read(_file(first, b"\x00\xff\x01", mask_type=1))["constant"]["verified"]
    assert read(_file(first, None))["constant"]["value_8bit"] == 0
    # Native CompParam is B + 3 padding bytes + f + i + I. A native AI
    # combination can leave nonzero bytes in that padding; flags are not padding.
    plain = read(_file(first, None))
    padded = read(_file(first, None, composition=b"\x01\xcd\xc9\x33" + struct.pack("<fiI", 1., 0, 0)))
    assert padded["content_sha256"] == plain["content_sha256"]
    assert padded["structure"]["composition_raw"] == [1, 1., 0, 0]
    assert padded["constant"] == plain["constant"]
    for composed in (struct.pack("<B3xfiI", 0, 1., 0, 0),
                     struct.pack("<B3xfiI", 1, 1., 5, 0),
                     struct.pack("<B3xfiI", 1, 1., 0, 256)):
        changed_composition = read(_file(first, None, composition=composed))
        assert changed_composition["content_sha256"] != plain["content_sha256"]
        assert not changed_composition["constant"]["verified"]
    with pytest.raises(MaskReadError):
        read(_file(first, None, composition=struct.pack("<B3xfiI", 2, 1., 0, 0)))
    assert read(_file(first, None, mask_type=1, fill=1))["constant"]["value_8bit"] == 255
    assert not read(_file(first, None, mask_type=1))["constant"]["verified"]
    assert not read(_file(first, b"\x01\x01", mask_type=1, fill=1))["constant"]["verified"]
    # Same bytes with different UUID and global block numbering are the same mask.
    source = read(_file(first, b"\x09\x00\x00\x01", [b"compressed tile"]))
    copied = read(_file(second, b"\x08\x01", [b"unrelated tile", b"compressed tile"]), second)
    assert source["content_sha256"] == copied["content_sha256"]
    assert not source["constant"]["verified"]
    changed = read(_file(first, b"\x09\x00\x00\x01", [b"changed tile"]))
    assert changed["content_sha256"] != source["content_sha256"]
    # Encoded lengths 254/255 are ordinary lengths, unlike raw FE/FF markers.
    for size in (254, 255):
        assert not read(_file(first, b"\x09\x00\x00\x01", [b"x" * size]))["constant"]["verified"]
    # Synthetic delta block: first pixel 255, 15 zero deltas, one +1,
    # then 4079 zero deltas. Prediction gives 16 white columns, 48 black.
    edge = bytes([2, 255, 14, 64, 240, 143, 238, 32])
    edge_map = bytes([0, 255, 9, 0, 0])
    white = read(_file(first, edge_map, [edge], width=80))
    assert white["constant"] == full["constant"]
    assert white["structure"]["map"]["decoded_boundary_tiles"] == 1
    assert not read(_file(first, edge_map, [edge], width=81))["constant"]["verified"]
    with pytest.raises(MaskReadError):
        read(_file(first, edge_map, [edge[:-1]], width=80))
    for invalid in (_file(first, b"\x08\x01"), _file(first, b"\x01\x01\x01"),
                    _file(first, b"\x01\x01")[:-1], _file(first, b"\x01\x01") + b"junk",
                    _file(first, None, mask_type=1, fill=float("nan"))):
        with pytest.raises(MaskReadError):
            read(invalid)


def test_parameter_invert_preserves_render_seed_and_rejects_unknown_fields():
    from copy import deepcopy
    from c1ai.contracts import Refused
    from c1ai.macos_mask_actions import parameter_invert_kind, verify_parameter_invert

    layer = {"native_id": "test-gradient", "is_image_layer": False,
             "identity_source": "session_mask_uuid_readonly"}
    root = {"mask_type": 3, "composition_raw": [1, 1.0, 0, 0],
            "linear_gradient_raw": [10., 20., 30., 40., .25],
            "content_sha256": "before",
            "other_fields": [{"name": "RandSeed", "flags": 0, "format": 34,
                              "sha256": "seed-before", "children": []}]}
    before = {"layers": [layer], "details": [{}], "selected_layer": layer,
              "masks": [{"uuid": "test-gradient", "source": "session_comask_readonly",
                         "format_version": 3, "mask_type": 3,
                         "content_sha256": "before", "structure": root}]}
    after = deepcopy(before)
    changed = after["masks"][0]
    changed["content_sha256"] = changed["structure"]["content_sha256"] = "after"
    changed["structure"]["linear_gradient_raw"] = [30., 40., 10., 20., .75]
    assert verify_parameter_invert(before, after, layer)["parameter_transform_verified"]
    changed["structure"]["other_fields"][0]["sha256"] = "seed-after"
    with pytest.raises(Refused, match="native inversion transform"):
        verify_parameter_invert(before, after, layer)
    root["other_fields"][0]["name"] = "UnknownFeature"
    with pytest.raises(Refused, match="Unknown parameter-mask fields"):
        parameter_invert_kind(before, layer)
    root["other_fields"][0]["name"] = "RandSeed"
    root["composition_raw"] = [1, 1., 5, 0]
    with pytest.raises(Refused, match="neutral root composition"):
        parameter_invert_kind(before, layer)


def test_existing_gradient_updates_keep_identity_and_other_adjustments():
    from copy import deepcopy
    from c1ai.contracts import Refused
    from c1ai.macos_mask_actions import native_gradient_parameters, verify_native_gradient
    layer = {"native_id": "gradient", "is_image_layer": False,
             "identity_source": "session_mask_uuid_readonly"}
    root = {"mask_type": 3, "composition_raw": [1, 1., 0, 0],
            "metadata": {"SelectedSubmaskIndex": -1},
            "linear_gradient_raw": [100., 100., 800., 400., .5], "content_sha256": "before"}
    before = {"layers": [layer], "details": [{"kind": "adjustment", "exposure": 1}], "selected_layer": layer,
              "masks": [{"uuid": "gradient", "source": "session_comask_readonly", "format_version": 3,
                         "width": 1000, "height": 500, "mask_type": 3,
                         "content_sha256": "before", "structure": root}]}
    patch = {"action": "linear_gradient", "start": [.2, .3], "stop": [.7, .8], "midpoint": .6}
    parameters = native_gradient_parameters(before, layer, patch)
    after = deepcopy(before)
    after["masks"][0].update(content_sha256="after")
    after["masks"][0]["structure"].update(linear_gradient_raw=parameters, content_sha256="after")
    assert verify_native_gradient(before, after, layer, patch, parameters)["native_geometry_readback"]
    after["details"][0]["exposure"] = 2
    with pytest.raises(Refused):
        verify_native_gradient(before, after, layer, patch, parameters)
    with pytest.raises(Refused):
        native_gradient_parameters(before, layer, {"action": "radial_gradient", "center": [.5, .5],
            "radius_x": .2, "radius_y": .2, "angle": 0, "falloff": .5, "inverted": False})


def test_combine_existing_preserves_source_and_siblings_across_both_steps():
    from copy import deepcopy
    from c1ai.contracts import Refused
    from c1ai.macos_mask_actions import (
        preflight_combine_existing, verify_append_blank, verify_combine_existing,
    )

    def ref(uid):
        return {"native_id": uid, "name": uid, "is_image_layer": False,
                "identity_source": "session_mask_uuid_readonly"}

    parent, source_ref = ref("synthetic-parent"), ref("synthetic-source")
    original = {"mask_type": 3, "composition_raw": [1, 1., 0, 0],
                "metadata": {"SubmaskName": "Linear 1", "SelectedSubmaskIndex": -1},
                "linear_gradient_raw": [10., 20., 30., 40., .5],
                "content_sha256": "original-content"}
    source = {"mask_type": 0, "composition_raw": [1, 1., 0, 0],
              "metadata": {"MaskGenerator": "AISubject", "SelectedSubmaskIndex": -1},
              "map": {"sha256": "source-raster"},
              "other_fields": [{"name": "RandSeed", "flags": 0, "format": 34,
                                "sha256": "source-seed", "children": []}],
              "content_sha256": "source-content"}

    def state(root):
        return {"layers": [parent, source_ref], "selected_layer": parent,
                "details": [{"name": item["name"]} for item in (parent, source_ref)],
                "masks": [{"uuid": item["native_id"], "width": 64, "height": 64,
                           "source": "session_comask_readonly", "format_version": 3,
                           "mask_type": node["mask_type"], "structure": deepcopy(node),
                           "content_sha256": node["content_sha256"]}
                          for item, node in ((parent, root), (source_ref, source))]}

    before = state(original)
    sibling = deepcopy(original)
    # Native wrapping may relabel the existing child without changing content.
    sibling["metadata"] = {"SubmaskName": "Linear 2"}
    blank = {"mask_type": 0, "composition_raw": [1, 1., 5, 0],
             "metadata": {"SubmaskName": "Brush 1"}, "content_sha256": "blank-content"}
    middle = state({"mask_type": 255, "composition_raw": [1, 1., 0, 0],
                    "metadata": {"SelectedSubmaskIndex": 1},
                    "children": [sibling, blank], "content_sha256": "middle-content"})
    after = deepcopy(middle)
    copied = deepcopy(source)
    copied["metadata"]["SubmaskName"] = "Brush 1"
    copied["composition_raw"] = [1, 1., 5, 0]
    copied["content_sha256"] = "copied-content"
    after["masks"][0]["structure"]["children"][1] = copied
    after["masks"][0]["content_sha256"] = "after-content"
    after["masks"][0]["structure"]["content_sha256"] = "after-content"

    assert preflight_combine_existing(before, parent, source_ref, "subtract") == 1
    assert verify_append_blank(before, middle, parent, "subtract")["appended_index"] == 1
    result = verify_combine_existing(before, middle, after, parent, source_ref, "subtract")
    assert result["source_metadata_verified"] and result["source_structure_verified"]
    assert not result["pixels_verified"]

    # Independently corrupt the source copy, old sibling, or temporary Brush.
    for stage, path, value in (
        (2, (0, "structure", "children", 1, "other_fields", 0, "sha256"), "different-seed"),
        (2, (0, "structure", "children", 1, "metadata", "MaskGenerator"), "AIBackground"),
        (2, (0, "structure", "children", 1, "composition_raw", 2), 0),
        (2, (0, "structure", "children", 0, "metadata", "SubmaskName"), "unexpected-rename"),
        (2, (1, "structure", "map", "sha256"), "changed-non-target"),
        (1, (0, "structure", "children", 1, "map"), {"sha256": "not-empty"}),
    ):
        observations = deepcopy([before, middle, after])
        container = observations[stage]["masks"]
        for key in path[:-1]:
            container = container[key]
        container[path[-1]] = value
        with pytest.raises(Refused):
            verify_combine_existing(*observations, parent, source_ref, "subtract")


def test_viewer_gradient_requires_empty_local_scope_and_binds_view_geometry():
    from copy import deepcopy
    from c1ai.contracts import Refused
    from c1ai.journal import Journal
    from c1ai.macos_mask_actions import preflight_viewer_gradient, verify_viewer_gradient

    layer = {"native_id": "synthetic-empty", "is_image_layer": False,
             "identity_source": "session_mask_uuid_readonly"}
    other = {**layer, "native_id": "synthetic-other"}
    empty = {"mask_type": 0, "composition_raw": [1, 1., 0, 0],
             "metadata": {}, "content_sha256": "empty-content"}
    before = {"layers": [layer, other], "selected_layer": layer,
              "details": [{"kind": "adjustment", "values": {"exposure": 0}} for _ in range(2)],
              "masks": [{"uuid": ref["native_id"], "width": 128, "height": 64,
                         "format_version": 3, "source": "session_comask_readonly",
                         "mask_type": 0, "structure": deepcopy(empty), "content_sha256": "empty-content",
                         "constant": {"verified": True, "value_8bit": 0, "density": 0}}
                        for ref in (layer, other)]}
    patch = {"action": "linear_gradient", "coordinate_space": "viewer",
             "start": [.25, .25], "stop": [.75, .75], "midpoint": .5}
    after = deepcopy(before)
    after["masks"][0].update(mask_type=3, content_sha256="gradient-content",
        constant={"verified": False}, structure={
            "mask_type": 3, "composition_raw": [1, 1., 0, 0], "metadata": {"SelectedSubmaskIndex": -1},
            "linear_gradient_raw": [94., 46., 34., 18., .5], "content_sha256": "gradient-content",
            "other_fields": [{"name": "RandSeed", "flags": 0, "format": 34,
                              "sha256": "a" * 64, "children": []}]})
    assert preflight_viewer_gradient(before, layer, patch) == patch
    evidence = verify_viewer_gradient(before, after, layer, patch)
    assert evidence["native_geometry_readback"] and evidence["requested_viewer"] == patch
    assert evidence["actual_raw"]["linear_gradient_raw"] == [94., 46., 34., 18., .5]
    assert not evidence["raw_points_matched_to_request"] and not evidence["viewer_geometry_match_verified"]

    for failure in ("existing", "scope", "midpoint", "other_layer", "parameters", "extra"):
        old, new, request = deepcopy((before, after, patch))
        if failure == "existing":
            old["masks"][0] = deepcopy(after["masks"][0])
        elif failure == "scope":
            request["coordinate_space"] = "raw"
        elif failure == "midpoint":
            request["midpoint"] = .25
        elif failure == "other_layer":
            new["masks"][1]["content_sha256"] = "changed-other"
            new["masks"][1]["structure"]["content_sha256"] = "changed-other"
        elif failure == "parameters":
            new["details"][0]["values"]["exposure"] = 1
        else:
            new["masks"][0]["structure"]["unexpected_feature"] = True
        with pytest.raises(Refused):
            verify_viewer_gradient(old, new, layer, request)

    observation = {"target": {"instance": {"platform": "macos"}}, "module": "masks",
                   "values": {"masks": before}, "gradient_view": {
                       "photo_rect": {"x": 100, "y": 200, "width": 640, "height": 320}, "zoom_value": 0}}
    moved = deepcopy(observation)
    moved["gradient_view"]["photo_rect"]["x"] += 10
    assert Journal._observation_identity(observation) != Journal._observation_identity(moved)
