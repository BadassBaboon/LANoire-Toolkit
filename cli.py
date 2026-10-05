from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path
from PIL import Image

from core.archive_big import BigArchive
from core.archive_wad import WadArchive
from core.hash_db import HashDatabase
from core.trm_processor import TRMContainer
from core.dds_utils import (
    encode_image_to_dds,
    decode_dds_to_image,
    wrap_raw_dxt_to_dds,
    unwrap_dds_to_raw_dxt,
)


def cmd_list(args: argparse.Namespace) -> None:
    path = Path(args.archive)
    if not path.exists():
        print(f"Error: Archive {path} not found.")
        return

    if path.name.endswith(".wad.pc"):
        archive = WadArchive(path)
    else:
        archive = BigArchive(path)

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
        size_str = f"{entry.size}" if hasattr(entry, "size") else f"{entry.size1 + entry.size2}"
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

    if path.name.endswith(".wad.pc"):
        archive = WadArchive(path)
        print(f"Extracting {len(archive.entries)} files from {path.name} to {out_dir}...")
        archive.extract_all(out_dir, progress_cb=lambda i, total, name: print(f"[{i}/{total}] {name}"))
        print("\nExtraction complete!")
        return

    archive = BigArchive(path)
    filter_str = args.filter.lower() if args.filter else ""
    targets = [e for e in archive.entries if not filter_str or filter_str in e.path.lower()]
    print(f"Extracting {len(targets)} files matching '{filter_str}' to {out_dir}...")

    for i, e in enumerate(targets):
        raw_data = archive.read_entry_bytes(e, decompress=not args.raw)
        dest = out_dir / e.path
        dest.parent.mkdir(parents=True, exist_ok=True)

        extracted_tex = False

        if args.png or args.dds:
            # Check TRM for textures
            if raw_data[:4] == b"trM#":
                try:
                    trm = TRMContainer(raw_data)
                    face_set = trm.get_face_texture_set()
                    if face_set and face_set.textures:
                        for tex in face_set.textures:
                            role = tex.name.lower()
                            stem = dest.stem
                            if args.dds:
                                dds_path = dest.parent / f"{stem}_{role}.dds"
                                dds_path.write_bytes(tex.dds_bytes)
                                print(f"  -> Saved {dds_path.name}")
                            if args.png and tex.image:
                                png_path = dest.parent / f"{stem}_{role}.png"
                                tex.image.save(png_path)
                                print(f"  -> Saved {png_path.name}")
                        extracted_tex = True
                except Exception:
                    pass

            # Check standalone DDS
            if not extracted_tex and raw_data[:4] == b"DDS ":
                if args.dds:
                    dest_dds = dest.with_suffix(".dds")
                    dest_dds.write_bytes(raw_data)
                    print(f"  -> Saved {dest_dds.name}")
                if args.png:
                    try:
                        img = decode_dds_to_image(raw_data)
                        dest_png = dest.with_suffix(".png")
                        img.save(dest_png)
                        print(f"  -> Saved {dest_png.name}")
                    except Exception:
                        pass
                extracted_tex = True

            # Check raw reftexture segment
            if not extracted_tex:
                wrapped_dds, _ = wrap_raw_dxt_to_dds(raw_data)
                if wrapped_dds:
                    if args.dds:
                        dest_dds = dest.with_suffix(".dds")
                        dest_dds.write_bytes(wrapped_dds)
                        print(f"  -> Saved {dest_dds.name}")
                    if args.png:
                        try:
                            img = decode_dds_to_image(wrapped_dds)
                            dest_png = dest.with_suffix(".png")
                            img.save(dest_png)
                            print(f"  -> Saved {dest_png.name}")
                        except Exception:
                            pass
                    extracted_tex = True

        if not extracted_tex:
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
                print(f"Error processing {entry.path}: {e}")

    if found == 0:
        print(f"No matching head chunks found for '{target_name}'.")
    else:
        print(f"\nSuccessfully extracted face textures for {found} character(s) to {out_dir}!")


def cmd_inject_face(args: argparse.Namespace) -> None:
    archive_path = Path(args.characters_archive)
    head_query = args.head_chunk.lower()
    img_path = Path(args.texture_image)
    out_pc = Path(args.repack_out or archive_path)

    if not img_path.exists():
        print(f"Error: Upscaled image {img_path} not found.")
        return

    archive = BigArchive(archive_path)
    target_entry = None
    for e in archive.entries:
        if head_query in e.path.lower():
            target_entry = e
            break

    if not target_entry:
        print(f"Error: Entry matching '{head_query}' not found in {archive_path.name}.")
        return

    print(f"Found target entry: {target_entry.path}")
    raw_data = archive.read_entry_bytes(target_entry, decompress=True)

    # Determine replacement bytes
    if img_path.suffix.lower() == ".dds":
        new_dds_bytes = img_path.read_bytes()
    else:
        print(f"Encoding {img_path.name} to DXT DDS with mipmaps...")
        pil_img = Image.open(img_path)
        tex_idx = args.texture_index
        fourcc = "DXT1" if tex_idx in (0, 2) else "DXT5"
        new_dds_bytes = encode_image_to_dds(pil_img, fourcc=fourcc, generate_mips=True)

    if raw_data[:4] == b"trM#":
        trm = TRMContainer(raw_data)
        face_set = trm.get_face_texture_set()
        if not face_set:
            print("Error: Target TRM container does not contain Texture VRAM.")
            return

        print(f"Injecting into sub-texture slot {args.texture_index} (size {len(new_dds_bytes):,} bytes)...")
        face_set.replace_texture(args.texture_index, new_dds_bytes)
        trm.apply_face_texture_set(face_set)
        new_trm_bytes, new_s1, new_s2 = trm.build()
        archive.replace_entry(target_entry.hash, new_trm_bytes, size1=new_s1, size2=new_s2)
    else:
        # Check if reftexture segment
        _, orig_footer = wrap_raw_dxt_to_dds(raw_data)
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
    import re
    archive_path = Path(args.characters_archive)
    batch_dir = Path(args.batch_folder)
    out_pc = Path(args.repack_out or archive_path)

    if not batch_dir.exists():
        print(f"Error: Batch directory {batch_dir} does not exist.")
        return

    archive = BigArchive(archive_path)

    # Map head chunk stems to entries
    head_map = {}
    for entry in archive.entries:
        if "_head" in entry.path.lower():
            stem = Path(entry.path).stem.lower()
            head_map[stem] = entry

    image_exts = {".png", ".dds", ".jpg", ".tga"}
    image_files = [f for f in batch_dir.rglob("*") if f.suffix.lower() in image_exts]
    if not image_files:
        print(f"No image files found in {batch_dir}.")
        return

    print(f"Found {len(image_files)} potential texture files in {batch_dir}. Matching against archive head chunks...")

    updates_by_entry: dict[int, dict] = {}

    for img_file in image_files:
        filename = img_file.stem.lower()
        if "normal" in filename:
            slot_idx = 1
            fourcc = "DXT5"
        elif "specular" in filename or "spec" in filename:
            slot_idx = 2
            fourcc = "DXT1"
        else:
            slot_idx = 0
            fourcc = "DXT1"

        parent_stem = img_file.parent.name.lower()
        matched_stem = None
        if parent_stem in head_map:
            matched_stem = parent_stem
        else:
            clean_name = filename
            for tag in ["_diffuse", "_normal", "_specular", "_spec", "_dxt1", "_dxt5", "_raw"]:
                clean_name = clean_name.replace(tag, "")
            clean_name = re.sub(r"_\d+x\d+", "", clean_name)

            if clean_name in head_map:
                matched_stem = clean_name
            else:
                for stem in head_map:
                    if stem in filename or clean_name in stem:
                        matched_stem = stem
                        break

        if not matched_stem:
            print(f"  [Skip] Could not match {img_file.name} to a character head chunk.")
            continue

        entry = head_map[matched_stem]
        if entry.hash not in updates_by_entry:
            updates_by_entry[entry.hash] = {"entry": entry, "slots": {}}

        updates_by_entry[entry.hash]["slots"][slot_idx] = (img_file, fourcc)

    print(f"\nPrepared updates for {len(updates_by_entry)} character head entries.")

    if not updates_by_entry:
        print("Nothing to inject.")
        return

    applied_count = 0
    for entry_hash, update_info in updates_by_entry.items():
        entry = update_info["entry"]
        slots = update_info["slots"]

        raw_data = archive.read_entry_bytes(entry, decompress=True)
        if raw_data[:4] != b"trM#":
            continue

        try:
            trm = TRMContainer(raw_data)
            face_set = trm.get_face_texture_set()
            if not face_set:
                continue

            for slot_idx, (img_path, fourcc) in slots.items():
                if img_path.suffix.lower() == ".dds":
                    new_dds = img_path.read_bytes()
                else:
                    pil_img = Image.open(img_path)
                    new_dds = encode_image_to_dds(pil_img, fourcc=fourcc, generate_mips=True)

                face_set.replace_texture(slot_idx, new_dds)
                print(f"  -> Injected {img_path.name} into {entry.path} (slot {slot_idx})")

            trm.apply_face_texture_set(face_set)
            new_trm, s1, s2 = trm.build()
            archive.replace_entry(entry.hash, new_trm, size1=s1, size2=s2)
            applied_count += 1
        except Exception as e:
            print(f"Error applying updates to {entry.path}: {e}")

    print(f"\nSuccessfully applied textures to {applied_count} character heads.")
    print(f"Repacking archive to {out_pc}...")
    archive.repack(
        out_pc,
        compress=True,
        backup_original=True,
        progress_cb=lambda i, tot, name: print(f"[{i}/{tot}] Repacking...", end="\r"),
    )
    print(f"\nRepacking complete: {out_pc}")


def cmd_extract_face_video(args: argparse.Namespace) -> None:
    from core.dgad_video import batch_export_face_videos, export_face_video, find_ffmpeg

    path = Path(args.archive)
    if not path.is_file():
        print(f"Error: Archive {path} not found.")
        return

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
        print(f"Error: Archive {path} not found.")
        return

    video_p = Path(args.video_file)
    if not video_p.is_file():
        print(f"Error: Video file {video_p} not found.")
        return

    out_pc = Path(args.repack_out or path)
    archive = BigArchive(path)

    # Find matching chunk
    chunk_name = args.head_chunk.lower()
    target_entry = None
    for e in archive.entries:
        if e.path.startswith("intermediate/chunks/da/") and chunk_name in e.path.lower():
            target_entry = e
            break

    if not target_entry:
        print(f"Error: Could not find DA chunk matching '{args.head_chunk}' in {path.name}")
        return

    print(f"Injecting {video_p.name} into {target_entry.path}...")
    orig_chunk = archive.read_entry_bytes(target_entry)
    new_chunk = rebuild_dgad_with_video(
        orig_chunk,
        video_p,
        stream_name="color",
        target_bitrate=args.bitrate or "1500k",
    )
    archive.replace_entry(target_entry.path, new_chunk)
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
        print(f"Error: Archive {path} not found.")
        return

    folder = Path(args.batch_folder)
    if not folder.is_dir():
        print(f"Error: Folder {folder} not found.")
        return

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
        print(f"No matching face video files found in {folder}!")
        return

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
    p_inj.add_argument("--texture-index", "-i", type=int, default=0, help="0: Diffuse, 1: Normal, 2: Specular")
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

    args = parser.parse_args()

    if args.command == "list":
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


