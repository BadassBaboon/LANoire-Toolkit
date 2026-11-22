from __future__ import annotations
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .archive_io import backup_if_needed, extraction_path, staged_output
from .hash_db import HashDatabase

WAD_MAGIC = b"WAD\x01"


@dataclass
class WadArchiveEntry:
    hash: int
    offset: int
    size: int
    name: str = ""
    magic: bytes = b""
    custom_data: Optional[bytes] = None

    @property
    def path(self) -> str:
        return self.name

    @property
    def format_label(self) -> str:
        if self.magic == b"DGAD" or "da/" in self.path.lower():
            return "DGAD (Face)"
        if self.magic == b"FSB4" or self.path.lower().endswith((".fsb", ".mp3", ".ogg")):
            return "FSB4 (Audio)"
        if self.magic.startswith(b"ptM"):
            return "ptM# (Particle)"
        if self.magic.startswith(b"trM") or self.path.lower().endswith((".trm", ".chunk")):
            return "trM# (Model)"
        if self.magic == b"DDS " or self.path.lower().endswith(".dds"):
            return "DDS (Texture)"
        if self.magic:
            try:
                m_str = self.magic.decode("ascii", errors="ignore")
                if m_str.isprintable() and len(m_str) >= 2:
                    return m_str
            except Exception:
                pass
        return "BIN (Raw)"


class WadArchive:
    """Reader, extractor, and repacker for L.A. Noire .wad.pc archives (e.g. out.wad.pc)."""

    def __init__(self, file_path: Path | str, hash_db: Optional[HashDatabase] = None):
        self.path = Path(file_path)
        self.hash_db = hash_db or HashDatabase.get_instance()
        self.entries: list[WadArchiveEntry] = []
        self._load()

    def _load(self) -> None:

        with open(self.path, "rb") as f:
            magic = f.read(4)
            if magic != WAD_MAGIC:
                raise ValueError("Invalid WAD signature")

            file_count = struct.unpack("<I", f.read(4))[0]
            file_size = os.fstat(f.fileno()).st_size
            if 8 + file_count * 12 > file_size:
                raise ValueError("Incomplete WAD entry table")
            if file_count == 0:
                return

            # Read entries table
            raw_entries: list[tuple[int, int, int]] = []
            for _ in range(file_count):
                h, off, sz = struct.unpack("<3I", f.read(12))
                if off < 8 + 12 * file_count or off + sz > file_size:
                    raise ValueError("Invalid WAD entry extent")
                raw_entries.append((h, off, sz))

            # Names table follows the last file data
            names_offset = max(off + sz for _, off, sz in raw_entries)

            f.seek(names_offset)
            names: list[str] = []
            for _ in range(file_count):
                nl = struct.unpack("<H", f.read(2))[0]
                raw_name = f.read(nl)
                if len(raw_name) != nl:
                    raise ValueError("Incomplete WAD name")
                name = raw_name.decode("latin1")
                names.append(name)

            for i in range(file_count):
                h, off, sz = raw_entries[i]
                n = names[i]
                self.hash_db.register_name(n)
                f.seek(off)
                m = f.read(4)
                self.entries.append(
                    WadArchiveEntry(
                        hash=h,
                        offset=off,
                        size=sz,
                        name=n,
                        magic=m,
                    )
                )

    def read_entry_bytes(self, entry: WadArchiveEntry) -> bytes:
        if entry.custom_data is not None:
            return entry.custom_data

        with open(self.path, "rb") as f:
            f.seek(entry.offset)
            return f.read(entry.size)

    def extract_entry(self, entry: WadArchiveEntry, output_path: Path | str) -> Path:
        data = self.read_entry_bytes(entry)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        return out

    def extract_all(
        self,
        output_dir: Path | str,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
    ) -> int:
        dest = Path(output_dir)
        total = len(self.entries)

        for i, entry in enumerate(self.entries):
            target = extraction_path(dest, entry.name)
            self.extract_entry(entry, target)
            if progress_cb:
                progress_cb(i + 1, total, entry.name)

        return total

    def replace_entry(self, entry_hash: int, new_data: bytes) -> bool:
        for entry in self.entries:
            if entry.hash == entry_hash:
                entry.custom_data = new_data
                entry.magic = new_data[:4]
                return True
        return False

    def repack(
        self,
        output_file: Path | str,
        backup_original: bool = True,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
    ) -> Path:
        out_path = Path(output_file)
        total = len(self.entries)
        with staged_output(out_path) as temp:
            with temp.open("wb") as f_out:
                f_out.write(WAD_MAGIC)
                f_out.write(struct.pack("<I", total))

                # Header table placeholder
                data_start_offset = 8 + total * 12
                f_out.seek(data_start_offset)

                updated_entries: list[tuple[int, int, int, str]] = []

                for i, entry in enumerate(self.entries):
                    curr_off = f_out.tell()
                    data = self.read_entry_bytes(entry)
                    f_out.write(data)
                    updated_entries.append((entry.hash, curr_off, len(data), entry.name))
                    if progress_cb:
                        progress_cb(i + 1, total, entry.name)

                # Names table
                for _, _, _, name in updated_entries:
                    encoded = name.encode("latin1", errors="replace")
                    f_out.write(struct.pack("<H", len(encoded)))
                    f_out.write(encoded)

                # Seek back and write header entries table
                f_out.seek(8)
                for h, off, sz, _ in updated_entries:
                    f_out.write(struct.pack("<3I", h, off, sz))

            backup_if_needed(self.path, out_path, backup_original)

        self.path = out_path
        for entry, (_, offset, size, _) in zip(self.entries, updated_entries):
            entry.offset = offset
            entry.size = size
            entry.custom_data = None
        return out_path
