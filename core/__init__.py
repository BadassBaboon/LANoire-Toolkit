"""
L.A. Noire Toolkit Core Engine
Supports reading, unpacking, editing, and repacking .big.pc and .wad.pc archives,
TRM models, UniqueTextureVRAM face textures, and SGES deflate chunks.
"""

from .hash_db import HashDatabase
from .sges import decompress_sges, compress_sges
from .dds_utils import (
    parse_dds_header,
    create_dds_header,
    decode_dds_to_image,
    encode_image_to_dds,
)
from .trm_processor import TRMContainer, TRMSubfile, FaceTextureSet
from .archive_big import BigArchive, BigArchiveEntry
from .archive_wad import WadArchive, WadArchiveEntry

__all__ = [
    "HashDatabase",
    "decompress_sges",
    "compress_sges",
    "parse_dds_header",
    "create_dds_header",
    "decode_dds_to_image",
    "encode_image_to_dds",
    "TRMContainer",
    "TRMSubfile",
    "FaceTextureSet",
    "BigArchive",
    "BigArchiveEntry",
    "WadArchive",
    "WadArchiveEntry",
]
