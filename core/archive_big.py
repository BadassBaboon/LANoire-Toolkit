from __future__ import annotations
import os
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Optional

from .hash_db import HashDatabase
from .sges import SGES_MAGIC, compress_sges, decompress_sges


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

    def _load_table(self) -> None:
        if not self.path.exists():
            return

        with open(self.path, "rb") as f:
            f.seek(0, os.SEEK_END)
            self.total_file_size = f.tell()
            if self.total_file_size < 12:
                return

            f.seek(-4, os.SEEK_END)
            self.table_offset = struct.unpack("<I", f.read(4))[0]
            table_pos = self.total_file_size - self.table_offset
            if table_pos < 0 or table_pos >= self.total_file_size:
                return

            f.seek(table_pos)
            archive_type, num_entries = struct.unpack("<2I", f.read(8))
            if archive_type != 3:
                return

            if num_entries > 0:
                self.is_case_0 = False
                for _ in range(num_entries):
                    h, off_raw, s1, s2, s3 = struct.unpack("<5I", f.read(20))
                    real_off = off_raw << 4
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
                while pad in (b"X" * 16, b"\x00" * 16) and curr_offset + 16 < table_pos:
                    curr_offset += 16
                    pad = f.read(16)
            else:
                break

    def read_entry_bytes(self, entry: BigArchiveEntry, decompress: bool = True) -> bytes:
        """Reads raw or decompressed bytes for a given entry."""
        if entry.custom_data is not None:
            return entry.custom_data

        with open(self.path, "rb") as f:
            f.seek(entry.offset)
            read_size = entry.compressed_size if entry.compressed_size > 0 else (entry.size1 + entry.size2)
            raw = f.read(read_size)

        if decompress and raw[:4] == b"sges":
            return decompress_sges(raw)
        return raw

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
            target_path = dest / entry.path
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

        target_entry.custom_data = new_data
        if size1 is not None:
            target_entry.size1 = size1
        else:
            target_entry.size1 = len(new_data)
        if size2 is not None:
            target_entry.size2 = size2
        elif size1 is None:
            target_entry.size2 = 0
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
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if backup_original and out_path.resolve() == self.path.resolve():
            bak_path = self.path.with_suffix(".big.pc.bak")
            if not bak_path.exists():
                shutil.copy2(self.path, bak_path)

        temp_out = out_path.with_suffix(".tmp_big")
        total = len(self.entries)
        new_entries_table: list[tuple[int, int, int, int, int]] = []

        with open(self.path, "rb") as f_in, open(temp_out, "wb") as f_out:
            for i, entry in enumerate(self.entries):
                # 16-byte align current file position
                curr_pos = f_out.tell()
                pad = (16 - (curr_pos % 16)) % 16
                if pad:
                    f_out.write(b"X" * pad)
                    curr_pos += pad

                offset_raw = curr_pos >> 4

                if entry.custom_data is not None:
                    # New / modified content
                    data_to_write = entry.custom_data
                    if compress and entry.is_compressed:
                        data_to_write = compress_sges(data_to_write)
                        comp_size = len(data_to_write)
                        s1 = entry.size1 or len(entry.custom_data)
                        s2 = entry.size2
                    else:
                        comp_size = 0
                        if entry.size1 + entry.size2 == len(entry.custom_data):
                            s1 = entry.size1
                            s2 = entry.size2
                        else:
                            s1 = len(entry.custom_data)
                            s2 = 0
                else:
                    # Stream directly from original file without re-compressing
                    f_in.seek(entry.offset)
                    orig_sz = entry.compressed_size if entry.compressed_size > 0 else (entry.size1 + entry.size2)
                    data_to_write = f_in.read(orig_sz)
                    comp_size = entry.compressed_size
                    s1 = entry.size1
                    s2 = entry.size2

                f_out.write(data_to_write)
                new_entries_table.append((entry.hash, offset_raw, s1, s2, comp_size))

                # Update in-memory entry to match the new file exactly
                entry.offset = offset_raw << 4
                entry.compressed_size = comp_size
                entry.size1 = s1
                entry.size2 = s2
                entry.custom_data = None

                if progress_cb:
                    progress_cb(i + 1, total, entry.path)

            # 16-byte align before table
            curr_pos = f_out.tell()
            pad = (16 - (curr_pos % 16)) % 16
            if pad:
                f_out.write(b"X" * pad)

            # Write Table at EOF
            table_start = f_out.tell()
            if not self.is_case_0:
                f_out.write(struct.pack("<2I", 3, len(new_entries_table)))
                for h, off_r, s1, s2, s3 in new_entries_table:
                    f_out.write(struct.pack("<5I", h, off_r, s1, s2, s3))
            else:
                # Case 0: empty table
                f_out.write(struct.pack("<2I", 3, 0))

            # Table offset from EOF
            table_size_bytes = f_out.tell() + 4 - table_start
            f_out.write(struct.pack("<I", table_size_bytes))

        # Atomic replace
        if out_path.exists():
            out_path.unlink()
        temp_out.rename(out_path)
        self.path = out_path
        return out_path
