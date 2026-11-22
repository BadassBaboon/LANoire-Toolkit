import random
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from cli import select_entry
from core.archive_big import BigArchive
from core.archive_wad import WadArchive
from core.dds_utils import encode_image_to_dds, parse_dds_header, wrap_raw_dxt_to_dds, unwrap_dds_to_raw_dxt
from core.dgad_video import demux_ogg_packets, make_ogg_page, rebuild_dgad_with_video, parse_dgad_chunk
from core.sges import compress_sges, decompress_sges, recompress_sges, sges_group_sizes
from core.trm_processor import FaceTextureSet, import_head_textures


def big_bytes():
    data = b"first" + b"X" * 11 + b"unindexed bytes!" + b"second"
    table = struct.pack("<2I10II", 3, 2, 1, 0, 5, 0, 0, 2, 2, 6, 0, 0, 52)
    return data + table


def wad_bytes(names):
    data_start = 8 + 12 * len(names)
    table = b"".join(struct.pack("<3I", i, data_start + i, 1) for i in range(len(names)))
    names_table = b"".join(struct.pack("<H", len(n)) + n for n in names)
    return b"WAD\x01" + struct.pack("<I", len(names)) + table + bytes(range(len(names))) + names_table


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_big_save_can_be_retried_after_failed_publish(self):
        source = self.root / "sample.big.pc"
        source.write_bytes(big_bytes())
        archive = BigArchive(source)
        archive.replace_entry(1, b"long replacement")
        publish = Path.replace

        def fail_destination(temp, destination):
            if destination == source:
                raise PermissionError("Archive is in use")
            return publish(temp, destination)

        with patch.object(Path, "replace", fail_destination), self.assertRaises(PermissionError):
            archive.repack(source)
        self.assertEqual(source.read_bytes(), big_bytes())
        self.assertEqual(archive.entries[0].custom_data, b"long replacement")
        self.assertEqual(list(self.root.glob("*.tmp")), [])
        archive.repack(source)
        self.assertEqual((self.root / "sample.big.pc.bak").read_bytes(), big_bytes())
        self.assertEqual(source.read_bytes()[16:32], b"unindexed bytes!")
        again = BigArchive(source)
        self.assertEqual([again.read_entry_bytes(e) for e in again.entries], [b"long replacement", b"second"])

    def test_wad_repack_tracks_new_offsets_and_keeps_first_backup(self):
        source = self.root / "sample.wad.pc"
        original = wad_bytes([b"first", b"second"])
        source.write_bytes(original)
        archive = WadArchive(source)
        archive.replace_entry(0, b"long replacement")
        archive.repack(source)
        self.assertEqual(archive.read_entry_bytes(archive.entries[1]), b"\x01")
        archive.replace_entry(1, b"another")
        archive.repack(source)
        self.assertEqual((self.root / "sample.wad.pc.bak").read_bytes(), original)
        again = WadArchive(source)
        self.assertEqual([again.read_entry_bytes(e) for e in again.entries], [b"long replacement", b"another"])

    def test_wad_extraction_stays_in_chosen_directory(self):
        source = self.root / "sample.wad.pc"
        source.write_bytes(wad_bytes([b"../outside"]))
        with self.assertRaises(ValueError):
            WadArchive(source).extract_all(self.root / "output")
        self.assertFalse((self.root / "outside").exists())

    def test_invalid_archives_cannot_be_repacked_as_empty(self):
        source = self.root / "sample.pc"
        source.write_bytes(b"unrecognized bytes")
        for archive in (BigArchive, WadArchive):
            with self.subTest(archive=archive), self.assertRaises(ValueError):
                archive(source)

    def test_single_entry_matching_and_conflicting_batch_inputs(self):
        source = self.root / "sample.big.pc"
        source.write_bytes(big_bytes())
        archive = BigArchive(source)
        one, two = archive.entries
        one.path, two.path = "x/head_head_wet.chunk", "x/head_head.chunk"
        self.assertIs(select_entry(archive.entries, "head_head"), two)
        self.assertIs(select_entry(archive.entries, "0x00000001"), one)
        with self.assertRaises(SystemExit):
            select_entry(archive.entries, "head")
        for ext in ("dds", "png"):
            (self.root / f"head_head_diffuse.{ext}").touch()
        with self.assertRaisesRegex(ValueError, "slot 0"):
            import_head_textures(archive, self.root)
        self.assertIsNone(two.custom_data)


class CodecTests(unittest.TestCase):
    def test_sges_separates_main_and_vram_on_resize(self):
        template = compress_sges(b"original")
        encoded = recompress_sges(b"main" + b"v" * 200000, template, 4, sequential=False)
        self.assertEqual(sges_group_sizes(encoded), [4, 200000])
        self.assertEqual(decompress_sges(encoded), b"main" + b"v" * 200000)
        with self.assertRaises(ValueError):
            recompress_sges(b"smaller", encoded, 7, sequential=True)

    def test_index_sizes_exclude_raw_chunk_padding(self):
        stored = struct.pack("<4sHH4xHBB", b"sges", 7, 1, 16, 0, 0) + b"asset" + bytes(11)
        self.assertEqual(decompress_sges(stored, (5, 0)), b"asset")

    def test_multi_page_sges_offsets_and_all_payloads(self):
        data = random.Random(17).randbytes(1100000)
        encoded = recompress_sges(data, compress_sges(b"old"), 131072, False)
        page = 512 * 1024
        for pos in (page, 2 * page):
            self.assertEqual(encoded[pos:pos + 4], b"sges")
            self.assertEqual(encoded[pos + 8], 2)
            main, vram = struct.unpack_from("<II", encoded, pos + 12)
            self.assertEqual(main, 131072)
            self.assertGreater(vram, 0)
        self.assertEqual(decompress_sges(encoded), data)
        self.assertEqual(sges_group_sizes(encoded), [131072, len(data) - 131072])

    def test_ogg_continuations_empty_frames_and_other_streams(self):
        header = b"\x80theora" + bytes(35)
        audio = make_ogg_page(2, 0, 10, 0, [b"audio"])
        video = make_ogg_page(2, 0, 11, 0, [header])
        large = b"x" * (255 * 255)
        pages = make_ogg_page(0, 64, 11, 1, [large])
        tail = make_ogg_page(4, 65, 11, 3, [b""])
        self.assertEqual(demux_ogg_packets(audio + video + pages + tail), [header, large, b""])

    def test_color_upscale_preserves_normal_resolution_and_motion(self):
        header = struct.pack("<4s12I", b"DGAD", 3, 0, 0, 0, 0, 0, 1, 30, 0, 80, 128, 512)
        frame = bytearray(208)
        struct.pack_into("<I4xI4xI", frame, 180, 4, 3, 5)
        source = header + struct.pack("<I", 220) + frame + b"meshnrmcolor"

        ident = bytearray(42)
        ident[:7] = b"\x80theora"
        ident[14:20] = (1024).to_bytes(3, "big") * 2
        struct.pack_into(">II", ident, 22, 30, 1)
        packets = [bytes(ident), b"\x81theora", b"\x82theora", b"\x00"]
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "color.ogv"
            video.write_bytes(make_ogg_page(2, 0, 123, 0, packets))
            result = parse_dgad_chunk(rebuild_dgad_with_video(source, video))
        self.assertEqual(result.raw_header_52[:48], header[:48])
        self.assertEqual(result.resolution, 1024)
        self.assertEqual(result.frames[0].stream2_normals, b"nrm")
        self.assertEqual(result.frames[0].stream1_mocap, b"mesh")

    def test_standard_texture_metadata_survives_replacement(self):
        dds = encode_image_to_dds(Image.new("RGB", (16, 16)))
        main = b"ptM#" + bytes(range(48))
        textures = FaceTextureSet(main, dds, is_unique=False)
        textures.replace_texture(0, dds)
        self.assertEqual(bytes(textures.raw_main), main)
        self.assertEqual(parse_dds_header(dds)["mipmaps"], 5)

    def test_reftexture_mip_groups_roundtrip(self):
        small, medium, large = bytes([1])*368, bytes([2])*1024, bytes([3])*4096
        raw = small + medium + large
        dds, layout = wrap_raw_dxt_to_dds(raw, [len(small), len(medium), len(large)])
        self.assertEqual(dds[128:], large + medium + small)
        self.assertEqual(unwrap_dds_to_raw_dxt(dds, layout), raw)
        self.assertEqual(wrap_raw_dxt_to_dds(raw), (None, None))
