"""Read-only mask evidence for the locally checked Capture One 16.8.6 format.

This is an internal sidecar reader, not an official mask API. It never loads
vendor code or writes a sidecar. XCon framing and raster tile references are
checked before evidence is returned. Compressed tiles are normally only hashed.
One checked delta-codec subset decodes boundary tiles to exclude image padding.
Constants are recognized only from raster tiles, a missing default raster, or
the native ConstantFill endpoint representation; composites remain unverified.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import struct
import uuid

from .macos_mask_codec import MaskCodecError, decode_tile


class MaskReadError(ValueError):
    """Unsupported, incomplete, ambiguous, or concurrently changing evidence."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(value) -> str:
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":"),
                           allow_nan=False).encode())


@dataclass
class _Chunk:
    name: str
    flags: int
    format: int
    data: bytes
    children: list["_Chunk"]


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos, self.count = data, 0, 0

    def take(self, length: int) -> bytes:
        if length < 0 or self.pos + length > len(self.data):
            raise MaskReadError("Truncated mask container")
        result = self.data[self.pos:self.pos + length]
        self.pos += length
        return result

    def number(self) -> int:
        first = self.take(1)[0]
        if first < 64:
            return first
        if first >= 128:
            raise MaskReadError("Unexpected mask container marker")
        value = first & 63
        for _ in range(9):
            part = self.take(1)[0]
            if part >= 128:
                raise MaskReadError("Invalid mask container integer")
            value = (value << 6) | (part & 63)
            if part >= 64:
                return value
        raise MaskReadError("Oversized mask container integer")

    def chunk(self, depth=0) -> _Chunk:
        self.count += 1
        if depth > 12 or self.count > 100000:
            raise MaskReadError("Mask container nesting or count exceeds limit")
        start = self.pos
        length = self.number()
        if not 0 < length <= 64:
            raise MaskReadError("Unsupported mask chunk name")
        try:
            name = self.take(length).decode("ascii")
        except UnicodeDecodeError as exc:
            raise MaskReadError("Invalid mask chunk name") from exc
        flags, fmt = self.take(2)
        # Deferred references need a different resolver; do not hash missing data.
        if flags & 1:
            raise MaskReadError("Deferred mask chunks are not supported")
        while self.pos < len(self.data) and self.data[self.pos] == 255:
            self.pos += 1
        length = self.number()
        data = self.take(length)
        children = []
        while True:
            marker = self.take(1)[0]
            if marker == 255:  # Native writer alignment padding.
                continue
            if marker == 254:
                size = self.pos - start
                if self.number() != size:
                    raise MaskReadError("Mask chunk length check failed")
                return _Chunk(name, flags, fmt, data, children)
            self.pos -= 1
            children.append(self.chunk(depth + 1))


def _one(node: _Chunk, name: str) -> _Chunk:
    matches = [c for c in node.children if c.name == name]
    if len(matches) != 1:
        raise MaskReadError("Missing or ambiguous " + name + " chunk")
    return matches[0]


def _unpack(fmt: str, data: bytes):
    if len(data) != struct.calcsize(fmt):
        raise MaskReadError("Unexpected mask field length")
    return struct.unpack(fmt, data)


def _uuid(node: _Chunk) -> str:
    try:
        return str(uuid.UUID(_one(_one(node, "UserInfo"), "Info").data.decode("ascii"))).upper()
    except (ValueError, UnicodeDecodeError) as exc:
        raise MaskReadError("Invalid mask UUID") from exc


def _boundary_delta(block: bytes) -> bytes | None:
    """Decode only native codec 2's zero/one-bit delta runs, into 64x64 bytes.

    Other runs/codecs remain unsupported. This small subset handles the partial
    edge tiles produced by inverting an empty raster. It is not a general mask
    codec. The framing, bit-plane ordering, 4x4 scan and cumulative prediction
    follow the locally checked Decompress64x64Delta reference path.
    """
    if not block or block[0] != 2:
        return None
    reader = _Reader(block[1:])
    first = reader.take(1)[0]
    runs, total, bit_count = [], 0, 4
    while total < 4095:
        token = reader.take(1)[0]
        if token < 64:
            kind, length = 0, token + 1
        elif token < 80:
            kind, length = 1, (token & 15) + 1
        elif token in (240, 241):
            kind, part = token & 15, reader.take(1)[0]
            if part & 128:
                if part & 64:
                    return None
                length = ((part & 63) << 8) + reader.take(1)[0] + 1
            else:
                length = part + 1
        else:
            return None
        total += length
        if total > 4095:
            raise MaskReadError("Delta tile run exceeds 64x64 pixels")
        bit_count += length + 1 if kind else 0
        runs.append((kind, length))
    bits = []
    # Native UnpackBits_REF writes one plane at a time for 16/4-byte groups.
    for group in (16, 4):
        while bit_count >= group * 8:
            packed = reader.take(group)
            bits.extend((value >> shift) & 1 for shift in range(8) for value in packed)
            bit_count -= group * 8
    while bit_count:
        value, length = reader.take(1)[0], min(8, bit_count)
        bits.extend((value >> shift) & 1 for shift in range(length))
        bit_count -= length
    if reader.pos != len(reader.data):
        raise MaskReadError("Unconsumed delta tile data")
    if bits[2] or bits[3]:
        return None
    pixels, position = [first], 4
    for kind, length in runs:
        if kind:
            base = bits[position]
            pixels.extend((value - base) & 255 for value in bits[position + 1:position + length + 1])
            position += length + 1
        else:
            pixels.extend([0] * length)
    if len(pixels) != 4096 or position != len(bits):
        raise MaskReadError("Invalid decoded delta tile size")
    if bits[1]:
        source = pixels[:]
        order = (0, 1, 14, 15, 3, 2, 13, 12, 4, 7, 8, 11, 5, 6, 9, 10)
        for row in range(16):
            for column in range(16):
                start = (row * 16 + column) * 16
                for index, offset in enumerate(order):
                    pixels[(row * 4 + index // 4) * 64 + column * 4 + index % 4] = source[start + offset]
    for column in range(1, 64):
        pixels[column] = (pixels[column] + pixels[column - 1]) & 255
    for index in range(64, 4096):
        pixels[index] = (pixels[index] + pixels[index - 64]) & 255
    if bits[0]:
        pixels = [pixels[column * 64 + row] for row in range(64) for column in range(64)]
    return bytes(pixels)


def _map(data: bytes, width: int, height: int, blocks: list[bytes]):
    reader = _Reader(data)
    previous_block, density = 0, 0
    counts: Counter = Counter()
    referenced = set()
    digest = hashlib.sha256()
    values, unknown, decoded_count, decoded_cache = set(), False, 0, {}
    columns = (width + 63) // 64
    # Native maps enumerate 64-pixel tiles in image row order.
    for tile in range(columns * ((height + 63) // 64)):
        token = reader.take(1)[0]
        if token == 0:
            density = reader.take(1)[0]
        elif token in (8, 9, 10, 11):
            index = previous_block + 1 if token == 8 else int.from_bytes(reader.take(token - 7), "big")
            if index >= len(blocks):
                raise MaskReadError("Mask references a missing raster block")
            previous_block = index
            referenced.add(index)
            counts["compressed"] += 1
            digest.update(b"B" + hashlib.sha256(blocks[index]).digest())
            valid_width = min(64, width - (tile % columns) * 64)
            valid_height = min(64, height - (tile // columns) * 64)
            decoded = None
            if valid_width < 64 or valid_height < 64:
                encoded = blocks[index]
                if encoded not in decoded_cache:
                    decoded_cache[encoded] = _boundary_delta(encoded)
                decoded = decoded_cache[encoded]
            if decoded is None:
                unknown = True
            else:
                decoded_count += 1
                for row in range(valid_height):
                    values.update(decoded[row * 64:row * 64 + valid_width])
            continue
        elif token != 1:
            raise MaskReadError("Unsupported raster tile token")
        counts[str(density)] += 1
        values.add(density)
        digest.update(b"C" + bytes([density]))
    if reader.pos != len(data):
        raise MaskReadError("Unconsumed raster tile data")
    value = next(iter(values)) if len(values) == 1 and not unknown else None
    return {"sha256": digest.hexdigest(), "tile_counts": dict(counts),
            "referenced_blocks": len(referenced),
            "referenced_blocks_sha256": _digest(sorted({_sha(blocks[i]) for i in referenced})),
            "decoded_boundary_tiles": decoded_count,
            "constant_value_8bit": value}


def _structure(node: _Chunk, width: int, height: int, blocks: list[bytes]):
    result = {"mask_type": _unpack("<I", _one(node, "MaskType").data)[0]}
    # Installed ImageProcessing ObjC metadata declares {?=BfiI}: one-byte
    # bool, three ABI padding bytes, float, signed blend mode, unsigned flags.
    # ImageCore writeDrawBufList (0xf1c958..0xf1c990) persists all 16 bytes,
    # including uninitialized padding. Only offsets 1..3 are non-semantic;
    # the final uint32 is a real field and must never be truncated to a bool.
    composition = _unpack("<B3xfiI", _one(node, "CompParam").data)
    if composition[0] not in (0, 1) or not math.isfinite(composition[1]):
        raise MaskReadError("Invalid mask composition parameter")
    # Keep the existing public field shape, with canonical native field values.
    # Raw framing/integrity is still covered by the full file SHA-256.
    result["composition_raw"] = list(composition)
    try:
        metadata = json.loads(_one(_one(node, "UserInfo"), "Name").data or b"{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise MaskReadError("Invalid mask metadata") from exc
    if not isinstance(metadata, dict):
        raise MaskReadError("Invalid mask metadata object")
    # Selection UI state and labels do not change mask content or copy equality.
    result["metadata"] = metadata
    semantic = {"mask_type": result["mask_type"], "composition_raw": list(composition)}
    extras = []
    for child in node.children:
        if child.name in ("UserInfo", "MaskType", "CompParam"):
            continue
        if child.name == "Map":
            if child.children or "map" in result:
                raise MaskReadError("Ambiguous raster map")
            result["map"] = _map(child.data, width, height, blocks)
            semantic["map"] = result["map"]["sha256"]
        elif child.name == "CompositeMask":
            count = _unpack("<I", child.data)[0]
            if "children" in result or count != len(child.children) or not 0 < count <= 64:
                raise MaskReadError("Invalid composite mask count")
            if any(c.name != "MaskIndex" for c in child.children):
                raise MaskReadError("Unsupported composite child")
            if [_unpack("<I", c.data)[0] for c in child.children] != list(range(count)):
                raise MaskReadError("Invalid composite child order")
            pairs = [_structure(c, width, height, blocks) for c in child.children]
            result["children"] = [p[0] for p in pairs]
            semantic["children"] = [p[1] for p in pairs]
        elif child.name == "LumaRange":
            if "luma_range" in result or child.children:
                raise MaskReadError("Ambiguous Luma Range")
            values = _unpack("<6fI", child.data)
            if not all(math.isfinite(v) for v in values) or values[-1] not in (0, 1):
                raise MaskReadError("Invalid Luma Range values")
            result["luma_range"] = dict(zip(("range_low", "range_high", "falloff_low",
                                                "falloff_high", "radius", "sensitivity", "invert"), values))
            semantic["luma_range"] = result["luma_range"]
        elif child.name == "ConstantFill":
            if "constant_fill" in result or child.children:
                raise MaskReadError("Ambiguous ConstantFill")
            value = _unpack("<f", child.data)[0]
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise MaskReadError("Invalid ConstantFill density")
            result["constant_fill"] = semantic["constant_fill"] = value
        elif child.name == "LinearGradient":
            if "linear_gradient_raw" in result or child.children or result["mask_type"] != 3:
                raise MaskReadError("Ambiguous or mismatched LinearGradient")
            values = _unpack("<5f", child.data)
            if not all(math.isfinite(value) for value in values):
                raise MaskReadError("Non-finite LinearGradient parameters")
            # Native IC_LinearGradientParameters: two IC_POINTF and a float.
            # Keep vendor coordinates; no unverified Viewer/raw normalization.
            result["linear_gradient_raw"] = semantic["linear_gradient_raw"] = list(values)
        elif child.name == "RadialGradient":
            if "radial_gradient_raw" in result or child.children or result["mask_type"] != 4:
                raise MaskReadError("Ambiguous or mismatched RadialGradient")
            values = _unpack("<7fi", child.data)
            if not all(math.isfinite(value) for value in values[:-1]) or values[-1] not in (0, 1):
                raise MaskReadError("Invalid RadialGradient parameters")
            # Native IC_RadialGradientParameters: three IC_POINTF, float, int.
            result["radial_gradient_raw"] = semantic["radial_gradient_raw"] = list(values)
        else:
            # Unknown mask features remain inspectable, but are never pixel verified.
            def opaque(c):
                return {"name": c.name, "flags": c.flags, "format": c.format,
                        "sha256": _sha(c.data), "children": [opaque(s) for s in c.children]}
            extras.append(opaque(child))
    if extras:
        result["other_fields"] = semantic["other_fields"] = extras
    result["content_sha256"] = _digest({"width": width, "height": height, "mask": semantic})
    return result, semantic


def _raster_evidence(data: bytes | None, width: int, height: int, blocks: list[bytes]) -> dict:
    """Hash valid tile pixels only, excluding the image-edge storage padding."""
    if width * height > 100_000_000:
        raise MaskReadError("Raster decoding exceeds the bounded pixel limit")
    reader = _Reader(data) if data is not None else None
    pixels, complement = hashlib.sha256(), hashlib.sha256()
    inverse = bytes(range(255, -1, -1))
    previous_block, density, pixel_count = 0, 0, 0
    columns = (width + 63) // 64
    # Retain only the most recently used block; large masks need bounded memory.
    cached_index, cached_tile = None, None
    for tile in range(columns * ((height + 63) // 64)):
        token = reader.take(1)[0] if reader is not None else 1
        decoded = None
        if token == 0:
            density = reader.take(1)[0]
        elif token in (8, 9, 10, 11):
            index = previous_block + 1 if token == 8 else int.from_bytes(reader.take(token - 7), "big")
            if index >= len(blocks):
                raise MaskReadError("Mask references a missing raster block")
            previous_block = index
            if cached_index != index:
                try:
                    cached_tile = decode_tile(blocks[index])
                except MaskCodecError as exc:
                    raise MaskReadError("Raster tile codec could not be verified") from exc
                cached_index = index
            decoded = cached_tile
        elif token != 1:
            raise MaskReadError("Unsupported raster tile token")
        valid_width = min(64, width - tile % columns * 64)
        valid_height = min(64, height - tile // columns * 64)
        valid = (bytes([density]) * (valid_width * valid_height) if decoded is None
                 else b"".join(decoded[row * 64:row * 64 + valid_width] for row in range(valid_height)))
        pixels.update(valid)
        complement.update(valid.translate(inverse))
        pixel_count += len(valid)
    if (reader is not None and reader.pos != len(data)) or pixel_count != width * height:
        raise MaskReadError("Raster pixel coverage is incomplete")
    return {"pixels_sha256": pixels.hexdigest(), "complement_sha256": complement.hexdigest(),
            "pixel_count": pixel_count, "width": width, "height": height,
            "order": "64x64_tiles_row_order_valid_rows", "decoder": "c1_16.8.6_codec_0_2",
            "decoded": True}


def read_mask_file(path: str | Path, mask_uuid: str, *, decode_raster: bool = False) -> dict:
    """Return saved per-UUID evidence; caller must save and bind the live layer.

    `content_sha256` excludes UUIDs, labels, selected-submask state and global
    block numbers. It includes the actual referenced compressed block bytes.
    Equal digests support copy readback, not proof of current screen rendering.
    Optional raster evidence decodes only a neutral, simple type-0 raster;
    unknown codecs or features raise rather than return partial pixel evidence.
    """
    try:
        identifier = str(uuid.UUID(mask_uuid)).upper()
        with Path(path).open("rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > 256 * 1024 * 1024:
                raise MaskReadError("Mask file is not regular or exceeds limit")
            data = source.read(256 * 1024 * 1024 + 1)
            after = os.fstat(source.fileno())
        current = Path(path).stat()
        stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if stamp(before) != stamp(after) or stamp(after) != stamp(current) or len(data) != before.st_size:
            raise MaskReadError("Mask file changed while reading")
        reader = _Reader(data)
        root = reader.chunk()
        if (reader.pos != len(data) or root.name != "XCon" or root.flags != 192 or root.format != 66
                or root.data != bytes.fromhex("bebafeca0002")
                or _one(root, "TYPE").data != b"CaptureOne Mask" or _one(root, "VER").data != b"\x03\x00"):
            raise MaskReadError("Unsupported mask container version")
        if sorted(c.name for c in root.children) != ["Blk", "MaskInfo", "TYPE", "VER"]:
            raise MaskReadError("Unsupported mask container sections")
        info, block = _one(root, "MaskInfo"), _one(root, "Blk")
        count, width, height, pixel_format, reserved1, reserved2 = _unpack("<6I", _one(info, "Hdr").data)
        if (not 0 < width <= 65536 or not 0 < height <= 65536 or pixel_format != 12
                or reserved1 or reserved2 or ((width + 63) // 64) * ((height + 63) // 64) > 1000000):
            raise MaskReadError("Unsupported mask image geometry")
        masks = [c for c in info.children if c.name == "MaskIndex"]
        if len(info.children) != count + 1 or len(masks) != count or count > 4096:
            raise MaskReadError("Invalid mask index count")
        if [_unpack("<I", m.data)[0] for m in masks] != list(range(count)):
            raise MaskReadError("Invalid mask index order")
        selected = [m for m in masks if _uuid(m) == identifier]
        if len(selected) != 1:
            raise MaskReadError("Mask UUID is missing or ambiguous")
        block_count, r1, r2, r3 = _unpack("<4I", _one(block, "Hdr").data)
        index, payload = _one(block, "Idx").data, _one(block, "dat").data
        if r1 or r2 or r3 or len(index) != block_count * 2 or len(block.children) != 3:
            raise MaskReadError("Unsupported raster block index")
        sizes = struct.unpack("<" + "H" * block_count, index)
        if sum(sizes) != len(payload) or any(size == 0 for size in sizes):
            raise MaskReadError("Raster block lengths do not match payload")
        blocks, offset = [], 0
        for size in sizes:
            blocks.append(payload[offset:offset + size])
            offset += size
        structure, _ = _structure(selected[0], width, height, blocks)
        value, constant_source = None, None
        neutral = (structure["composition_raw"] == [1, 1.0, 0, 0]
                   and not any(k in structure for k in ("children", "luma_range", "other_fields")))
        if neutral and structure["mask_type"] == 0 and "constant_fill" not in structure:
            if "map" in structure:
                value = structure["map"]["constant_value_8bit"]
                constant_source = ("raster_tiles_and_decoded_boundary" if structure["map"]["decoded_boundary_tiles"]
                                   else "raster_tiles")
            else:
                # 16.8.6 CDrawBuf::init leaves the raster null; IsConstantLocked
                # returns true/density 0 for a type-0 mask with that null raster.
                value, constant_source = 0, "empty_raster"
        elif (neutral and structure["mask_type"] == 1 and "map" not in structure
              and structure.get("constant_fill") in (0.0, 1.0)):
            # The native reader passes this float to MakeConstantFill; the
            # type-1 IsConstantLocked branch returns its density directly.
            value = int(structure["constant_fill"] * 255)
            constant_source = "constant_fill"
        verified = value is not None
        result = {"uuid": identifier, "width": width, "height": height, "format_version": 3,
                "source": "session_comask_readonly", "mask_type": structure["mask_type"],
                "structure": structure, "content_sha256": structure["content_sha256"],
                "file_sha256": _sha(data), "blocks_count": block_count,
                "constant_source": constant_source if verified else None,
                "constant": {"verified": verified, "value_8bit": value if verified else None,
                             "density": value / 255 if verified else None}}
        if decode_raster:
            if (not neutral or structure["mask_type"] != 0 or any(key in structure for key in
                    ("constant_fill", "linear_gradient_raw", "radial_gradient_raw"))):
                raise MaskReadError("Pixel evidence requires a neutral simple raster mask")
            raster = _one(selected[0], "Map").data if "map" in structure else None
            result["raster"] = _raster_evidence(raster, width, height, blocks)
        return result
    except MaskReadError:
        raise
    except (OSError, ValueError, TypeError, struct.error, RecursionError) as exc:
        raise MaskReadError("Cannot read supported saved mask evidence") from exc
