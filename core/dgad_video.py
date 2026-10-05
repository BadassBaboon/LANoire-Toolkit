from __future__ import annotations
import io
import os
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# Standard Ogg CRC-32 table generator (RFC 3533 polynomial 0x04C11DB7)
_CRC_LOOKUP = [0] * 256
for _i in range(256):
    _curr = _i << 24
    for _ in range(8):
        if _curr & 0x80000000:
            _curr = ((_curr << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
        else:
            _curr = (_curr << 1) & 0xFFFFFFFF
    _CRC_LOOKUP[_i] = _curr


def calc_ogg_crc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) ^ _CRC_LOOKUP[((crc >> 24) ^ b) & 0xFF]) & 0xFFFFFFFF
    return crc


def make_ogg_page(header_type: int, granulepos: int, serial: int, page_no: int, packets: list[bytes]) -> bytes:
    """Builds a single RFC 3533 Ogg Page with the specified packets and checksum."""
    seg_table = bytearray()
    payload = bytearray()
    for pkt in packets:
        l = len(pkt)
        while l >= 255:
            seg_table.append(255)
            l -= 255
        seg_table.append(l)
        payload.extend(pkt)

    hdr = bytearray(b"OggS\x00")
    hdr.append(header_type)
    hdr.extend(struct.pack("<QII", granulepos, serial, page_no))
    hdr.extend(b"\x00\x00\x00\x00")  # Checksum placeholder at offset 22
    hdr.append(len(seg_table))
    hdr.extend(seg_table)

    full_page = hdr + payload
    crc = calc_ogg_crc(bytes(full_page))
    struct.pack_into("<I", full_page, 22, crc)
    return bytes(full_page)


def demux_ogg_packets(ogv_data: bytes) -> list[bytes]:
    """Extracts raw Theora packets from an Ogg bitstream."""
    pos = 0
    packets: list[bytes] = []
    current_packet = bytearray()

    while pos < len(ogv_data):
        if ogv_data[pos:pos+4] != b"OggS":
            # Search for next OggS sync code if slight desync
            next_sync = ogv_data.find(b"OggS", pos + 1)
            if next_sync == -1:
                break
            pos = next_sync

        if pos + 27 > len(ogv_data):
            break

        n_segs = ogv_data[pos + 26]
        seg_table = ogv_data[pos + 27 : pos + 27 + n_segs]
        payload_offset = pos + 27 + n_segs

        curr_p = payload_offset
        for seg_len in seg_table:
            if curr_p + seg_len > len(ogv_data):
                break
            current_packet.extend(ogv_data[curr_p : curr_p + seg_len])
            curr_p += seg_len
            if seg_len < 255:
                packets.append(bytes(current_packet))
                current_packet = bytearray()

        pos = curr_p

    return packets


def find_ffmpeg() -> Optional[str]:
    """Finds FFmpeg executable in PATH or known default directories."""
    # Check environment variable first
    env_p = os.environ.get("FFMPEG_PATH")
    if env_p and Path(env_p).is_file():
        return env_p

    # Check system PATH
    which_p = shutil.which("ffmpeg")
    if which_p:
        return which_p

    # Check local directories and standard install locations
    local_candidates = [
        Path.cwd() / "ffmpeg.exe",
        Path.cwd() / "bin" / "ffmpeg.exe",
        Path(__file__).resolve().parent.parent / "bin" / "ffmpeg.exe",
        Path(os.path.expandvars(r"%ProgramFiles%\ffmpeg\bin\ffmpeg.exe")),
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
        Path(r"C:\ffmpeg\ffmpeg.exe"),
        Path(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe")),
        Path(os.path.expandvars(r"%USERPROFILE%\scoop\shims\ffmpeg.exe")),
        Path(os.path.expandvars(r"%ProgramData%\chocolatey\bin\ffmpeg.exe")),
    ]
    for c in local_candidates:
        if c.is_file():
            return str(c)

    return None


@dataclass
class DGADFrame:
    frame_index: int
    header_208: bytes
    stream1_mocap: bytes
    stream2_normals: bytes
    stream3_color: bytes


@dataclass
class DGADInfo:
    version: int
    frame_count: int
    fps: int
    macroblock_scale: int
    resolution: int
    frames: list[DGADFrame]
    raw_header_52: bytes


def parse_dgad_chunk(data: bytes) -> DGADInfo:
    """Parses a DGAD binary chunk into frames and metadata."""
    if len(data) < 52 or data[:4] != b"DGAD":
        raise ValueError("Not a valid DGAD file (magic mismatch or size < 52 bytes)")

    u = struct.unpack("<13I", data[:52])
    version = u[1]
    frame_count = u[7]
    fps = u[8]
    macroblock_scale = u[11]
    resolution = u[12]

    pos = 52
    frames: list[DGADFrame] = []
    f_idx = 0

    while pos < len(data):
        if pos + 4 > len(data):
            break
        flen = struct.unpack("<I", data[pos : pos + 4])[0]
        fdata = data[pos + 4 : pos + 4 + flen]
        if len(fdata) < 208:
            break

        s1_sz = struct.unpack("<I", fdata[180:184])[0]
        s2_sz = struct.unpack("<I", fdata[188:192])[0]
        s3_sz = struct.unpack("<I", fdata[196:200])[0]

        s1_bytes = fdata[208 : 208 + s1_sz]
        s2_bytes = fdata[208 + s1_sz : 208 + s1_sz + s2_sz]
        s3_bytes = fdata[208 + s1_sz + s2_sz : 208 + s1_sz + s2_sz + s3_sz]
        fhdr = fdata[:208]

        frames.append(
            DGADFrame(
                frame_index=f_idx,
                header_208=fhdr,
                stream1_mocap=s1_bytes,
                stream2_normals=s2_bytes,
                stream3_color=s3_bytes,
            )
        )
        f_idx += 1
        pos += 4 + flen

    return DGADInfo(
        version=version,
        frame_count=frame_count,
        fps=fps,
        macroblock_scale=macroblock_scale,
        resolution=resolution,
        frames=frames,
        raw_header_52=data[:52],
    )


def extract_video_stream_to_ogv(dgad: DGADInfo, stream_name: str = "color", serial: int = 0x12345678) -> bytes:
    """Extracts either 'color' (Stream 3) or 'normals' (Stream 2) into valid RFC 3533 Ogg Theora bytes."""
    header_packets: list[bytes] = []
    video_packets: list[bytes] = []

    for idx, frame in enumerate(dgad.frames):
        s_data = frame.stream3_color if stream_name == "color" else frame.stream2_normals
        if idx == 0:
            cpos = 0
            while cpos < len(s_data):
                pkt_len = struct.unpack("<I", s_data[cpos : cpos + 4])[0]
                cpos += 4
                if pkt_len == 0xFFFFFFFF:
                    break
                header_packets.append(s_data[cpos : cpos + pkt_len])
                cpos += pkt_len
            video_packets.append(s_data[cpos:])
        else:
            video_packets.append(s_data)

    if len(header_packets) < 3:
        raise ValueError(f"Failed to find 3 Theora header packets in stream '{stream_name}'")

    pages: list[bytes] = []
    page_no = 0

    # Page 0: BOS with identification header (Packet 0)
    pages.append(make_ogg_page(0x02, 0, serial, page_no, [header_packets[0]]))
    page_no += 1

    # Page 1: Comment and Setup headers (Packets 1 and 2)
    pages.append(make_ogg_page(0x00, 0, serial, page_no, [header_packets[1], header_packets[2]]))
    page_no += 1

    # Video pages
    last_keyframe = 0
    kfgshift = 6

    for idx, pkt in enumerate(video_packets):
        is_key = (pkt[0] & 0x40) == 0
        if is_key:
            last_keyframe = idx

        granpos = (last_keyframe << kfgshift) | (idx - last_keyframe)
        is_last = idx == len(video_packets) - 1
        flags = 0x04 if is_last else 0x00

        pages.append(make_ogg_page(flags, granpos, serial, page_no, [pkt]))
        page_no += 1

    return b"".join(pages)


def extract_first_frame_image(chunk_data: bytes, stream_name: str = "color") -> Optional[Any]:
    """Decodes Frame 0 of a DGAD chunk into a PIL Image for fast UI preview."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    try:
        from PIL import Image
        dgad = parse_dgad_chunk(chunk_data)
        ogv_data = extract_video_stream_to_ogv(dgad, stream_name=stream_name)
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            "pipe:0",
            "-vframes",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "pipe:1",
        ]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        png_bytes, _ = proc.communicate(input=ogv_data, timeout=5)
        if png_bytes:
            return Image.open(io.BytesIO(png_bytes))
    except Exception:
        pass
    return None


def export_face_video(
    chunk_data: bytes,
    output_path: Path | str,
    export_format: str = "mp4",
    stream_name: str = "color",
    ffmpeg_exe: Optional[str] = None,
) -> Path:
    """Exports MotionScan video from a DGAD chunk to .mp4 (default) or .ogv."""
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    dgad = parse_dgad_chunk(chunk_data)
    ogv_bytes = extract_video_stream_to_ogv(dgad, stream_name=stream_name)

    if export_format.lower() == "ogv":
        with open(out_path, "wb") as f:
            f.write(ogv_bytes)
        return out_path

    # Export as MP4 via FFmpeg
    ffmpeg = ffmpeg_exe or find_ffmpeg()
    if not ffmpeg:
        # Fall back to saving as .ogv if FFmpeg is not found
        fallback_path = out_path.with_suffix(".ogv")
        with open(fallback_path, "wb") as f:
            f.write(ogv_bytes)
        raise RuntimeError(
            f"FFmpeg not found! Saved raw Theora stream to {fallback_path} instead. "
            "Please ensure FFmpeg is installed to export directly to .mp4."
        )

    with tempfile.NamedTemporaryFile(suffix=".ogv", delete=False) as tmp_ogv:
        tmp_ogv.write(ogv_bytes)
        tmp_ogv_name = tmp_ogv.name

    try:
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            tmp_ogv_name,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"FFmpeg transcode failed:\n{res.stderr}")
    finally:
        if os.path.exists(tmp_ogv_name):
            os.remove(tmp_ogv_name)

    return out_path


def rebuild_dgad_with_video(
    orig_chunk_data: bytes,
    input_video_path: Path | str,
    stream_name: str = "color",
    target_bitrate: str = "1500k",
    ffmpeg_exe: Optional[str] = None,
) -> bytes:
    """Re-encodes and repacks a video file (.mp4, .ogv, etc.) back into a DGAD chunk."""
    in_video = Path(input_video_path)
    if not in_video.is_file():
        raise FileNotFoundError(f"Video file not found: {in_video}")

    dgad = parse_dgad_chunk(orig_chunk_data)

    # If already an .ogv, read directly; otherwise encode with FFmpeg
    if in_video.suffix.lower() == ".ogv":
        with open(in_video, "rb") as f:
            ogv_bytes = f.read()
    else:
        ffmpeg = ffmpeg_exe or find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("FFmpeg is required to re-encode video to Theora .ogv format!")

        with tempfile.NamedTemporaryFile(suffix=".ogv", delete=False) as tmp_ogv:
            tmp_ogv_name = tmp_ogv.name

        try:
            cmd = [
                ffmpeg,
                "-y",
                "-i",
                str(in_video),
                "-r",
                str(dgad.fps),
                "-c:v",
                "libtheora",
                "-b:v",
                target_bitrate,
                tmp_ogv_name,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise RuntimeError(f"FFmpeg encoding to Theora failed:\n{res.stderr}")
            with open(tmp_ogv_name, "rb") as f:
                ogv_bytes = f.read()
        finally:
            if os.path.exists(tmp_ogv_name):
                os.remove(tmp_ogv_name)

    # Demux packets from OGV
    pkts = demux_ogg_packets(ogv_bytes)
    if len(pkts) < 3:
        raise ValueError(f"Extracted OGV has less than 3 header packets ({len(pkts)} found)")

    hdr0, hdr1, hdr2 = pkts[0], pkts[1], pkts[2]
    frame_pkts = pkts[3:]

    if len(frame_pkts) != len(dgad.frames):
        raise ValueError(
            f"Frame count mismatch: video has {len(frame_pkts)} frames, but chunk requires {len(dgad.frames)} frames!"
        )

    # Read dimensions from identification header
    fmbw, fmbh = struct.unpack(">HH", hdr0[10:14])
    new_width = fmbw * 16

    # Build stream payload for each frame
    new_stream_per_frame: list[bytes] = []
    f0_hdr = bytearray()
    f0_hdr.extend(struct.pack("<I", len(hdr0)) + hdr0)
    f0_hdr.extend(struct.pack("<I", len(hdr1)) + hdr1)
    f0_hdr.extend(struct.pack("<I", len(hdr2)) + hdr2)
    f0_hdr.extend(struct.pack("<I", 0xFFFFFFFF))
    f0_hdr.extend(frame_pkts[0])
    new_stream_per_frame.append(bytes(f0_hdr))

    for i in range(1, len(frame_pkts)):
        new_stream_per_frame.append(frame_pkts[i])

    # Rebuild DGAD binary
    out = bytearray(dgad.raw_header_52)
    if stream_name == "color":
        struct.pack_into("<I", out, 44, new_width // 4)
        struct.pack_into("<I", out, 48, new_width)

    for i, frame in enumerate(dgad.frames):
        s1 = frame.stream1_mocap
        if stream_name == "color":
            s2 = frame.stream2_normals
            s3 = new_stream_per_frame[i]
        else:
            s2 = new_stream_per_frame[i]
            s3 = frame.stream3_color

        flen = 208 + len(s1) + len(s2) + len(s3)
        out.extend(struct.pack("<I", flen))

        fhdr_m = bytearray(frame.header_208)
        struct.pack_into("<I", fhdr_m, 180, len(s1))
        struct.pack_into("<I", fhdr_m, 188, len(s2))
        struct.pack_into("<I", fhdr_m, 196, len(s3))

        out.extend(fhdr_m)
        out.extend(s1)
        out.extend(s2)
        out.extend(s3)

    return bytes(out)


def batch_export_face_videos(
    archive: Any,
    output_dir: Path | str,
    character_filter: str = "",
    export_format: str = "mp4",
    stream_name: str = "color",
    export_normals: bool = False,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> int:
    """
    Extracts MotionScan facial performance videos from a .big.pc archive.
    Writes a manifest.json file mapping video names to archive paths and hashes.
    Supports named and hashed (unknown/0x...bin) DGAD entries.
    """
    import json

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    da_entries = [
        e for e in archive.entries
        if getattr(e, "magic", None) == b"DGAD" or "da/" in e.path.lower()
    ]
    if character_filter:
        filt = character_filter.lower().strip()
        da_entries = [
            e for e in da_entries
            if filt in e.path.lower() or filt in f"0x{e.hash:08x}"
        ]

    total = len(da_entries)
    if total == 0:
        return 0

    manifest: dict[str, dict] = {}
    exported_count = 0

    for i, entry in enumerate(da_entries):
        base_name = Path(entry.path).stem
        if progress_cb:
            progress_cb(i + 1, total, base_name)

        chunk_data = archive.read_entry_bytes(entry)
        if len(chunk_data) < 52 or chunk_data[:4] != b"DGAD":
            continue

        try:
            dgad = parse_dgad_chunk(chunk_data)
        except Exception:
            continue

        # Export color video (stream 3)
        video_filename = f"{base_name}.{export_format.lower()}"
        out_video_path = out_dir / video_filename
        try:
            export_face_video(
                chunk_data,
                out_video_path,
                export_format=export_format,
                stream_name=stream_name,
            )
            exported_count += 1
            manifest[base_name] = {
                "archive_path": entry.path,
                "hash": entry.hash,
                "hex_hash": f"0x{entry.hash:08x}",
                "frames": len(dgad.frames),
                "fps": dgad.fps,
                "orig_res": dgad.resolution,
                "exported_file": video_filename,
            }
        except Exception as err:
            print(f"Error exporting {base_name}: {err}")

        # Optionally export normals video (stream 2)
        if export_normals:
            norm_filename = f"{base_name}_normals.{export_format.lower()}"
            norm_path = out_dir / norm_filename
            try:
                export_face_video(
                    chunk_data,
                    norm_path,
                    export_format=export_format,
                    stream_name="normals",
                )
            except Exception as err:
                print(f"Error exporting normals for {base_name}: {err}")

    # Write manifest.json
    manifest_path = out_dir / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    return exported_count


def batch_import_face_videos(
    archive: Any,
    input_dir: Path | str,
    stream_name: str = "color",
    target_bitrate: str = "1500k",
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> list[str]:
    """
    Scans a directory of video files (.mp4, .ogv, etc.), matches them to
    DGAD chunks in the archive, re-encodes, and injects them into memory.
    Returns the list of modified entry paths.
    """
    import json

    in_dir = Path(input_dir)
    if not in_dir.is_dir():
        raise NotADirectoryError(f"Directory not found: {in_dir}")

    # Load manifest if available
    manifest: dict[str, dict] = {}
    manifest_p = in_dir / "manifest.json"
    if manifest_p.is_file():
        try:
            with open(manifest_p, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            manifest = {}

    # Gather video files in input_dir
    video_exts = {".mp4", ".ogv", ".mkv", ".mov", ".avi"}
    video_files = [p for p in in_dir.iterdir() if p.is_file() and p.suffix.lower() in video_exts]

    if not video_files:
        return []

    # Build lookup maps for archive entries
    da_by_stem: dict[str, Any] = {}
    da_by_path: dict[str, Any] = {}
    da_by_hash: dict[int, Any] = {}

    for e in archive.entries:
        if getattr(e, "magic", None) == b"DGAD" or "da/" in e.path.lower():
            s = Path(e.path).stem.lower()
            da_by_stem[s] = e
            da_by_stem[f"0x{e.hash:08x}".lower()] = e
            da_by_path[e.path.lower().replace("\\", "/")] = e
            da_by_hash[e.hash] = e

    matched_pairs: list[tuple[Path, Any]] = []

    for v_file in video_files:
        v_stem = v_file.stem.lower()

        # 1. Check manifest match
        target_entry = None
        for m_key, m_val in manifest.items():
            if m_key.lower() == v_stem or m_val.get("exported_file", "").lower() == v_file.name.lower():
                # Try integer hash
                if "hash" in m_val and m_val["hash"] in da_by_hash:
                    target_entry = da_by_hash[m_val["hash"]]
                    break
                # Try hex hash
                if "hex_hash" in m_val:
                    try:
                        h_val = int(m_val["hex_hash"], 16)
                        if h_val in da_by_hash:
                            target_entry = da_by_hash[h_val]
                            break
                    except ValueError:
                        pass
                # Try archive path
                arch_p = m_val.get("archive_path", "").lower().replace("\\", "/")
                if arch_p in da_by_path:
                    target_entry = da_by_path[arch_p]
                    break
                if m_key.lower() in da_by_stem:
                    target_entry = da_by_stem[m_key.lower()]
                    break

        # 2. Heuristic match if manifest absent or no match in manifest
        if not target_entry:
            clean_stem = v_stem
            for suffix in ["_upscaled", "_enhanced", "_4x", "_2x", "_out", "_color", "_reencoded"]:
                if clean_stem.endswith(suffix):
                    clean_stem = clean_stem[:-len(suffix)]

            # Check if clean_stem starts with hex hash 0x...
            if clean_stem.startswith("0x"):
                hex_str = clean_stem.split("_")[0]
                try:
                    h_val = int(hex_str, 16)
                    if h_val in da_by_hash:
                        target_entry = da_by_hash[h_val]
                except ValueError:
                    pass

            if not target_entry and clean_stem in da_by_stem:
                target_entry = da_by_stem[clean_stem]

            if not target_entry:
                # Substring match if clean_stem begins with an entry stem
                for e_stem, e_obj in da_by_stem.items():
                    if clean_stem.startswith(e_stem):
                        target_entry = e_obj
                        break

        if target_entry:
            matched_pairs.append((v_file, target_entry))

    total = len(matched_pairs)
    injected_paths: list[str] = []

    for idx, (v_file, entry) in enumerate(matched_pairs):
        if progress_cb:
            progress_cb(idx + 1, total, v_file.name)

        orig_data = archive.read_entry_bytes(entry)
        try:
            new_chunk_data = rebuild_dgad_with_video(
                orig_data,
                v_file,
                stream_name=stream_name,
                target_bitrate=target_bitrate,
            )
            archive.replace_entry(entry.hash, new_chunk_data)
            injected_paths.append(entry.path)
        except Exception as err:
            print(f"Failed to inject {v_file.name} into {entry.path}: {err}")

    return injected_paths

