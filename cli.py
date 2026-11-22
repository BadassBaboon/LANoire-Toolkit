from __future__ import annotations
import argparse
from pathlib import Path
from PIL import Image

from core.archive_big import BigArchive
from core import open_archive
from core.archive_io import extraction_path
from core.trm_processor import TRMContainer, import_head_textures
from core.dds_utils import (
    parse_dds_header,
    encode_image_to_dds,
    decode_dds_to_image,
    wrap_raw_dxt_to_dds,
    unwrap_dds_to_raw_dxt,
)


def cmd_list(args: argparse.Namespace) -> None:
    path = Path(args.archive)
    if not path.exists():
        raise SystemExit(f"Error: Archive {path} not found.")

    archive = open_archive(path)

    print(f"\nArchive: {path.name} ({len(archive.entries)} entries)")
    print(f"{'Hash':<12} {'Size':<10} {'Type':<6} {'Path'}")
    print("-" * 75)

    filter_text = args.filter.lower() if args.filter else ""
    matched = 0

    for entry in archive.entries:
        entry_path = entry.name if hasattr(entry, "name") else entry.path
        if filter_text and filter_text not in entry_path.lower():
            continue

        matched += 1
        size_str = f"{entry.size}" if hasattr(entry, "size") else f"{entry.size1 + entry.size2 or entry.compressed_size}"
        comp_str = "SGES" if getattr(entry, "is_compressed", False) else "RAW"
        print(f"0x{entry.hash:08x}   {size_str:<10} {comp_str:<6} {entry_path}")

        if args.limit and matched >= args.limit:
            print(f"... (truncated at {args.limit} entries)")
            break

    print(f"\nTotal shown: {matched} / {len(archive.entries)}")


def cmd_extract(args: argparse.Namespace) -> None:
    path = Path(args.archive)
    out_dir = Path(args.out or f"extracted_{path.stem}")
    out_dir.mkdir(parents=True, exist_ok=True)

    archive = open_archive(path)
    filter_str = args.filter.lower() if args.filter else ""
    targets = [e for e in archive.entries if not filter_str or filter_str in e.path.lower()]
    print(f"Extracting {len(targets)} files matching '{filter_str}' to {out_dir}...")

    for i, e in enumerate(targets):
        raw_data = (archive.read_entry_bytes(e, decompress=not args.raw) if isinstance(archive, BigArchive)
                    else archive.read_entry_bytes(e))
        dest = extraction_path(out_dir, e.path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        textures = []
        if args.png or args.dds:
            if raw_data.startswith(b"trM#"):
                face_set = TRMContainer(raw_data).get_face_texture_set()
                if face_set:
                    textures = [(dest.with_name(f"{dest.stem}_{t.name.lower()}.dds"), t.dds_bytes)
                                for t in face_set.textures]
            else:
                groups = archive.reftexture_groups(e) if isinstance(archive, BigArchive) and not args.raw else ()
                dds, _ = wrap_raw_dxt_to_dds(raw_data, groups)
                if dds:
                    textures.append((dest.with_suffix(".dds"), dds))

        if textures:
            for dds_path, dds in textures:
                if args.dds:
                    dds_path.write_bytes(dds)
                    print(f"  -> Saved {dds_path.name}")
                if args.png:
                    png_path = dds_path.with_suffix(".png")
                    decode_dds_to_image(dds).save(png_path)
                    print(f"  -> Saved {png_path.name}")
        else:
            dest.write_bytes(raw_data)
            print(f"[{i+1}/{len(targets)}] {e.path}")

    print("\nExtraction complete!")


def cmd_extract_face(args: argparse.Namespace) -> None:
    archive_path = Path(args.characters_archive)
    out_dir = Path(args.out or "extracted_faces")
    out_dir.mkdir(parents=True, exist_ok=True)

    archive = BigArchive(archive_path)
    target_name = args.character.lower()

    found = 0
    for entry in archive.entries:
        if "_head" in entry.path.lower() and (not target_name or target_name in entry.path.lower()):
            raw_trm = archive.read_entry_bytes(entry, decompress=True)
            if raw_trm[:4] != b"trM#":
                continue

            try:
                trm = TRMContainer(raw_trm)
                face_set = trm.get_face_texture_set()
                if not face_set:
                    continue

                stem = Path(entry.path).stem
                char_dir = out_dir / stem
                char_dir.mkdir(parents=True, exist_ok=True)

                print(f"\nExtracting face textures for: {stem}")
                for tex in face_set.textures:
                    fourcc = tex.info.get("fourcc", "UNKNOWN") if tex.info else "RAW"
                    w = tex.info.get("width", 0) if tex.info else 0
                    h = tex.info.get("height", 0) if tex.info else 0
                    role = tex.name.lower()

                    dds_path = char_dir / f"{stem}_{role}_{w}x{h}_{fourcc}.dds"
                    dds_path.write_bytes(tex.dds_bytes)
                    print(f"  -> Saved {dds_path.name}")

                    if tex.image:
                        png_path = char_dir / f"{stem}_{role}_{w}x{h}_{fourcc}.png"
                        tex.image.save(png_path)
                        print(f"  -> Saved {png_path.name}")

                found += 1
            except Exception as e:
                raise ValueError(f"Error processing {entry.path}: {e}") from e

    if found == 0:
        raise SystemExit(f"No matching head chunks found for '{target_name}'.")
    else:
        print(f"\nSuccessfully extracted face textures for {found} character(s) to {out_dir}!")


def select_entry(entries, query):
    key = query.lower()
    exact = [e for e in entries if key in (e.path.lower(), Path(e.path).stem.lower(), f"0x{e.hash:08x}")]
    matches = exact or [e for e in entries if key in e.path.lower()]
    if len(matches) != 1:
        raise SystemExit(f"{len(matches)} entries match {query!r}. Specify a unique name, path, or hash.")
    return matches[0]


def cmd_inject_face(args: argparse.Namespace) -> None:
    archive_path = Path(args.characters_archive)
    head_query = args.head_chunk.lower()
    img_path = Path(args.texture_image)
    out_pc = Path(args.repack_out or archive_path)

    if not img_path.exists():
        raise SystemExit(f"Error: Upscaled image {img_path} not found.")

    archive = BigArchive(archive_path)
    target_entry = select_entry(archive.entries, head_query)

    print(f"Found target entry: {target_entry.path}")
    raw_data = archive.read_entry_bytes(target_entry, decompress=True)

    # Determine replacement bytes
    if img_path.suffix.lower() == ".dds":
        new_dds_bytes = img_path.read_bytes()
        decode_dds_to_image(new_dds_bytes).load()
    else:
        print(f"Encoding {img_path.name} to DXT DDS with mipmaps...")
        pil_img = Image.open(img_path)
        tex_idx = args.texture_index
        fourcc = "DXT1" if tex_idx in (0, 2) else "DXT5"
        if raw_data[:4] != b"trM#":
            wrapped, _ = wrap_raw_dxt_to_dds(raw_data, archive.reftexture_groups(target_entry))
            if wrapped:
                fourcc = parse_dds_header(wrapped)["fourcc"]
        new_dds_bytes = encode_image_to_dds(pil_img, fourcc=fourcc, generate_mips=True)

    if raw_data[:4] == b"trM#":
        trm = TRMContainer(raw_data)
        face_set = trm.get_face_texture_set()
        if not face_set:
            raise SystemExit("Error: Target TRM container does not contain Texture VRAM.")

        print(f"Injecting into sub-texture slot {args.texture_index} (size {len(new_dds_bytes):,} bytes)...")
        face_set.replace_texture(args.texture_index, new_dds_bytes)
        trm.apply_face_texture_set(face_set)
        new_trm_bytes, new_s1, new_s2 = trm.build()
        archive.replace_entry(target_entry.hash, new_trm_bytes, size1=new_s1, size2=new_s2)
    else:
        # Check if reftexture segment
        wrapped, orig_footer = wrap_raw_dxt_to_dds(raw_data, archive.reftexture_groups(target_entry))
        if not wrapped:
            raise ValueError("Selected entry is not a recognized texture")
        if orig_footer:
            raw_payload = unwrap_dds_to_raw_dxt(new_dds_bytes, orig_footer)
            archive.replace_entry(target_entry.hash, raw_payload)
        else:
            archive.replace_entry(target_entry.hash, new_dds_bytes)

    print(f"Repacking archive to {out_pc}...")
    archive.repack(
        out_pc,
        compress=True,
        backup_original=True,
        progress_cb=lambda i, total, name: print(f"[{i}/{total}] Repacking...", end="\r"),
    )
    print(f"\nSuccessfully injected texture and repacked {out_pc}!")


def cmd_batch_inject_faces(args: argparse.Namespace) -> None:
    archive_path = Path(args.characters_archive)
    out_pc = Path(args.repack_out or archive_path)
    folder = Path(args.batch_folder)
    if not folder.is_dir():
        raise SystemExit(f"Batch directory does not exist: {folder}")
    archive = BigArchive(archive_path)
    applied_count = import_head_textures(archive, folder)
    if not applied_count:
        raise SystemExit("No matching head textures were found.")
    print(f"\nSuccessfully applied textures to {applied_count} character heads.")
    print(f"Repacking archive to {out_pc}...")
    archive.repack(out_pc)
    print(f"\nRepacking complete: {out_pc}")


def cmd_extract_face_video(args: argparse.Namespace) -> None:
    from core.dgad_video import batch_export_face_videos, find_ffmpeg

    path = Path(args.archive)
    if not path.is_file():
        raise SystemExit(f"Error: Archive {path} not found.")

    out_dir = Path(args.out or f"extracted_videos_{path.stem}")
    out_dir.mkdir(parents=True, exist_ok=True)

    archive = BigArchive(path)
    fmt = args.format.lower()
    if fmt == "mp4" and not find_ffmpeg():
        print("Warning: FFmpeg not detected in PATH or standard directories. Defaulting to .ogv output.")
        fmt = "ogv"

    print(f"Exporting MotionScan face videos from {path.name} to {out_dir} (Format: .{fmt})...")
    count = batch_export_face_videos(
        archive,
        out_dir,
        character_filter=args.character or "",
        export_format=fmt,
        export_normals=args.normals,
        progress_cb=lambda i, tot, name: print(f"[{i}/{tot}] Exporting: {name}"),
    )
    print(f"\nExtraction complete: {count} face video(s) exported to {out_dir}")


def cmd_inject_face_video(args: argparse.Namespace) -> None:
    from core.dgad_video import rebuild_dgad_with_video

    path = Path(args.archive)
    if not path.is_file():
        raise SystemExit(f"Error: Archive {path} not found.")

    video_p = Path(args.video_file)
    if not video_p.is_file():
        raise SystemExit(f"Error: Video file {video_p} not found.")

    out_pc = Path(args.repack_out or path)
    archive = BigArchive(path)

    # Find matching chunk
    chunk_name = args.head_chunk.lower()
    videos = [e for e in archive.entries if e.magic == b"DGAD" or "/da/" in e.path.lower()]
    target_entry = select_entry(videos, chunk_name)

    print(f"Injecting {video_p.name} into {target_entry.path}...")
    orig_chunk = archive.read_entry_bytes(target_entry)
    new_chunk = rebuild_dgad_with_video(
        orig_chunk,
        video_p,
        stream_name="color",
        target_bitrate=args.bitrate or "1500k",
    )
    archive.replace_entry(target_entry.hash, new_chunk)
    print(f"Updated {target_entry.path} in memory ({len(orig_chunk)} -> {len(new_chunk)} bytes).")

    print(f"Repacking archive to {out_pc}...")
    archive.repack(
        out_pc,
        compress=True,
        backup_original=True,
        progress_cb=lambda i, tot, name: print(f"[{i}/{tot}] Repacking...", end="\r"),
    )
    print(f"\nRepacking complete: {out_pc}")


def cmd_batch_inject_face_videos(args: argparse.Namespace) -> None:
    from core.dgad_video import batch_import_face_videos

    path = Path(args.archive)
    if not path.is_file():
        raise SystemExit(f"Error: Archive {path} not found.")

    folder = Path(args.batch_folder)
    if not folder.is_dir():
        raise SystemExit(f"Error: Folder {folder} not found.")

    out_pc = Path(args.repack_out or path)
    archive = BigArchive(path)

    print(f"Scanning {folder} and injecting face videos into {path.name}...")
    injected = batch_import_face_videos(
        archive,
        folder,
        stream_name="color",
        target_bitrate=args.bitrate or "1500k",
        progress_cb=lambda i, tot, name: print(f"[{i}/{tot}] Processing: {name}"),
    )

    if not injected:
        raise SystemExit(f"No matching face video files found in {folder}")

    print(f"\nSuccessfully injected {len(injected)} face videos into archive.")
    print(f"Repacking archive to {out_pc}...")
    archive.repack(
        out_pc,
        compress=True,
        backup_original=True,
        progress_cb=lambda i, tot, name: print(f"[{i}/{tot}] Repacking...", end="\r"),
    )
    print(f"\nRepacking complete: {out_pc}")


def main():
    parser = argparse.ArgumentParser(description="L.A. Noire PC Toolkit CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = subparsers.add_parser("list", help="List archive contents")
    p_list.add_argument("archive", help="Path to .big.pc or .wad.pc")
    p_list.add_argument("--filter", "-f", help="Substring filter on path")
    p_list.add_argument("--limit", "-l", type=int, default=0, help="Limit output rows")

    # extract
    p_ext = subparsers.add_parser("extract", help="Extract archive contents")
    p_ext.add_argument("archive", help="Path to .big.pc or .wad.pc")
    p_ext.add_argument("--out", "-o", help="Output directory")
    p_ext.add_argument("--filter", "-f", help="Filter substring")
    p_ext.add_argument("--png", action="store_true", help="Export textures directly as PNG images")
    p_ext.add_argument("--dds", action="store_true", help="Export textures directly as DDS files")
    p_ext.add_argument("--raw", action="store_true", help="Keep SGES compression raw")

    # extract-face
    p_face = subparsers.add_parser("extract-face", help="Extract character face textures directly to PNG/DDS")
    p_face.add_argument("characters_archive", help="Path to characters.big.pc")
    p_face.add_argument("character", nargs="?", default="phelps", help="Character name substring (default: phelps)")
    p_face.add_argument("--out", "-o", help="Output directory")

    # inject-face / inject-tex
    p_inj = subparsers.add_parser("inject-face", help="Inject upscaled texture into a character head/entry and repack")
    p_inj.add_argument("characters_archive", help="Path to archive (e.g. characters.big.pc)")
    p_inj.add_argument("head_chunk", help="Chunk name or substring (e.g. cole_phelps_head_new)")
    p_inj.add_argument("texture_image", help="Path to edited PNG or DDS image")
    p_inj.add_argument("--texture-index", "-i", type=int, choices=(0, 1, 2), default=0, help="0: Diffuse, 1: Normal, 2: Specular")
    p_inj.add_argument("--repack-out", "-r", help="Output .big.pc path (default: overwrite with backup)")

    # batch-inject-faces
    p_binj = subparsers.add_parser("batch-inject-faces", help="Batch inject folder of upscaled face textures into characters.big.pc")
    p_binj.add_argument("characters_archive", help="Path to characters.big.pc")
    p_binj.add_argument("batch_folder", help="Directory containing upscaled PNG or DDS images")
    p_binj.add_argument("--repack-out", "-r", help="Output .big.pc path (default: overwrite with backup)")

    # extract-face-video
    p_v_ext = subparsers.add_parser("extract-face-video", help="Extract MotionScan face videos to MP4 or OGV")
    p_v_ext.add_argument("archive", help="Path to .big.pc (e.g. cases_1_1.big.pc or da.big.pc)")
    p_v_ext.add_argument("character", nargs="?", default="", help="Character filter or chunk name (e.g. phelps)")
    p_v_ext.add_argument("--out", "-o", help="Output directory (default: extracted_videos_<archive>)")
    p_v_ext.add_argument("--format", default="mp4", choices=["mp4", "ogv"], help="Export format (default: mp4)")
    p_v_ext.add_argument("--normals", action="store_true", help="Also export facial normal-map video streams")

    # inject-face-video
    p_v_inj = subparsers.add_parser("inject-face-video", help="Inject upscaled video into a DGAD chunk and repack")
    p_v_inj.add_argument("archive", help="Path to .big.pc")
    p_v_inj.add_argument("head_chunk", help="Chunk name or substring (e.g. idle_phelps_neutral_1)")
    p_v_inj.add_argument("video_file", help="Path to upscaled .mp4 or .ogv video file")
    p_v_inj.add_argument("--bitrate", default="1500k", help="Theora re-encoding bitrate (default: 1500k)")
    p_v_inj.add_argument("--repack-out", "-r", help="Output .big.pc path (default: overwrite with backup)")

    # batch-inject-face-videos
    p_v_binj = subparsers.add_parser("batch-inject-face-videos", help="Batch inject folder of upscaled face videos into .big.pc")
    p_v_binj.add_argument("archive", help="Path to .big.pc")
    p_v_binj.add_argument("batch_folder", help="Directory containing upscaled .mp4 or .ogv video files")
    p_v_binj.add_argument("--bitrate", default="1500k", help="Theora re-encoding bitrate (default: 1500k)")
    p_v_binj.add_argument("--repack-out", "-r", help="Output .big.pc path (default: overwrite with backup)")

    p_repack = subparsers.add_parser("repack", help="Repack a BIG or WAD archive")
    p_repack.add_argument("archive")
    p_repack.add_argument("output")

    args = parser.parse_args()

    try:
        dispatch(args)
    except (OSError, ValueError, RuntimeError, IndexError) as err:
        parser.exit(1, f"Error: {err}\n")


def dispatch(args):
    if args.command == "repack":
        source = Path(args.archive)
        archive = open_archive(source)
        print(archive.repack(args.output))
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "extract":
        cmd_extract(args)
    elif args.command == "extract-face":
        cmd_extract_face(args)
    elif args.command == "inject-face":
        cmd_inject_face(args)
    elif args.command == "batch-inject-faces":
        cmd_batch_inject_faces(args)
    elif args.command == "extract-face-video":
        cmd_extract_face_video(args)
    elif args.command == "inject-face-video":
        cmd_inject_face_video(args)
    elif args.command == "batch-inject-face-videos":
        cmd_batch_inject_face_videos(args)


if __name__ == "__main__":
    main()


