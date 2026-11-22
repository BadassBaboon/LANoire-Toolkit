"""
L.A. Noire Toolkit Core Engine
Supports reading, unpacking, editing, and repacking .big.pc and .wad.pc archives,
TRM models, UniqueTextureVRAM face textures, and SGES deflate chunks.
"""

from pathlib import Path

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
from .archive_wad import WAD_MAGIC, WadArchive, WadArchiveEntry
from .config import (
    load_config,
    save_config,
    get_config_value,
    set_config_value,
    get_ffmpeg_path,
    set_ffmpeg_path,
    detect_all_ffmpeg_candidates,
)

def open_archive(path: Path | str) -> BigArchive | WadArchive:
    path = Path(path)
    with path.open("rb") as source:
        is_wad = source.read(4) == WAD_MAGIC
    return WadArchive(path) if is_wad else BigArchive(path)


__all__ = [
    "open_archive",
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
    "load_config",
    "save_config",
    "get_config_value",
    "set_config_value",
    "get_ffmpeg_path",
    "set_ffmpeg_path",
    "detect_all_ffmpeg_candidates",
]
