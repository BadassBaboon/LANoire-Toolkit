from __future__ import annotations
import os
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

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
        if not self.path.exists():
            return

        with open(self.path, "rb") as f:
            magic = f.read(4)
            if magic != WAD_MAGIC:
                return

            file_count = struct.unpack("<I", f.read(4))[0]
            if file_count == 0:
                return

            # Read entries table
            raw_entries: list[tuple[int, int, int]] = []
            for _ in range(file_count):
                h, off, sz = struct.unpack("<3I", f.read(12))
                raw_entries.append((h, off, sz))

            # Names table follows the last file data
            f.seek(8 + 4 + (file_count - 1) * 12)
            last_off, last_sz = struct.unpack("<2I", f.read(8))
            names_offset = last_off + last_sz

            f.seek(names_offset)
            names: list[str] = []
            for _ in range(file_count):
                nl = struct.unpack("<H", f.read(2))[0]
                name = f.read(nl).decode("latin1", errors="replace")
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
            target = dest / entry.name.replace("/", os.sep)
            self.extract_entry(entry, target)
            if progress_cb:
                progress_cb(i + 1, total, entry.name)

        return total

    def repack(
        self,
        output_file: Path | str,
        backup_original: bool = True,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
    ) -> Path:
        out_path = Path(output_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if backup_original and out_path.resolve() == self.path.resolve():
            bak_path = self.path.with_suffix(".wad.pc.bak")
            if not bak_path.exists():
                shutil.copy2(self.path, bak_path)

        temp_out = out_path.with_suffix(".tmp_wad")
        total = len(self.entries)

        with open(temp_out, "wb") as f_out:
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

        if out_path.exists():
            out_path.unlink()
        temp_out.rename(out_path)
        return out_path
