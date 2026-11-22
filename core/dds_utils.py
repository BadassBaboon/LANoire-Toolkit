from __future__ import annotations
import io
import struct
from dataclasses import dataclass
from typing import Any
from PIL import Image

DDS_MAGIC = b"DDS "

# Known square DXT reftextures, indexed by unpacked segment length.
REF_TEXTURE_FORMATS = {
    1398128: (1024, "DXT5"), 349552: (512, "DXT5"), 174800: (512, "DXT1"),
    87408: (256, "DXT5"), 43728: (256, "DXT1"), 43704: (256, "DXT1"),
    21872: (128, "DXT5"), 10960: (128, "DXT1"), 5520: (64, "DXT5"),
    5488: (64, "DXT5"), 2768: (64, "DXT1"), 1424: (32, "DXT5"),
    1392: (32, "DXT5"), 720: (32, "DXT1"), 696: (32, "DXT1"), 368: (16, "DXT5"),
}


def parse_dds_header(data: bytes) -> dict[str, Any]:
    """Parses a 128-byte DDS header into metadata."""
    if len(data) < 128 or data[:4] != DDS_MAGIC:
        return {}

    size, flags, h, w, pitch, depth, mips = struct.unpack("<7I", data[4:32])
    pf_size, pf_flags, fourcc_raw = struct.unpack("<2I4s", data[76:88])

    if size != 124 or pf_size != 32 or not w or not h:
        return {}
    try:
        fourcc = fourcc_raw.decode("ascii").strip("\x00")
    except UnicodeDecodeError:
        fourcc = f"0x{fourcc_raw.hex()}"

    return {
        "width": w,
        "height": h,
        "mipmaps": max(1, mips),
        "fourcc": fourcc,
        "flags": flags,
        "pitch_linear": pitch,
        "depth": depth,
    }


def create_dds_header(
    width: int,
    height: int,
    fourcc: str = "DXT1",
    mip_count: int = 1,
) -> bytes:
    """Builds a standard 128-byte DirectX DDS header."""
    magic = DDS_MAGIC
    size = 124
    flags = 0x1 | 0x2 | 0x4 | 0x1000  # DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT
    if mip_count > 1:
        flags |= 0x20000  # DDSD_MIPMAPCOUNT
    flags |= 0x80000      # DDSD_LINEARSIZE

    pitch_linear = max(1, ((width + 3) // 4)) * max(1, ((height + 3) // 4)) * (8 if fourcc == "DXT1" else 16)
    reserved1 = b"\x00" * 44

    # DDPF
    pf_size = 32
    pf_flags = 0x4  # DDPF_FOURCC
    pf_fourcc = fourcc.encode("ascii").ljust(4, b"\x00")[:4]
    pixel_format = struct.pack("<2I4s5I", pf_size, pf_flags, pf_fourcc, 0, 0, 0, 0, 0)

    caps = 0x1000  # DDSCAPS_TEXTURE
    if mip_count > 1:
        caps |= 0x8 | 0x400000  # DDSCAPS_COMPLEX | DDSCAPS_MIPMAP

    header = struct.pack("<4s7I", magic, size, flags, height, width, pitch_linear, 0, mip_count)
    header += reserved1 + pixel_format + struct.pack("<5I", caps, 0, 0, 0, 0)
    return header


def decode_dds_to_image(dds_bytes: bytes) -> Image.Image:
    """Opens a DDS image, retaining its native pixel mode."""
    return Image.open(io.BytesIO(dds_bytes), formats=["DDS"])


def encode_image_to_dds(
    img: Image.Image,
    fourcc: str = "DXT1",
    generate_mips: bool = True,
) -> bytes:
    """
    Encodes a Pillow image into a DXT-compressed DDS byte stream with mipmaps.
    Supports DXT1 and DXT5.
    """
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    w, h = img.size
    fmt = fourcc.upper()
    if fmt not in ("DXT1", "DXT5"):
        fmt = "DXT1"

    if not generate_mips:
        buf = io.BytesIO()
        img.save(buf, format="DDS", pixel_format=fmt)
        return buf.getvalue()

    # Generate full mipmap chain
    mips: list[bytes] = []
    cur_img = img.copy()

    while True:
        buf = io.BytesIO()
        cur_img.save(buf, format="DDS", pixel_format=fmt)
        # Pillow writes 128-byte header + mip data
        mips.append(buf.getvalue()[128:])

        if cur_img.size == (1, 1):
            break

        nw = max(1, cur_img.width // 2)
        nh = max(1, cur_img.height // 2)
        cur_img = cur_img.resize((nw, nh), Image.Resampling.LANCZOS)

    header = create_dds_header(w, h, fourcc=fmt, mip_count=len(mips))
    payload = b"".join(mips)
    return header + payload


@dataclass
class RefTextureLayout:
    width: int
    format: str
    original: bytes
    levels: list[tuple[int, int]]


def wrap_raw_dxt_to_dds(data: bytes, group_sizes=()):
    if data.startswith(DDS_MAGIC):
        return data, None
    if len(data) not in REF_TEXTURE_FORMATS or not group_sizes or sum(group_sizes) != len(data):
        return None, None

    width, fmt = REF_TEXTURE_FORMATS[len(data)]
    side = width
    end = len(data)
    levels = []
    # Each streaming group after group zero holds one larger mip.
    for size in reversed(group_sizes[1:]):
        end -= size
        if size != max(1, (side + 3) // 4) ** 2 * (8 if fmt == "DXT1" else 16):
            return None, None
        levels.append((end, size))
        side //= 2

    # Group zero packs the remaining small mips. Preserve opaque padding
    # when their individual offsets cannot be inferred from its length.
    tail = []
    pos = 0
    while side:
        size = max(1, (side + 3) // 4) ** 2 * (8 if fmt == "DXT1" else 16)
        tail.append((pos, size))
        pos += size
        side //= 2
    if not tail or tail[0][1] > end:
        return None, None
    levels.extend(tail if pos == end else tail[:1])
    payload = b"".join(data[offset:offset+size] for offset, size in levels)
    return create_dds_header(width, width, fmt, len(levels)) + payload, RefTextureLayout(width, fmt, data, levels)


def unwrap_dds_to_raw_dxt(dds_bytes: bytes, layout: RefTextureLayout) -> bytes:
    info = parse_dds_header(dds_bytes)
    if not info or (info["width"], info["height"], info["fourcc"]) != (layout.width, layout.width, layout.format):
        raise ValueError("Reftexture replacement must match the original dimensions and DXT format")
    if info["mipmaps"] < len(layout.levels):
        raise ValueError("Replacement is missing required mip levels")
    raw = bytearray(layout.original)
    pos = 128
    for offset, size in layout.levels:
        if pos + size > len(dds_bytes):
            raise ValueError("Incomplete DDS mip data")
        raw[offset:offset+size] = dds_bytes[pos:pos+size]
        pos += size
    return bytes(raw)


def extract_all_dds_from_vram(vram_bytes: bytes) -> list[bytes]:
    """Finds and slices all concatenated DDS textures inside a VRAM payload."""
    results: list[bytes] = []
    pos = 0
    while pos < len(vram_bytes):
        idx = vram_bytes.find(DDS_MAGIC, pos)
        if idx == -1:
            break
        next_idx = vram_bytes.find(DDS_MAGIC, idx + 4)
        if next_idx == -1:
            results.append(vram_bytes[idx:])
            break
        else:
            results.append(vram_bytes[idx:next_idx])
            pos = next_idx
    return results
