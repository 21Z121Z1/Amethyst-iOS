from __future__ import annotations

import hashlib
import math
import struct
import zlib
from pathlib import Path
from typing import Any

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _decode_png(path: Path | str) -> tuple[int, int, int, bytes]:
    raw = Path(path).read_bytes()
    if not raw.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG file")
    cursor = len(_PNG_SIGNATURE)
    width = height = bit_depth = color_type = interlace = None
    payload = bytearray()
    while cursor + 12 <= len(raw):
        length = struct.unpack(">I", raw[cursor : cursor + 4])[0]
        kind = raw[cursor + 4 : cursor + 8]
        data = raw[cursor + 8 : cursor + 8 + length]
        cursor += 12 + length
        if kind == b"IHDR":
            width, height, bit_depth, color_type, _compression, _filter, interlace = struct.unpack(">IIBBBBB", data)
        elif kind == b"IDAT":
            payload.extend(data)
        elif kind == b"IEND":
            break
    if not width or not height:
        raise ValueError("PNG is missing IHDR")
    if bit_depth != 8 or interlace != 0 or color_type not in {0, 2, 4, 6}:
        raise ValueError(f"unsupported PNG layout: bit_depth={bit_depth} color_type={color_type} interlace={interlace}")
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    stride = width * channels
    decoded = zlib.decompress(bytes(payload))
    expected = height * (stride + 1)
    if len(decoded) != expected:
        raise ValueError(f"unexpected PNG payload size: {len(decoded)} != {expected}")

    output = bytearray(height * stride)
    source = 0
    for y in range(height):
        filter_type = decoded[source]
        source += 1
        row = bytearray(decoded[source : source + stride])
        source += stride
        prior_start = (y - 1) * stride
        for x in range(stride):
            left = row[x - channels] if x >= channels else 0
            up = output[prior_start + x] if y else 0
            up_left = output[prior_start + x - channels] if y and x >= channels else 0
            if filter_type == 1:
                row[x] = (row[x] + left) & 0xFF
            elif filter_type == 2:
                row[x] = (row[x] + up) & 0xFF
            elif filter_type == 3:
                row[x] = (row[x] + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                row[x] = (row[x] + _paeth(left, up, up_left)) & 0xFF
            elif filter_type != 0:
                raise ValueError(f"unsupported PNG filter {filter_type}")
        output[y * stride : (y + 1) * stride] = row
    return width, height, color_type, bytes(output)


def _rgba(color_type: int, pixels: bytes, index: int) -> tuple[int, int, int, int]:
    if color_type == 6:
        return pixels[index], pixels[index + 1], pixels[index + 2], pixels[index + 3]
    if color_type == 2:
        return pixels[index], pixels[index + 1], pixels[index + 2], 255
    if color_type == 4:
        return pixels[index], pixels[index], pixels[index], pixels[index + 1]
    return pixels[index], pixels[index], pixels[index], 255


def analyze_png(path: Path | str, *, max_samples: int = 200_000) -> dict[str, Any]:
    path = Path(path)
    raw = path.read_bytes()
    result: dict[str, Any] = {
        "path": str(path),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    try:
        width, height, color_type, pixels = _decode_png(path)
    except (ValueError, zlib.error, struct.error) as exc:
        result.update({"supported": False, "error": f"{type(exc).__name__}: {exc}"})
        return result

    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    pixel_count = width * height
    step = max(1, math.ceil(pixel_count / max_samples))
    n = 0
    sum_luma = 0.0
    sum_luma_sq = 0.0
    non_black = 0
    alpha_nonzero = 0
    for pixel in range(0, pixel_count, step):
        r, g, b, a = _rgba(color_type, pixels, pixel * channels)
        luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
        sum_luma += luma
        sum_luma_sq += luma * luma
        non_black += int(max(r, g, b) > 8)
        alpha_nonzero += int(a > 0)
        n += 1
    mean = sum_luma / n if n else 0.0
    variance = max(0.0, (sum_luma_sq / n if n else 0.0) - mean * mean)
    result.update({
        "supported": True,
        "width": width,
        "height": height,
        "color_type": color_type,
        "sample_count": n,
        "mean_luma_0_255": round(mean, 4),
        "luma_stddev_0_255": round(math.sqrt(variance), 4),
        "non_black_ratio": round(non_black / n, 6) if n else 0.0,
        "alpha_nonzero_ratio": round(alpha_nonzero / n, 6) if n else 0.0,
    })
    return result


def compare_png_frames(first: Path | str, second: Path | str, *, max_samples: int = 200_000) -> dict[str, Any]:
    try:
        w1, h1, c1, p1 = _decode_png(first)
        w2, h2, c2, p2 = _decode_png(second)
    except (ValueError, zlib.error, struct.error) as exc:
        return {"supported": False, "error": f"{type(exc).__name__}: {exc}"}
    if (w1, h1) != (w2, h2):
        return {"supported": False, "reason": "dimension_mismatch", "first": [w1, h1], "second": [w2, h2]}
    ch1 = {0: 1, 2: 3, 4: 2, 6: 4}[c1]
    ch2 = {0: 1, 2: 3, 4: 2, 6: 4}[c2]
    count = w1 * h1
    step = max(1, math.ceil(count / max_samples))
    sampled = changed = 0
    total_abs = 0
    for pixel in range(0, count, step):
        a = _rgba(c1, p1, pixel * ch1)
        b = _rgba(c2, p2, pixel * ch2)
        delta = (abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))
        total_abs += sum(delta)
        changed += int(max(delta) > 4)
        sampled += 1
    return {
        "supported": True,
        "width": w1,
        "height": h1,
        "sample_count": sampled,
        "changed_pixel_ratio": round(changed / sampled, 6) if sampled else 0.0,
        "mean_abs_rgb_delta_0_255": round(total_abs / (sampled * 3), 4) if sampled else 0.0,
    }
