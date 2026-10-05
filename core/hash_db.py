from __future__ import annotations
import json
import os
import zlib
from pathlib import Path
from typing import Optional


class HashDatabase:
    """Manages CRC32 file name hashes for L.A. Noire archives."""

    _instance: Optional[HashDatabase] = None

    def __init__(self, json_path: Optional[str | Path] = None):
        self.hash_to_path: dict[int, str] = {}
        self.path_to_hash: dict[str, int] = {}
        self._loaded: bool = False
        
        default_paths = [
            Path(__file__).parent.parent / "data" / "name_hashes.json",
        ]
        
        target = Path(json_path) if json_path else None
        if not target or not target.exists():
            for p in default_paths:
                if p.exists():
                    target = p
                    break
        
        if target and target.exists():
            self.load_from_json(target)

    @classmethod
    def get_instance(cls) -> HashDatabase:
        if cls._instance is None:
            cls._instance = HashDatabase()
        return cls._instance

    @staticmethod
    def calc_hash(path: str) -> int:
        """Computes Team Bondi's CRC32 hash for lowercase path."""
        norm = path.lower().replace("\\", "/")
        return zlib.crc32(norm.encode("utf-8")) & 0xFFFFFFFF

    def load_from_json(self, json_path: Path | str) -> int:
        """Loads filename dictionary from name_hashes.json."""
        path = Path(json_path)
        if not path.exists():
            return 0

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        count = 0
        file_hashes = data.get("file_name_hashes", {})
        for str_hash, file_name in file_hashes.items():
            try:
                h = int(str_hash)
                self.hash_to_path[h] = file_name
                self.path_to_hash[file_name.lower().replace("\\", "/")] = h
                count += 1
            except ValueError:
                continue

        self._loaded = True
        return count

    def get_path(self, hash_val: int) -> str:
        """Returns the human-readable path if known, else a fallback hex path."""
        if hash_val in self.hash_to_path:
            return self.hash_to_path[hash_val]
        return f"unknown/0x{hash_val:08x}.bin"

    def get_hash(self, path: str) -> int:
        """Returns hash for path, calculating if not in dict."""
        norm = path.lower().replace("\\", "/")
        if norm in self.path_to_hash:
            return self.path_to_hash[norm]
        return self.calc_hash(norm)

    def register_name(self, name: str) -> int:
        """Registers a newly guessed or found file name."""
        h = self.calc_hash(name)
        self.hash_to_path[h] = name
        self.path_to_hash[name.lower().replace("\\", "/")] = h
        return h

    @property
    def total_entries(self) -> int:
        return len(self.hash_to_path)
