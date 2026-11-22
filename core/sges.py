from __future__ import annotations
import struct
import zlib

SGES_MAGIC = 0x73656773  # 'sges' in little-endian


PAGE_SIZE = 512 * 1024


def _chunks(data: bytes):
    page = 0
    while page < len(data):
        magic, _, count, objects, _, _, _ = struct.unpack_from("<I2H4B", data, page)
        if magic != SGES_MAGIC:
            raise ValueError("Invalid SGES continuation")
        table = page + 12 + objects * 4
        start = (table + count * 4 + 15) & ~15
        if start > len(data):
            raise ValueError("Incomplete SGES chunk table")
        for i in range(count):
            low, flags, high = struct.unpack_from("<HBB", data, table + i * 4)
            end = start + low + (high << 16)
            if end > len(data):
                raise ValueError("Incomplete SGES chunk")
            chunk = data[start:end]
            if flags & 0x10:
                chunk = zlib.decompress(chunk, -15)
            yield flags & ~0x10, chunk
            start = end
        if start >= len(data) or len(data) <= page + PAGE_SIZE:
            break
        page += PAGE_SIZE
        if start > page:
            raise ValueError("SGES page exceeds its streaming boundary")


def sges_group_sizes(data: bytes):
    sizes = []
    for group, chunk in _chunks(data):
        if group == len(sizes):
            sizes.append(0)
        elif group != len(sizes) - 1:
            return ()
        sizes[group] += len(chunk)
    return sizes


def decompress_sges(data: bytes, block_sizes=None) -> bytes:
    if not data.startswith(b"sges"):
        return data
    chunks = list(_chunks(data))
    raw = b"".join(chunk for _, chunk in chunks)
    if block_sizes is None or len(raw) == sum(block_sizes):
        return raw
    # Uncompressed SGES chunks include their 16-byte disk padding. The
    # archive index records the logical size of each destination block.
    blocks = []
    for i, size in enumerate(block_sizes):
        block = b"".join(chunk for group, chunk in chunks if group == i)
        if not size <= len(block) <= ((size + 15) & ~15):
            raise ValueError("SGES data length disagrees with the BIG index")
        blocks.append(block[:size])
    return b"".join(blocks)


def _encode_chunks(header, chunks):
    output = bytearray()
    table = bytearray()
    payload = bytearray()
    offsets = [0] * (1 + max((group for group, _ in chunks), default=0))
    page_header = bytearray(header)

    def flush_page():
        struct.pack_into("<H", page_header, 6, len(table) // 4)
        output.extend(page_header)
        output.extend(table)
        output.extend(bytes(-len(output) % 16))
        output.extend(payload)

    for group, chunk in chunks:
        encoder = zlib.compressobj(9, wbits=-15)
        encoded = encoder.compress(chunk) + encoder.flush()
        encoded += bytes(-len(encoded) % 16)
        flags = group | 0x10
        if len(chunk) % 16 == 0 and len(chunk) <= len(encoded):
            encoded, flags = chunk, group

        header_size = (len(page_header) + len(table) + 4 + 15) & ~15
        if header_size + len(payload) + len(encoded) > PAGE_SIZE:
            if not table:
                raise ValueError("SGES chunk exceeds the streaming page size")
            flush_page()
            output.extend(bytes(-len(output) % PAGE_SIZE))
            page_header = bytearray(header[:12])
            page_header[8] = len(offsets)
            page_header.extend(struct.pack(f"<{len(offsets)}I", *offsets))
            table.clear()
            payload.clear()
        table.extend(struct.pack("<HBB", len(encoded) & 0xffff, flags, len(encoded) >> 16))
        payload.extend(encoded)
        offsets[group] += len(chunk)
    flush_page()
    return bytes(output)


def _split(block, group, size=128 * 1024):
    return [(group, block[i:i + size]) for i in range(0, len(block), size)]


def compress_sges(raw_data: bytes, chunk_size: int = 128 * 1024) -> bytes:
    if not 0 < chunk_size <= 128 * 1024:
        raise ValueError("SGES chunk size must be between 1 and 131072 bytes")
    header = struct.pack("<I2H4B", SGES_MAGIC, 7, 0, 0, 0, 0, 0)
    return _encode_chunks(header, _split(raw_data, 0, chunk_size) or [(0, b"")])


def recompress_sges(data: bytes, template: bytes, size1: int, sequential: bool) -> bytes:
    original = list(_chunks(template))
    same_split = sequential or size1 == sum(len(chunk) for group, chunk in original if group == 0)
    header = template[:12 + template[8] * 4]
    if same_split and len(data) == sum(len(chunk) for _, chunk in original):
        if data == b"".join(chunk for _, chunk in original):
            return template
        chunks = []
        start = 0
        for group, chunk in original:
            end = start + len(chunk)
            chunks.append((group, data[start:end]))
            start = end
    else:
        if sequential or template[8] or any(group > 1 for group, _ in original):
            raise ValueError("Replacement must preserve the sizes of the SGES streaming groups")
        chunks = _split(data[:size1], 0) + _split(data[size1:], 1)
    return _encode_chunks(header, chunks)
