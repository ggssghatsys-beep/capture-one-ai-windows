"""Bounded read-only 64x64 mask tiles from the checked Mac 16.8.6 format.

Supports raw codec 0 and codec 2 delta runs 0..5/15. This implements the local
Decompress64x64Delta/UnpackBits_REF layout; it never loads vendor code. Unknown
codecs, run kinds and flag bits are refused, not approximated.
"""
from __future__ import annotations


class MaskCodecError(ValueError):
    pass


def decode_tile(block: bytes) -> bytes:
    """Return exactly 4096 row-major bytes, including the tile's edge padding."""
    if not isinstance(block, bytes) or not 1 <= len(block) <= 65535:
        raise MaskCodecError("Invalid compressed tile length")
    if block[0] == 0:
        if len(block) != 4097:
            raise MaskCodecError("Raw tile must contain exactly 4096 pixels")
        return block[1:]
    if block[0] != 2:
        raise MaskCodecError("Unsupported mask tile codec")
    position = 1

    def take(length):
        nonlocal position
        if length < 0 or position + length > len(block):
            raise MaskCodecError("Truncated delta tile")
        value = block[position:position + length]
        position += length
        return value

    first = take(1)[0]
    counts = {kind: 0 for kind in (0, 1, 2, 3, 4, 5, 15)}
    counts[1] = 4  # transpose, scan order, two reserved flags
    runs, total = [], 0
    while total < 4095:
        token = take(1)[0]
        if token < 64:
            kind, length = 0, token + 1
        elif token < 176:
            kind, length = (token - 64) // 16 + 1, (token & 15) + 1
        elif token < 240:
            kind, length = (token - 176) // 8 + 8, (token & 7) + 1
        else:
            kind, part = token & 15, take(1)[0]
            if part & 128:
                if part & 64:
                    raise MaskCodecError("Unsupported delta run length flag")
                length = ((part & 63) << 8) + take(1)[0] + 1
            else:
                length = part + 1
        if kind not in counts:
            raise MaskCodecError("Unsupported delta run kind")
        total += length
        if total > 4095:
            raise MaskCodecError("Delta runs exceed a 64x64 tile")
        counts[kind] += length + (1 if 1 <= kind <= 5 else 0)
        runs.append((kind, length))

    def unpack(bits, count):
        values, mask = [], (1 << bits) - 1
        for group in (16, 4):
            size = group * (8 // bits)
            while count >= size:
                packed = take(group)
                values.extend((value >> shift) & mask
                              for shift in range(0, 8, bits) for value in packed)
                count -= size
        while count:
            value, size = take(1)[0], min(8 // bits, count)
            values.extend((value >> (bits * shift)) & mask for shift in range(size))
            count -= size
        return values

    one = unpack(1, counts[1] + counts[3] + counts[5])
    two = unpack(2, counts[2] + counts[3])
    four = unpack(4, counts[4] + counts[5])
    streams = {
        1: one[:counts[1]], 2: two[:counts[2]], 4: four[:counts[4]],
        3: [low | (high << 1) for low, high in
            zip(one[counts[1]:counts[1] + counts[3]], two[counts[2]:])],
        5: [low | (high << 1) for low, high in
            zip(one[counts[1] + counts[3]:], four[counts[4]:])],
        15: list(take(counts[15])),
    }
    if position != len(block) or streams[1][2] or streams[1][3]:
        raise MaskCodecError("Unconsumed tile bytes or unsupported delta flags")
    offsets = {kind: 0 for kind in streams}
    offsets[1] = 4
    pixels = [first]
    for kind, length in runs:
        if kind == 0:
            pixels.extend([0] * length)
            continue
        start = offsets[kind]
        if kind == 15:
            pixels.extend(streams[kind][start:start + length])
            offsets[kind] += length
        else:
            base = streams[kind][start]
            pixels.extend((value - base) & 255 for value in streams[kind][start + 1:start + length + 1])
            offsets[kind] += length + 1
    if len(pixels) != 4096 or any(offsets[kind] != counts[kind] for kind in streams):
        raise MaskCodecError("Invalid decoded tile count")
    if streams[1][1]:
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
    if streams[1][0]:
        pixels = [pixels[column * 64 + row] for row in range(64) for column in range(64)]
    return bytes(pixels)
