from __future__ import annotations
import struct
from dataclasses import dataclass
from typing import Optional
from PIL import Image

from .dds_utils import (
    decode_dds_to_image,
    encode_image_to_dds,
    parse_dds_header,
    extract_all_dds_from_vram,
)

TRM_MAGIC = b"trM#"

# Well-known TRM subfile hashes
HASH_UNIQUE_TEXTURE_MAIN = 962647487     # 0x395db1df
HASH_UNIQUE_TEXTURE_VRAM = 4056466362    # 0xf1c8bfba
HASH_TEXTURE_MAIN = 1188501517           # 0x46d7180d
HASH_TEXTURE_VRAM = 2390691336          # 0x8e7f0e08
HASH_HIGH_LOD_GRAPHICS_MAIN = 710690163
HASH_HIGH_LOD_GRAPHICS_VRAM = 3807662966


@dataclass
class TRMSubfile:
    hash: int
    size: int
    offset_raw: int
    data: bytes
    is_vram: bool = False


@dataclass
class FaceSubTexture:
    index: int
    hash: int
    offset_in_vram: int
    dds_bytes: bytes
    name: str = ""
    image: Optional[Image.Image] = None
    info: Optional[dict] = None


class FaceTextureSet:
    """Manages textures packed inside TRM VRAM (UniqueTextureVRAM or TextureVRAM)."""

    def __init__(self, main_bytes: Optional[bytes], vram_bytes: bytes, is_unique: bool = True):
        self.raw_main = bytearray(main_bytes) if main_bytes else bytearray()
        self.raw_vram = bytearray(vram_bytes)
        self.is_unique = is_unique
        self.textures: list[FaceSubTexture] = []
        self._parse()

    def _parse(self) -> None:
        parsed_via_table = False
        if len(self.raw_main) >= 8 and self.is_unique and not self.raw_main[:4].startswith(b"ptM"):
            try:
                num_textures = struct.unpack("<I", self.raw_main[4:8])[0]
                offsets: list[tuple[int, int]] = []

                for i in range(num_textures):
                    rec_off = 8 + i * 12
                    if rec_off + 12 > len(self.raw_main):
                        break
                    tex_off, pad, tex_hash = struct.unpack("<3I", self.raw_main[rec_off : rec_off + 12])
                    offsets.append((tex_off, tex_hash))

                if offsets and all(off < len(self.raw_vram) and self.raw_vram[off:off+4] == b"DDS " for off, _ in offsets):
                    for i, (off, tex_hash) in enumerate(offsets):
                        end_off = offsets[i + 1][0] if i + 1 < len(offsets) else len(self.raw_vram)
                        dds_data = bytes(self.raw_vram[off:end_off])
                        role = "Diffuse" if i == 0 else ("Normal" if i == 1 else ("Specular" if i == 2 else f"Tex {i}"))
                        tex = FaceSubTexture(
                            index=i,
                            hash=tex_hash,
                            offset_in_vram=off,
                            dds_bytes=dds_data,
                            name=role,
                            info=parse_dds_header(dds_data),
                        )
                        try:
                            tex.image = decode_dds_to_image(dds_data)
                        except Exception:
                            tex.image = None
                        self.textures.append(tex)
                    parsed_via_table = True
            except Exception:
                parsed_via_table = False

        if not parsed_via_table:
            # Fallback: slice all DDS from VRAM directly
            all_dds = extract_all_dds_from_vram(self.raw_vram)
            cur_off = 0
            for i, dds_data in enumerate(all_dds):
                role = "Diffuse" if i == 0 else ("Normal" if i == 1 else ("Specular" if i == 2 else f"Tex {i}"))
                tex = FaceSubTexture(
                    index=i,
                    hash=0,
                    offset_in_vram=cur_off,
                    dds_bytes=dds_data,
                    name=role,
                    info=parse_dds_header(dds_data),
                )
                try:
                    tex.image = decode_dds_to_image(dds_data)
                except Exception:
                    tex.image = None
                self.textures.append(tex)
                cur_off += len(dds_data)

    def replace_texture(self, index: int, new_dds: bytes) -> None:
        """Replaces a sub-texture and recalculates offsets in Main and VRAM."""
        if index < 0 or index >= len(self.textures):
            raise IndexError(f"Texture index {index} out of range (total {len(self.textures)})")

        self.textures[index].dds_bytes = new_dds
        self.textures[index].info = parse_dds_header(new_dds)
        try:
            self.textures[index].image = decode_dds_to_image(new_dds)
        except Exception:
            self.textures[index].image = None

        new_vram = bytearray()
        new_offsets: list[int] = []

        for tex in self.textures:
            pad = (16 - (len(new_vram) % 16)) % 16
            if pad:
                new_vram.extend(b"\x00" * pad)
            new_offsets.append(len(new_vram))
            new_vram.extend(tex.dds_bytes)

        self.raw_vram = new_vram

        # Update offsets in raw_main if present
        if len(self.raw_main) >= 8:
            for i, off in enumerate(new_offsets):
                rec_off = 8 + i * 12
                if rec_off + 4 <= len(self.raw_main):
                    struct.pack_into("<I", self.raw_main, rec_off, off)
                self.textures[i].offset_in_vram = off


class TRMContainer:
    """Parses and serializes L.A. Noire trM# (Trunk Model) container files."""

    def __init__(self, data: bytes):
        self.raw_data = data
        self.version: int = 1
        self.size1: int = 0
        self.size2: int = 0
        self.subfiles: list[TRMSubfile] = []
        self._parse()

    def _parse(self) -> None:
        if len(self.raw_data) < 24 or self.raw_data[:4] != TRM_MAGIC:
            raise ValueError("Not a valid TRM container (magic mismatch)")

        magic, ver, s1, s2, null_val, num_files = struct.unpack("<4s5I", self.raw_data[:24])
        self.version = ver
        self.size1 = s1
        self.size2 = s2

        for i in range(num_files):
            entry_off = 24 + i * 12
            fh, fsz, foff = struct.unpack("<3I", self.raw_data[entry_off : entry_off + 12])

            is_vram = (foff % 16 != 0) or (foff >= s1)
            if is_vram:
                real_start = s1 + (foff & ~0xF)
            else:
                real_start = foff

            sub_data = self.raw_data[real_start : real_start + fsz]
            self.subfiles.append(
                TRMSubfile(
                    hash=fh,
                    size=fsz,
                    offset_raw=foff,
                    data=sub_data,
                    is_vram=is_vram,
                )
            )

    def get_subfile(self, hash_val: int) -> Optional[TRMSubfile]:
        for sf in self.subfiles:
            if sf.hash == hash_val:
                return sf
        return None

    def get_face_texture_set(self) -> Optional[FaceTextureSet]:
        """Extracts FaceTextureSet from UniqueTextureVRAM or TextureVRAM."""
        # First check UniqueTexture
        main_sf = self.get_subfile(HASH_UNIQUE_TEXTURE_MAIN)
        vram_sf = self.get_subfile(HASH_UNIQUE_TEXTURE_VRAM)
        if vram_sf and len(vram_sf.data) > 0:
            return FaceTextureSet(main_sf.data if main_sf else None, vram_sf.data, is_unique=True)

        # Next check standard Texture
        tex_main = self.get_subfile(HASH_TEXTURE_MAIN)
        tex_vram = self.get_subfile(HASH_TEXTURE_VRAM)
        if tex_vram and len(tex_vram.data) > 0:
            return FaceTextureSet(tex_main.data if tex_main else None, tex_vram.data, is_unique=False)

        return None

    def apply_face_texture_set(self, face_set: FaceTextureSet) -> None:
        """Applies updated texture and VRAM back into the TRM subfile list."""
        if face_set.is_unique:
            main_sf = self.get_subfile(HASH_UNIQUE_TEXTURE_MAIN)
            vram_sf = self.get_subfile(HASH_UNIQUE_TEXTURE_VRAM)
        else:
            main_sf = self.get_subfile(HASH_TEXTURE_MAIN)
            vram_sf = self.get_subfile(HASH_TEXTURE_VRAM)

        if not vram_sf:
            raise ValueError("TRM does not have target Texture VRAM subfile")

        if main_sf and face_set.raw_main:
            main_sf.data = bytes(face_set.raw_main)
            main_sf.size = len(main_sf.data)

        vram_sf.data = bytes(face_set.raw_vram)
        vram_sf.size = len(vram_sf.data)

    def build(self) -> tuple[bytes, int, int]:
        """
        Reassembles the TRM file into binary bytes.
        Returns: (trm_bytes, size1, size2).
        """
        header_table_size = 24 + len(self.subfiles) * 12
        cur_main_off = (header_table_size + 15) & ~15

        main_payload = bytearray(cur_main_off)
        vram_payload = bytearray()

        updated_entries: list[tuple[int, int, int]] = []

        # Write Main subfiles first
        for sf in self.subfiles:
            if not sf.is_vram:
                pad = (16 - (len(main_payload) % 16)) % 16
                if pad:
                    main_payload.extend(b"\x00" * pad)
                sf_offset = len(main_payload)
                main_payload.extend(sf.data)
                updated_entries.append((sf.hash, len(sf.data), sf_offset))

        pad_b1 = (16 - (len(main_payload) % 16)) % 16
        if pad_b1:
            main_payload.extend(b"\x00" * pad_b1)
        new_size1 = len(main_payload)

        # Write VRAM subfiles
        for sf in self.subfiles:
            if sf.is_vram:
                pad = (16 - (len(vram_payload) % 16)) % 16
                if pad:
                    vram_payload.extend(b"\x00" * pad)
                vram_off_relative = len(vram_payload) | 0x1
                vram_payload.extend(sf.data)
                updated_entries.append((sf.hash, len(sf.data), vram_off_relative))

        pad_b2 = (16 - (len(vram_payload) % 16)) % 16
        if pad_b2:
            vram_payload.extend(b"\x00" * pad_b2)
        new_size2 = len(vram_payload)

        trm_hdr = struct.pack(
            "<4s5I",
            TRM_MAGIC,
            self.version,
            new_size1,
            new_size2,
            0,
            len(self.subfiles),
        )

        table_bytes = bytearray()
        for h, sz, off in updated_entries:
            table_bytes.extend(struct.pack("<3I", h, sz, off))

        main_payload[:24] = trm_hdr
        main_payload[24 : 24 + len(table_bytes)] = table_bytes

        total_bytes = bytes(main_payload) + bytes(vram_payload)
        return total_bytes, new_size1, new_size2
