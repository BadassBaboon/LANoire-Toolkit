from __future__ import annotations
import os
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Optional

from .hash_db import HashDatabase
from .archive_io import backup_if_needed, extraction_path, staged_output
from .sges import recompress_sges, decompress_sges, sges_group_sizes


@dataclass
class BigArchiveEntry:
    hash: int
    offset: int                # Byte offset in archive
    offset_raw: int            # Raw offset (offset >> 4)
    size1: int                 # Decompressed block 1 size
    size2: int                 # Decompressed block 2 size
    compressed_size: int       # Total compressed size on disk (0 if uncompressed)
    path: str = ""
    is_compressed: bool = False
    magic: bytes = b""
    custom_data: Optional[bytes] = None

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
        if self.is_compressed:
            return "SGES (Compressed)"
        if self.magic:
            try:
                m_str = self.magic.decode("ascii", errors="ignore")
                if m_str.isprintable() and len(m_str) >= 2:
                    return m_str
            except Exception:
                pass
        return "BIN (Raw)"


class BigArchive:
    """Reader, extractor, and repacker for L.A. Noire .big.pc archives."""

    def __init__(self, file_path: Path | str, hash_db: Optional[HashDatabase] = None):
        self.path = Path(file_path)
        self.hash_db = hash_db or HashDatabase.get_instance()
        self.entries: list[BigArchiveEntry] = []
        self.is_case_0: bool = False  # Sequential sges archive (e.g. reftextures)
        self.table_offset: int = 0
        self.total_file_size: int = 0
        self._load_table()
        self._stored_sizes = [e.compressed_size or e.size1 + e.size2 for e in self.entries]

    def _load_table(self) -> None:
        with open(self.path, "rb") as f:
            f.seek(0, os.SEEK_END)
            self.total_file_size = f.tell()
            if self.total_file_size < 12:
                raise ValueError("Incomplete BIG archive footer")

            f.seek(-4, os.SEEK_END)
            self.table_offset = struct.unpack("<I", f.read(4))[0]
            table_pos = self.total_file_size - self.table_offset
            if table_pos < 0 or self.table_offset < 12:
                raise ValueError("Invalid BIG table offset")

            f.seek(table_pos)
            archive_type, num_entries = struct.unpack("<2I", f.read(8))
            if archive_type != 3 or self.table_offset != 12 + 20 * num_entries:
                raise ValueError("Unsupported or incomplete BIG table")

            if num_entries > 0:
                self.is_case_0 = False
                for _ in range(num_entries):
                    h, off_raw, s1, s2, s3 = struct.unpack("<5I", f.read(20))
                    real_off = off_raw << 4
                    if real_off + (s3 or s1 + s2) > table_pos:
                        raise ValueError("BIG entry extends beyond the data area")
                    is_comp = (s3 != 0)
                    self.entries.append(
                        BigArchiveEntry(
                            hash=h,
                            offset=real_off,
                            offset_raw=off_raw,
                            size1=s1,
                            size2=s2,
                            compressed_size=s3,
                            path=self.hash_db.get_path(h),
                            is_compressed=is_comp,
                        )
                    )
                # Fast header sniffing (takes <0.3s for 26k entries)
                for entry in self.entries:
                    if not entry.is_compressed:
                        f.seek(entry.offset)
                        entry.magic = f.read(4)
                    else:
                        entry.magic = b"sges"
            else:
                # Case 0: sequential SGES stream (e.g. reftextures_characters.big.pc)
                self.is_case_0 = True
                self._scan_case_0_segments(f, table_pos)

    def _scan_case_0_segments(self, f: BinaryIO, table_pos: int) -> None:
        """Scans sequential SGES chunks for archives without a tail table."""
        curr_offset = 0
        seg_idx = 0

        while curr_offset < table_pos:
            f.seek(curr_offset)
            magic = f.read(4)
            if magic == b"sges":
                typ, num_chunks, u0, u1, u2, u3 = struct.unpack("<2H4B", f.read(8))
                f.seek(curr_offset + 12 + u0 * 4)
                
                total_payload_sz = 0
                for _ in range(num_chunks):
                    csz, flags, coeff = struct.unpack("<H2B", f.read(4))
                    csz += coeff * 0x10000
                    total_payload_sz += csz

                data_offset = (curr_offset + 12 + u0 * 4 + num_chunks * 4 + 15) & ~15
                total_seg_sz = (data_offset - curr_offset) + total_payload_sz
                total_seg_aligned = (total_seg_sz + 15) & ~15

                if curr_offset + total_seg_aligned > table_pos:
                    raise ValueError("Incomplete sequential SGES segment")
                dummy_hash = self.hash_db.calc_hash(f"segments/{seg_idx:06d}.sges")
                self.entries.append(
                    BigArchiveEntry(
                        hash=dummy_hash,
                        offset=curr_offset,
                        offset_raw=curr_offset >> 4,
                        size1=0,
                        size2=0,
                        compressed_size=total_seg_aligned,
                        path=f"segments/{seg_idx:06d}.sges",
                        is_compressed=True,
                        magic=b"sges",
                    )
                )

                curr_offset += total_seg_aligned
                seg_idx += 1

                # Skip any zero / 'X' padding
                f.seek(curr_offset)
                pad = f.read(16)
                while pad in (b"X" * 16, b"\x00" * 16) and curr_offset < table_pos:
                    curr_offset += 16
                    pad = f.read(16)
            else:
                raise ValueError(f"Unrecognized sequential segment at {curr_offset}")

    def read_entry_bytes(self, entry: BigArchiveEntry, decompress: bool = True) -> bytes:
        """Reads raw or decompressed bytes for a given entry."""
        if entry.custom_data is not None:
            return entry.custom_data

        with open(self.path, "rb") as f:
            f.seek(entry.offset)
            read_size = entry.compressed_size if entry.compressed_size > 0 else (entry.size1 + entry.size2)
            raw = f.read(read_size)

        if len(raw) != read_size:
            raise ValueError(f"Incomplete archive entry: {entry.path}")
        if decompress and entry.is_compressed:
            if raw[:4] != b"sges":
                raise ValueError(f"Missing SGES signature: {entry.path}")
            return decompress_sges(raw, None if self.is_case_0 else (entry.size1, entry.size2))
        return raw

    def reftexture_groups(self, entry):
        if not self.is_case_0:
            return ()
        with self.path.open("rb") as source:
            source.seek(entry.offset)
            return sges_group_sizes(source.read(entry.compressed_size))

    def extract_entry(self, entry: BigArchiveEntry, output_path: Path | str, decompress: bool = True) -> Path:
        """Extracts a single entry to disk."""
        data = self.read_entry_bytes(entry, decompress=decompress)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        return out

    def extract_all(
        self,
        output_dir: Path | str,
        decompress: bool = True,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
    ) -> int:
        """Extracts all entries to an output directory."""
        dest = Path(output_dir)
        total = len(self.entries)

        for i, entry in enumerate(self.entries):
            target_path = extraction_path(dest, entry.path)
            self.extract_entry(entry, target_path, decompress=decompress)
            if progress_cb:
                progress_cb(i + 1, total, entry.path)

        return total

    def replace_entry(
        self,
        entry_hash_or_path: int | str,
        new_data: bytes,
        size1: Optional[int] = None,
        size2: Optional[int] = None,
    ) -> bool:
        """Replaces an entry's data in memory ready for repacking."""
        target_entry: Optional[BigArchiveEntry] = None

        if isinstance(entry_hash_or_path, int):
            for e in self.entries:
                if e.hash == entry_hash_or_path:
                    target_entry = e
                    break
        else:
            norm = str(entry_hash_or_path).strip().lower().replace("\\", "/")
            hex_cand = Path(norm).stem
            if hex_cand.startswith("0x"):
                try:
                    val = int(hex_cand, 16)
                    for e in self.entries:
                        if e.hash == val:
                            target_entry = e
                            break
                except ValueError:
                    pass

            if not target_entry:
                h = self.hash_db.get_hash(norm)
                for e in self.entries:
                    if e.hash == h or e.path.lower().replace("\\", "/") == norm or Path(e.path).stem.lower() == hex_cand:
                        target_entry = e
                        break

        if not target_entry:
            return False

        if size1 is None:
            size1 = len(new_data) - (size2 or 0)
        if size2 is None:
            size2 = len(new_data) - size1
        if size1 < 0 or size2 < 0 or size1 + size2 != len(new_data):
            raise ValueError("BIG block sizes must sum to the replacement length")
        target_entry.custom_data = new_data
        target_entry.size1 = size1
        target_entry.size2 = size2
        target_entry.magic = new_data[:4]
        return True

    def repack(
        self,
        output_file: Path | str,
        compress: bool = True,
        backup_original: bool = True,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
    ) -> Path:
        """
        Repacks the archive to output_file with 16-byte alignment, sges chunking, and the entry table at EOF.
        """
        out_path = Path(output_file)
        table_pos = self.total_file_size - self.table_offset
        updates = []
        with staged_output(out_path) as temp:
            # Streaming archives can contain gigabytes of unindexed data.
            # Keep every existing offset unless an indexed replacement must grow.
            shutil.copyfile(self.path, temp)
            with temp.open("r+b") as output:
                for i, entry in enumerate(self.entries):
                    data = entry.custom_data
                    if data is not None:
                        stored_size = self._stored_sizes[i]
                        compressed = entry.is_compressed and (compress or self.is_case_0)
                        if compressed:
                            output.seek(entry.offset)
                            template = output.read(stored_size)
                            data = recompress_sges(data, template, entry.size1, self.is_case_0)
                        offset = entry.offset
                        if self.is_case_0:
                            end = (self.entries[i+1].offset if i+1 < len(self.entries)
                                   else self.total_file_size - self.table_offset)
                            if len(data) > end - offset:
                                raise ValueError(f"{entry.path} exceeds its streaming slot by {len(data) - (end - offset)} bytes")
                        elif len(data) > stored_size:
                            alignment = min(65536, offset & -offset) if offset else 65536
                            offset = (table_pos + alignment - 1) & -alignment
                            table_pos = (offset + len(data) + 15) & ~15
                        output.seek(offset)
                        output.write(data)
                        if self.is_case_0 and len(data) < stored_size:
                            output.write(bytes(stored_size - len(data)))
                        updates.append((i, offset, len(data), compressed))
                    if progress_cb:
                        progress_cb(i + 1, len(self.entries), entry.path)

                if updates and not self.is_case_0:
                    output.seek(table_pos)
                    output.write(struct.pack("<2I", 3, len(self.entries)))
                    changed = {i: (off >> 4, size if comp else 0)
                               for i, off, size, comp in updates}
                    for i, entry in enumerate(self.entries):
                        off, size = changed.get(i, (entry.offset_raw, entry.compressed_size))
                        output.write(struct.pack("<5I", entry.hash, off, entry.size1, entry.size2, size))
                    output.write(struct.pack("<I", self.table_offset))
            backup_if_needed(self.path, out_path, backup_original)

        for i, offset, size, compressed in updates:
            entry = self.entries[i]
            entry.offset = offset
            entry.offset_raw = offset >> 4
            entry.compressed_size = size if compressed else 0
            entry.is_compressed = compressed
            entry.magic = entry.custom_data[:4]
            entry.custom_data = None
            self._stored_sizes[i] = size
        self.total_file_size = table_pos + self.table_offset
        self.path = out_path
        return out_path
