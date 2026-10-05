from __future__ import annotations
import io
import struct
from typing import Any, Optional
from PIL import Image

DDS_MAGIC = b"DDS "

# Known raw reftexture segment sizes: {total_size: (w, h, fourcc, mips, payload_size)}
REF_TEXTURE_SIZES: dict[int, tuple[int, int, str, int, int]] = {
    1398128: (1024, 1024, "DXT5", 11, 1398096),
    349552:  (512, 512, "DXT5", 10, 349520),
    174800:  (512, 512, "DXT1", 10, 174768),
    87408:   (256, 256, "DXT5", 9, 87376),
    43728:   (256, 256, "DXT1", 9, 43696),
    43704:   (256, 256, "DXT1", 9, 43690),
    21872:   (128, 128, "DXT5", 8, 21840),
    10960:   (128, 128, "DXT1", 8, 10928),
    5520:    (64, 64, "DXT5", 7, 5488),
    5488:    (64, 64, "DXT5", 7, 5488),
    2768:    (64, 64, "DXT1", 7, 2752),
    1424:    (32, 32, "DXT5", 6, 1392),
    1392:    (32, 32, "DXT5", 6, 1392),
    720:     (32, 32, "DXT1", 6, 704),
    696:     (32, 32, "DXT1", 6, 688),
    368:     (16, 16, "DXT5", 5, 352),
}


def parse_dds_header(data: bytes) -> dict[str, Any]:
    """Parses a 128-byte DDS header into metadata."""
    if len(data) < 128 or data[:4] != DDS_MAGIC:
        return {}

    size, flags, h, w, pitch, depth, mips = struct.unpack("<7I", data[4:32])
    pf_size, pf_flags, fourcc_raw = struct.unpack("<2I4s", data[76:88])

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
    """Decodes DDS bytes to a Pillow RGBA image."""
    return Image.open(io.BytesIO(dds_bytes))


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

        if cur_img.width <= 4 and cur_img.height <= 4:
            break

        nw = max(1, cur_img.width // 2)
        nh = max(1, cur_img.height // 2)
        cur_img = cur_img.resize((nw, nh), Image.Resampling.LANCZOS)

    header = create_dds_header(w, h, fourcc=fmt, mip_count=len(mips))
    payload = b"".join(mips)
    return header + payload


def wrap_raw_dxt_to_dds(data: bytes) -> tuple[Optional[bytes], Optional[bytes]]:
    """
    If data is already DDS, returns (data, None).
    If data is a raw DXT payload (e.g. from reftextures), creates DDS header and returns (full_dds, footer).
    Otherwise returns (None, None).
    """
    if len(data) >= 128 and data[:4] == DDS_MAGIC:
        return data, None

    if len(data) in REF_TEXTURE_SIZES:
        w, h, fmt, mips, plen = REF_TEXTURE_SIZES[len(data)]
        dds_hdr = create_dds_header(w, h, fmt, mips)
        payload = data[:plen]
        footer = data[plen:]
        return dds_hdr + payload, footer

    return None, None


def unwrap_dds_to_raw_dxt(dds_bytes: bytes, original_footer: Optional[bytes] = None) -> bytes:
    """Strips the 128-byte DDS header and re-appends footer for reftextures."""
    payload = dds_bytes[128:] if dds_bytes[:4] == DDS_MAGIC else dds_bytes
    if original_footer:
        return payload + original_footer
    return payload


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
