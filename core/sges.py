from __future__ import annotations
import struct
import zlib

SGES_MAGIC = 0x73656773  # 'sges' in little-endian


def decompress_sges(data: bytes) -> bytes:
    """
    Decompresses an SGES multi-chunk container.
    Returns the concatenated raw decompressed payload.
    """
    if len(data) < 12:
        return data

    magic, seg_type, num_chunks, u0, u1, u2, u3 = struct.unpack("<I2H4B", data[:12])
    if magic != SGES_MAGIC:
        # Not an SGES container, return raw data
        return data

    # Header is aligned to 16 bytes after object array and chunk headers
    header_end = 12 + u0 * 4 + num_chunks * 4
    data_offset = (header_end + 15) & ~15

    chunks: list[tuple[int, int]] = []
    chunk_table_offset = 12 + u0 * 4

    for i in range(num_chunks):
        c_off = chunk_table_offset + i * 4
        chunk_sz, flags, coeff = struct.unpack("<H2B", data[c_off : c_off + 4])
        chunk_sz += coeff * 0x10000
        chunks.append((chunk_sz, flags))

    output = bytearray()
    curr_offset = data_offset

    for chunk_sz, flags in chunks:
        if curr_offset + chunk_sz > len(data):
            chunk_bytes = data[curr_offset:]
        else:
            chunk_bytes = data[curr_offset : curr_offset + chunk_sz]

        if flags & 0x10:
            # Raw DEFLATE compression (wbits = -15)
            try:
                decomp = zlib.decompress(chunk_bytes, -15)
                output.extend(decomp)
            except zlib.error:
                # Fallback in case chunk is uncompressed or malformed
                output.extend(chunk_bytes)
        else:
            output.extend(chunk_bytes)

        curr_offset += chunk_sz

    return bytes(output)


def compress_sges(raw_data: bytes, chunk_size: int = 128 * 1024) -> bytes:
    """
    Compresses raw data into an SGES multi-chunk container using raw DEFLATE.
    Produces an aligned, 100% engine-compatible SGES segment.
    """
    if not raw_data:
        raw_chunks = [b""]
    else:
        raw_chunks = [raw_data[i : i + chunk_size] for i in range(0, len(raw_data), chunk_size)]

    comp_chunks: list[bytes] = []
    chunk_headers = bytearray()

    for rc in raw_chunks:
        if len(rc) == 0:
            comp = b""
            flags = 0x00
        else:
            comp = zlib.compress(rc, level=9, wbits=-15)
            flags = 0x10

        # Each chunk is padded to 16-byte alignment
        pad_len = (16 - (len(comp) % 16)) % 16
        comp_aligned = comp + (b"\x00" * pad_len)

        csz = len(comp_aligned)
        coeff = csz // 0x10000
        sz_low = csz % 0x10000

        chunk_headers.extend(struct.pack("<H2B", sz_low, flags, coeff))
        comp_chunks.append(comp_aligned)

    # Base SGES header: magic ('sges'), version 7, num_chunks, u0=0, u1=0, u2=0, u3=0
    hdr = bytearray(struct.pack("<I2H4B", SGES_MAGIC, 7, len(raw_chunks), 0, 0, 0, 0))
    hdr.extend(chunk_headers)

    # Pad header to 16 bytes
    pad_hdr = (16 - (len(hdr) % 16)) % 16
    hdr.extend(b"\x00" * pad_hdr)

    payload = b"".join(comp_chunks)
    total = bytes(hdr) + payload

    # Ensure total segment is 16-byte aligned
    pad_total = (16 - (len(total) % 16)) % 16
    if pad_total:
        total += b"\x00" * pad_total

    return total
