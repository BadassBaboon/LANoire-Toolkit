from __future__ import annotations
import os
import sys
import threading
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import ttkbootstrap as tb
from ttkbootstrap.constants import *
from PIL import Image, ImageTk

# Ensure core is importable
toolkit_dir = Path(__file__).resolve().parent.parent
if str(toolkit_dir) not in sys.path:
    sys.path.insert(0, str(toolkit_dir))

from core.archive_big import BigArchive, BigArchiveEntry
from core.archive_wad import WadArchive
from core.hash_db import HashDatabase
from core.trm_processor import TRMContainer, FaceTextureSet
from core.dds_utils import (
    decode_dds_to_image,
    encode_image_to_dds,
    parse_dds_header,
    wrap_raw_dxt_to_dds,
    unwrap_dds_to_raw_dxt,
)
from core.dgad_video import (
    parse_dgad_chunk,
    export_face_video,
    rebuild_dgad_with_video,
    extract_first_frame_image,
    batch_export_face_videos,
    batch_import_face_videos,
    find_ffmpeg,
)


class LANoireToolkitApp(tb.Window):
    def __init__(self):
        super().__init__(
            title="L.A. Noire PC Toolkit",
            themename="darkly",
            size=(1320, 840),
            minsize=(960, 600),
        )

        self.hash_db = HashDatabase.get_instance()
        self.archive: Optional[BigArchive | WadArchive] = None
        self.current_entry: Optional[BigArchiveEntry] = None

        # Texture state for currently inspected entry
        self.current_trm: Optional[TRMContainer] = None
        self.current_face_set: Optional[FaceTextureSet] = None
        self.current_standalone_dds: Optional[bytes] = None
        self.current_ref_footer: Optional[bytes] = None
        self.active_dds_bytes: Optional[bytes] = None
        self.active_image: Optional[Image.Image] = None
        self.preview_image_ref: Optional[ImageTk.PhotoImage] = None

        # MotionScan video state
        self.current_dgad_raw: Optional[bytes] = None
        self.current_dgad_info = None

        self.filtered_entries: list[BigArchiveEntry] = []

        self._build_ui()

    def _build_ui(self):
        # Row 1: Operations Toolbar
        toolbar = tb.Frame(self, padding=(8, 6))
        toolbar.pack(fill=X, side=TOP)

        tb.Button(
            toolbar,
            text="Open Archive...",
            bootstyle="primary",
            command=self._on_open_archive_dialog,
        ).pack(side=LEFT, padx=3)

        self.btn_deploy = tb.Button(
            toolbar,
            text="Save (In-Place .bak)",
            bootstyle="warning",
            state=DISABLED,
            command=self._on_deploy_to_game,
        )
        self.btn_deploy.pack(side=LEFT, padx=3)

        self.btn_repack = tb.Button(
            toolbar,
            text="Repack as New...",
            bootstyle="success",
            state=DISABLED,
            command=self._on_repack_archive,
        )
        self.btn_repack.pack(side=LEFT, padx=3)

        tb.Separator(toolbar, orient=VERTICAL).pack(side=LEFT, padx=6, fill=Y)

        self.btn_batch_export_video = tb.Button(
            toolbar,
            text="Batch Export Videos...",
            bootstyle="outline-warning",
            state=DISABLED,
            command=self._on_batch_export_face_videos,
        )
        self.btn_batch_export_video.pack(side=LEFT, padx=3)

        self.btn_batch_import_video = tb.Button(
            toolbar,
            text="Batch Import Videos...",
            bootstyle="outline-success",
            state=DISABLED,
            command=self._on_batch_import_face_videos,
        )
        self.btn_batch_import_video.pack(side=LEFT, padx=3)

        tb.Separator(toolbar, orient=VERTICAL).pack(side=LEFT, padx=6, fill=Y)

        self.btn_batch_inject = tb.Button(
            toolbar,
            text="Batch Textures (Heads)...",
            bootstyle="outline-primary",
            state=DISABLED,
            command=self._on_batch_inject_folder,
        )
        self.btn_batch_inject.pack(side=LEFT, padx=3)

        # Row 2: Search & Filter Bar (Dedicated full-width row)
        filter_bar = tb.Frame(self, padding=(8, 4))
        filter_bar.pack(fill=X, side=TOP)

        tb.Label(filter_bar, text="Search:", font=("Segoe UI", 9, "bold")).pack(side=LEFT, padx=(4, 2))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *args: self._apply_filter())
        self.search_entry = tb.Entry(filter_bar, textvariable=self.search_var, width=28)
        self.search_entry.pack(side=LEFT, padx=2)

        tb.Button(
            filter_bar,
            text="X",
            bootstyle="secondary-outline",
            width=2,
            command=self._clear_search,
        ).pack(side=LEFT, padx=(0, 8))

        tb.Separator(filter_bar, orient=VERTICAL).pack(side=LEFT, padx=6, fill=Y)

        tb.Label(filter_bar, text="Filter:", font=("Segoe UI", 9, "bold")).pack(side=LEFT, padx=(4, 2))
        self.filter_category = tb.Combobox(
            filter_bar,
            values=["All Files", "MotionScan (DGAD)", "Heads/Faces", "Textures", "Models", "Audio", "Unknown/Hashed"],
            state="readonly",
            width=18,
        )
        self.filter_category.set("All Files")
        self.filter_category.pack(side=LEFT, padx=4)
        self.filter_category.bind("<<ComboboxSelected>>", lambda e: self._apply_filter())

        self.count_label = tb.Label(
            filter_bar,
            text="No archive loaded",
            font=("Segoe UI", 9),
            bootstyle="secondary",
        )
        self.count_label.pack(side=RIGHT, padx=8)

        # Status Bar
        self.status_bar = tb.Frame(self, padding=(8, 4))
        self.status_bar.pack(fill=X, side=BOTTOM)

        self.status_label = tb.Label(self.status_bar, text="Ready. Open an archive to begin.")
        self.status_label.pack(side=LEFT)

        self.progress_bar = tb.Progressbar(self.status_bar, mode="determinate", length=220, bootstyle="info-striped")
        self.progress_bar.pack(side=RIGHT, padx=8)

        # Main Paned Window
        main_paned = tb.Panedwindow(self, orient=HORIZONTAL)
        main_paned.pack(fill=BOTH, expand=True, padx=8, pady=4)

        # Left: File list
        left_frame = tb.Frame(main_paned)
        main_paned.add(left_frame, weight=3)

        # Treeview
        columns = ("name", "size", "format", "hash")
        self.tree = ttk.Treeview(left_frame, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("name", text="File Path")
        self.tree.heading("size", text="Size")
        self.tree.heading("format", text="Format")
        self.tree.heading("hash", text="Hash")

        self.tree.column("name", width=330, anchor=W)
        self.tree.column("size", width=80, anchor=E)
        self.tree.column("format", width=110, anchor=CENTER)
        self.tree.column("hash", width=95, anchor=CENTER)

        tree_scroll_y = tb.Scrollbar(left_frame, orient=VERTICAL, command=self.tree.yview)
        tree_scroll_x = tb.Scrollbar(left_frame, orient=HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=tree_scroll_y.set, xscrollcommand=tree_scroll_x.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll_y.grid(row=0, column=1, sticky="ns")
        tree_scroll_x.grid(row=1, column=0, sticky="ew")

        left_frame.rowconfigure(0, weight=1)
        left_frame.columnconfigure(0, weight=1)

        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # Context Menu for Treeview
        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="Export as PNG", command=self._on_export_active_as_png)
        self.context_menu.add_command(label="Export as DDS", command=self._on_export_active_as_dds)
        self.context_menu.add_command(label="Export Face Video (.mp4)", command=lambda: self._on_export_active_face_video("mp4"))
        self.context_menu.add_command(label="Export Face Video (.ogv)", command=lambda: self._on_export_active_face_video("ogv"))
        self.context_menu.add_command(label="Extract Raw Chunk", command=self._on_extract_selected)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Inject Texture (PNG/DDS)...", command=self._on_inject_upscaled_texture)
        self.context_menu.add_command(label="Inject Face Video (.mp4/.ogv)...", command=self._on_inject_face_video)
        self.tree.bind("<Button-3>", self._on_tree_right_click)

        # Action buttons below tree
        tree_actions = tb.Frame(left_frame, padding=(0, 4))
        tree_actions.grid(row=2, column=0, columnspan=2, sticky="ew")

        # Row 1: Selected entry actions
        actions_r1 = tb.Frame(tree_actions)
        actions_r1.pack(fill=X, side=TOP, pady=(0, 3))
        tb.Button(actions_r1, text="Export PNG", bootstyle="outline-success", command=self._on_export_active_as_png).pack(side=LEFT, padx=2)
        tb.Button(actions_r1, text="Export DDS", bootstyle="outline-info", command=self._on_export_active_as_dds).pack(side=LEFT, padx=2)
        tb.Button(actions_r1, text="Export Video", bootstyle="outline-warning", command=lambda: self._on_export_active_face_video("mp4")).pack(side=LEFT, padx=2)
        tb.Button(actions_r1, text="Extract Raw", bootstyle="outline-secondary", command=self._on_extract_selected).pack(side=LEFT, padx=2)

        # Row 2: Bulk extraction actions
        actions_r2 = tb.Frame(tree_actions)
        actions_r2.pack(fill=X, side=TOP)
        tb.Button(actions_r2, text="Extract All Files...", bootstyle="outline-dark", command=self._on_extract_all).pack(side=LEFT, padx=2)
        tb.Button(actions_r2, text="Extract All Faces (PNG)...", bootstyle="outline-warning", command=self._on_extract_all_faces).pack(side=LEFT, padx=2)

        # Right: Inspector & Preview
        right_frame = tb.Frame(main_paned, padding=8)
        main_paned.add(right_frame, weight=4)

        # Inspector notebook (tabs)
        self.notebook = tb.Notebook(right_frame)
        self.notebook.pack(fill=BOTH, expand=True)

        # Tab 1: Texture / Face Viewer
        self.tab_texture = tb.Frame(self.notebook, padding=8)
        self.notebook.add(self.tab_texture, text="Texture & Face Preview")

        # Action bar at top of Texture Tab
        tex_action_bar = tb.Frame(self.tab_texture)
        tex_action_bar.pack(fill=X, side=TOP, pady=(0, 6))

        self.btn_export_png = tb.Button(
            tex_action_bar,
            text="Export PNG",
            bootstyle="success",
            command=self._on_export_active_as_png,
        )
        self.btn_export_png.pack(side=LEFT, padx=3)

        self.btn_export_dds = tb.Button(
            tex_action_bar,
            text="Export DDS",
            bootstyle="info",
            command=self._on_export_active_as_dds,
        )
        self.btn_export_dds.pack(side=LEFT, padx=3)

        self.btn_export_video_mp4 = tb.Button(
            tex_action_bar,
            text="Export MP4",
            bootstyle="success",
            command=lambda: self._on_export_active_face_video("mp4"),
        )

        self.btn_export_video_ogv = tb.Button(
            tex_action_bar,
            text="Export OGV",
            bootstyle="outline-success",
            command=lambda: self._on_export_active_face_video("ogv"),
        )

        self.btn_replace_tex = tb.Button(
            tex_action_bar,
            text="Inject Texture...",
            bootstyle="primary",
            command=self._on_inject_upscaled_texture,
        )
        self.btn_replace_tex.pack(side=RIGHT, padx=3)

        self.btn_inject_video = tb.Button(
            tex_action_bar,
            text="Inject Video...",
            bootstyle="primary",
            command=self._on_inject_face_video,
        )

        # Multi-Texture Sub-Selector Frame (Diffuse, Normal, Specular, etc.)
        self.subtex_frame = tb.Labelframe(self.tab_texture, text="Texture Slots in Container", padding=8)
        self.subtex_frame.pack(fill=X, side=TOP, pady=(0, 6))

        self.selected_subtex_var = tk.IntVar(value=0)
        self.radio_buttons: list[tb.Radiobutton] = []

        # Image Canvas / Display
        self.image_canvas_frame = tb.Frame(self.tab_texture)
        self.image_canvas_frame.pack(fill=BOTH, expand=True)

        self.image_label = tb.Label(self.image_canvas_frame, anchor=CENTER)
        self.image_label.pack(fill=BOTH, expand=True)

        self.tex_info_label = tb.Label(
            self.tab_texture,
            text="Select a character head or texture chunk to view.",
            anchor=CENTER,
            font=("Segoe UI", 10, "bold"),
        )
        self.tex_info_label.pack(fill=X, side=BOTTOM, pady=4)

        # Tab 2: Raw / Hex / Metadata
        self.tab_hex = tb.Frame(self.notebook, padding=8)
        self.notebook.add(self.tab_hex, text="Raw Metadata & Hex")

        hex_scroll_y = tb.Scrollbar(self.tab_hex, orient=VERTICAL)
        hex_scroll_y.pack(side=RIGHT, fill=Y)

        hex_scroll_x = tb.Scrollbar(self.tab_hex, orient=HORIZONTAL)
        hex_scroll_x.pack(side=BOTTOM, fill=X)

        self.hex_text = tk.Text(
            self.tab_hex,
            bg="#1a1a1a",
            fg="#00ffcc",
            font=("Consolas", 10),
            wrap="none",
            yscrollcommand=hex_scroll_y.set,
            xscrollcommand=hex_scroll_x.set,
        )
        hex_scroll_y.config(command=self.hex_text.yview)
        hex_scroll_x.config(command=self.hex_text.xview)
        self.hex_text.pack(side=LEFT, fill=BOTH, expand=True)

    def _clear_search(self):
        self.search_var.set("")

    def _on_open_archive_dialog(self):
        path = filedialog.askopenfilename(
            title="Open L.A. Noire Archive",
            filetypes=[("L.A. Noire Archive (*.pc)", "*.pc"), ("All Files (*.*)", "*.*")],
        )
        if path:
            self.load_archive(Path(path))

    def load_archive(self, path: Path):
        self.status_label.config(text=f"Loading {path.name}...")
        self.update_idletasks()

        if path.name.endswith(".wad.pc"):
            self.archive = WadArchive(path)
        else:
            self.archive = BigArchive(path)

        self.btn_repack.config(state=NORMAL)
        self.btn_deploy.config(state=NORMAL)
        self.btn_batch_inject.config(state=NORMAL)
        self.btn_batch_import_video.config(state=NORMAL)
        self.btn_batch_export_video.config(state=NORMAL)
        self._apply_filter()
        self.status_label.config(text=f"Loaded {path.name} ({len(self.archive.entries)} entries).")

    def _apply_filter(self):
        if not self.archive:
            self.count_label.config(text="No archive loaded")
            return

        query = self.search_var.get().lower().strip()
        cat = self.filter_category.get()

        self.tree.delete(*self.tree.get_children())
        self.filtered_entries.clear()

        total_entries = len(self.archive.entries)

        for entry in self.archive.entries:
            path_str = entry.name if hasattr(entry, "name") else entry.path
            path_lower = path_str.lower()
            magic = getattr(entry, "magic", b"")
            fmt_lbl = getattr(entry, "format_label", "")
            h_str = f"0x{entry.hash:08x}"

            # Category filtering
            if cat == "MotionScan (DGAD)":
                if magic != b"DGAD" and "da/" not in path_lower and fmt_lbl != "DGAD (Face)":
                    continue
            elif cat == "Heads/Faces":
                if "_head" not in path_lower:
                    continue
            elif cat == "Textures":
                if not (magic == b"DDS " or any(k in path_lower for k in ("texture", "reftexture", ".dds", "diffuse", "segments/"))):
                    continue
            elif cat == "Models":
                if not (magic.startswith(b"trM") or any(k in path_lower for k in (".chunk", ".trm", "component"))):
                    continue
            elif cat == "Audio":
                if not (magic == b"FSB4" or any(k in path_lower for k in ("audio", ".mp3", ".ogg", "fsb"))):
                    continue
            elif cat == "Unknown/Hashed":
                if not ("unknown/" in path_lower or path_lower.startswith("0x") or path_lower.endswith(".bin")):
                    continue

            # Text search (searches path, hash, and format)
            if query:
                if query not in path_lower and query not in h_str.lower() and query not in fmt_lbl.lower():
                    continue

            self.filtered_entries.append(entry)

            sz = entry.size if hasattr(entry, "size") else (entry.size1 + entry.size2)
            self.tree.insert("", END, values=(path_str, f"{sz:,}", fmt_lbl, h_str))

        self.count_label.config(text=f"Showing {len(self.filtered_entries):,} of {total_entries:,} entries")

    def _on_tree_right_click(self, event):
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self.tree.selection_set(row_id)
            self._on_tree_select(None)
            self.context_menu.post(event.x_root, event.y_root)

    def _on_tree_select(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        item_idx = self.tree.index(sel[0])
        if item_idx >= len(self.filtered_entries):
            return

        self.current_entry = self.filtered_entries[item_idx]
        self._inspect_entry(self.current_entry)

    def _inspect_entry(self, entry: BigArchiveEntry):
        raw_data = self.archive.read_entry_bytes(entry, decompress=True)

        # Update Hex tab
        hex_preview = self._format_hex(raw_data[:1024])
        self.hex_text.delete("1.0", END)
        self.hex_text.insert(END, f"File: {entry.path}\nHash: 0x{entry.hash:08x}\nSize: {len(raw_data):,} bytes\n\n{hex_preview}")

        # Reset texture & video states
        self.current_face_set = None
        self.current_trm = None
        self.current_standalone_dds = None
        self.current_ref_footer = None
        self.active_dds_bytes = None
        self.active_image = None
        self.current_dgad_raw = None
        self.current_dgad_info = None

        has_texture = False
        is_dgad = False

        # Case 1: TRM model (Head, Character component, Prop, Vehicle, UI Streamed)
        if raw_data[:4] == b"trM#":
            try:
                self.current_trm = TRMContainer(raw_data)
                self.current_face_set = self.current_trm.get_face_texture_set()
                if self.current_face_set and self.current_face_set.textures:
                    has_texture = True
                    self._setup_subtex_radios()
                    self._update_face_preview()
            except Exception:
                pass

        # Case 2: Standalone DDS
        if not has_texture and raw_data[:4] == b"DDS ":
            self.current_standalone_dds = raw_data
            self.active_dds_bytes = raw_data
            try:
                self.active_image = decode_dds_to_image(raw_data)
                has_texture = True
                self.subtex_frame.pack_forget()
                self._display_image(self.active_image)
                info = parse_dds_header(raw_data)
                w, h = self.active_image.size
                fourcc = info.get("fourcc", "DDS")
                mips = info.get("mipmaps", 1)
                self.tex_info_label.config(text=f"DDS Texture | {w}x{h} {fourcc} | Mips: {mips} | Bytes: {len(raw_data):,}")
            except Exception:
                pass

        # Case 3: Raw reftexture segment
        if not has_texture:
            wrapped_dds, footer = wrap_raw_dxt_to_dds(raw_data)
            if wrapped_dds:
                self.current_standalone_dds = wrapped_dds
                self.current_ref_footer = footer
                self.active_dds_bytes = wrapped_dds
                try:
                    self.active_image = decode_dds_to_image(wrapped_dds)
                    has_texture = True
                    self.subtex_frame.pack_forget()
                    self._display_image(self.active_image)
                    info = parse_dds_header(wrapped_dds)
                    w, h = self.active_image.size
                    fourcc = info.get("fourcc", "DDS")
                    mips = info.get("mipmaps", 1)
                    self.tex_info_label.config(text=f"RefTexture (Auto-Wrapped) | {w}x{h} {fourcc} | Mips: {mips} | Bytes: {len(raw_data):,}")
                except Exception:
                    pass

        # Case 4: MotionScan DGAD facial performance chunk
        if not has_texture and len(raw_data) >= 52 and raw_data[:4] == b"DGAD":
            try:
                dgad = parse_dgad_chunk(raw_data)
                self.current_dgad_raw = raw_data
                self.current_dgad_info = dgad
                is_dgad = True
                has_texture = True
                self.subtex_frame.pack_forget()

                preview_img = extract_first_frame_image(raw_data)
                if preview_img:
                    self.active_image = preview_img
                    self._display_image(preview_img)
                else:
                    self.image_label.config(image="", text="MotionScan Facial Performance Video\n(Preview requires FFmpeg)")
                    self.preview_image_ref = None

                dur = len(dgad.frames) / (dgad.fps or 30)
                self.tex_info_label.config(
                    text=f"MotionScan Face Video | {dgad.resolution}x{dgad.resolution} @ {dgad.fps} FPS | "
                         f"Frames: {len(dgad.frames)} ({dur:.2f}s) | Normals: {dgad.macroblock_scale*2}x{dgad.macroblock_scale*2} | "
                         f"Size: {len(raw_data):,} bytes"
                )
            except Exception as e:
                print(f"Error inspecting DGAD: {e}")

        if is_dgad:
            self.notebook.select(self.tab_texture)
            # Show video controls, hide static texture controls
            self.btn_export_png.pack_forget()
            self.btn_export_dds.pack_forget()
            self.btn_replace_tex.pack_forget()
            self.btn_export_video_mp4.pack(side=LEFT, padx=3)
            self.btn_export_video_ogv.pack(side=LEFT, padx=3)
            self.btn_inject_video.pack(side=RIGHT, padx=3)
            self.btn_export_video_mp4.config(state=NORMAL)
            self.btn_export_video_ogv.config(state=NORMAL)
            self.btn_inject_video.config(state=NORMAL)
        elif has_texture:
            self.notebook.select(self.tab_texture)
            # Show static texture controls, hide video controls
            self.btn_export_video_mp4.pack_forget()
            self.btn_export_video_ogv.pack_forget()
            self.btn_inject_video.pack_forget()
            self.btn_export_png.pack(side=LEFT, padx=3)
            self.btn_export_dds.pack(side=LEFT, padx=3)
            self.btn_replace_tex.pack(side=RIGHT, padx=3)
            self.btn_export_png.config(state=NORMAL)
            self.btn_export_dds.config(state=NORMAL)
            self.btn_replace_tex.config(state=NORMAL)
        else:
            self.subtex_frame.pack_forget()
            self.image_label.config(image="", text="No image or texture found in this chunk.")
            self.preview_image_ref = None
            self.tex_info_label.config(text=f"Chunk Data: {len(raw_data):,} bytes (Binary/Model/Mocap)")
            self.btn_export_video_mp4.pack_forget()
            self.btn_export_video_ogv.pack_forget()
            self.btn_inject_video.pack_forget()
            self.btn_export_png.pack(side=LEFT, padx=3)
            self.btn_export_dds.pack(side=LEFT, padx=3)
            self.btn_replace_tex.pack(side=RIGHT, padx=3)
            self.btn_export_png.config(state=DISABLED)
            self.btn_export_dds.config(state=DISABLED)
            self.btn_replace_tex.config(state=DISABLED)

    def _setup_subtex_radios(self):
        """Creates radio buttons for all textures inside the container."""
        for rb in self.radio_buttons:
            rb.destroy()
        self.radio_buttons.clear()

        if not self.current_face_set or len(self.current_face_set.textures) <= 1:
            self.subtex_frame.pack_forget()
            return

        self.subtex_frame.pack(fill=X, side=TOP, pady=(0, 6))
        self.selected_subtex_var.set(0)

        for i, tex in enumerate(self.current_face_set.textures):
            label_text = f"Slot {i}: {tex.name.capitalize()}"
            rb = tb.Radiobutton(
                self.subtex_frame,
                text=label_text,
                variable=self.selected_subtex_var,
                value=i,
                command=self._update_face_preview,
            )
            rb.pack(side=LEFT, padx=6)
            self.radio_buttons.append(rb)

    def _update_face_preview(self):
        if not self.current_face_set or not self.current_face_set.textures:
            return

        idx = self.selected_subtex_var.get()
        if idx >= len(self.current_face_set.textures):
            idx = 0
            self.selected_subtex_var.set(0)

        subtex = self.current_face_set.textures[idx]
        self.active_dds_bytes = subtex.dds_bytes
        self.active_image = subtex.image

        if subtex.image:
            self._display_image(subtex.image)
            w, h = subtex.image.size
            fourcc = subtex.info.get("fourcc", "DDS") if subtex.info else "DDS"
            mips = subtex.info.get("mipmaps", 1) if subtex.info else 1
            self.tex_info_label.config(text=f"[{subtex.name}] Resolution: {w}x{h} | Format: {fourcc} | Mips: {mips} | Bytes: {len(subtex.dds_bytes):,}")
        else:
            self.image_label.config(image="", text="Could not decode texture preview.")
            self.preview_image_ref = None

    def _display_image(self, img: Image.Image):
        max_size = 480
        w, h = img.size
        scale = min(max_size / w, max_size / h, 1.0)
        display_w = max(1, int(w * scale))
        display_h = max(1, int(h * scale))

        disp_img = img.resize((display_w, display_h), Image.Resampling.NEAREST if scale >= 1.0 else Image.Resampling.LANCZOS)
        tk_img = ImageTk.PhotoImage(disp_img)
        self.image_label.config(image=tk_img, text="")
        self.preview_image_ref = tk_img

    def _on_export_active_as_png(self):
        """Exports the active texture directly as a PNG file."""
        if not self.active_image:
            # If entry is selected but not active_image, try to inspect first
            if self.current_entry:
                self._inspect_entry(self.current_entry)
            if not self.active_image:
                messagebox.showinfo("Export PNG", "No texture is available to export in this entry.")
                return

        stem = Path(self.current_entry.path).stem if self.current_entry else "texture"
        if self.current_face_set and len(self.current_face_set.textures) > 1:
            idx = self.selected_subtex_var.get()
            sub = self.current_face_set.textures[idx]
            stem = f"{stem}_{sub.name.lower()}"

        dest = filedialog.asksaveasfilename(
            title="Export Texture as PNG",
            defaultextension=".png",
            initialfile=f"{stem}.png",
            filetypes=[("PNG Image (*.png)", "*.png"), ("All Files (*.*)", "*.*")],
        )
        if not dest:
            return

        try:
            self.active_image.save(dest)
            messagebox.showinfo("Exported PNG", f"Successfully saved to:\n{Path(dest).name}")
        except Exception as e:
            messagebox.showerror("Export Failed", f"Failed to save PNG image:\n{e}")

    def _on_export_active_as_dds(self):
        """Exports the active texture directly as a standard DDS file."""
        if not self.active_dds_bytes:
            if self.current_entry:
                self._inspect_entry(self.current_entry)
            if not self.active_dds_bytes:
                messagebox.showinfo("Export DDS", "No DDS texture is available to export in this entry.")
                return

        stem = Path(self.current_entry.path).stem if self.current_entry else "texture"
        if self.current_face_set and len(self.current_face_set.textures) > 1:
            idx = self.selected_subtex_var.get()
            sub = self.current_face_set.textures[idx]
            stem = f"{stem}_{sub.name.lower()}"

        dest = filedialog.asksaveasfilename(
            title="Export Texture as DDS",
            defaultextension=".dds",
            initialfile=f"{stem}.dds",
            filetypes=[("DDS Texture (*.dds)", "*.dds"), ("All Files (*.*)", "*.*")],
        )
        if not dest:
            return

        try:
            Path(dest).write_bytes(self.active_dds_bytes)
            messagebox.showinfo("Exported DDS", f"Successfully saved to:\n{Path(dest).name}")
        except Exception as e:
            messagebox.showerror("Export Failed", f"Failed to save DDS file:\n{e}")

    def _on_inject_upscaled_texture(self):
        """Prompts for an edited PNG or DDS and injects it into the active slot/entry."""
        if not self.current_entry or not self.archive:
            messagebox.showinfo("Select Entry", "Please select a texture entry first.")
            return

        path = filedialog.askopenfilename(
            title="Select Edited Texture (PNG or DDS)",
            filetypes=[
                ("Supported Images (*.png;*.dds;*.jpg;*.tga)", "*.png;*.dds;*.jpg;*.tga"),
                ("PNG Image (*.png)", "*.png"),
                ("DDS Texture (*.dds)", "*.dds"),
                ("All Files (*.*)", "*.*"),
            ],
        )
        if not path:
            return

        img_path = Path(path)
        try:
            # 1. Determine DDS bytes
            if img_path.suffix.lower() == ".dds":
                new_dds_bytes = img_path.read_bytes()
            else:
                pil_img = Image.open(img_path)
                # Determine compression based on slot or current info
                info = parse_dds_header(self.active_dds_bytes) if self.active_dds_bytes else {}
                fourcc = info.get("fourcc", "DXT1")
                if fourcc not in ("DXT1", "DXT5"):
                    fourcc = "DXT1"
                new_dds_bytes = encode_image_to_dds(pil_img, fourcc=fourcc, generate_mips=True)

            # 2. Inject based on container type
            if self.current_face_set and self.current_trm:
                idx = self.selected_subtex_var.get()
                self.current_face_set.replace_texture(idx, new_dds_bytes)
                self.current_trm.apply_face_texture_set(self.current_face_set)
                new_trm_bytes, new_s1, new_s2 = self.current_trm.build()
                self.archive.replace_entry(self.current_entry.hash, new_trm_bytes, size1=new_s1, size2=new_s2)
                self._inspect_entry(self.current_entry)
            elif self.current_ref_footer is not None:
                # Raw reftexture segment
                raw_payload = unwrap_dds_to_raw_dxt(new_dds_bytes, self.current_ref_footer)
                self.archive.replace_entry(self.current_entry.hash, raw_payload)
                self._inspect_entry(self.current_entry)
            else:
                # Standalone DDS or generic chunk
                self.archive.replace_entry(self.current_entry.hash, new_dds_bytes)
                self._inspect_entry(self.current_entry)

            messagebox.showinfo(
                "Texture Injected",
                f"Successfully injected texture from {img_path.name}!\n\n"
                "Click 'Repack Archive' or 'Deploy to Game' to save changes to .pc.",
            )
        except Exception as e:
            messagebox.showerror("Injection Error", f"Failed to inject texture:\n{e}")

    def _on_export_active_face_video(self, export_format: str = "mp4"):
        """Exports MotionScan video from the currently inspected DGAD chunk."""
        if not self.current_entry or not self.current_dgad_raw:
            messagebox.showinfo("Export Video", "Please select a MotionScan (DGAD) chunk first.")
            return

        stem = Path(self.current_entry.path).stem
        ext = f".{export_format.lower()}"
        dest = filedialog.asksaveasfilename(
            title=f"Export Face Video ({ext})",
            defaultextension=ext,
            initialfile=f"{stem}{ext}",
            filetypes=[
                (f"Video File (*{ext})", f"*{ext}"),
                ("All Files (*.*)", "*.*"),
            ],
        )
        if not dest:
            return

        try:
            export_face_video(
                self.current_dgad_raw,
                dest,
                export_format=export_format,
                stream_name="color",
            )
            messagebox.showinfo("Video Exported", f"Successfully exported face video to:\n{Path(dest).name}")
        except Exception as e:
            messagebox.showerror("Export Failed", f"Failed to export video:\n{e}")

    def _on_inject_face_video(self):
        """Re-encodes and injects an edited/upscaled video back into the active DGAD chunk."""
        if not self.current_entry or not self.current_dgad_raw or not self.archive:
            messagebox.showinfo("Inject Video", "Please select a MotionScan (DGAD) chunk first.")
            return

        path = filedialog.askopenfilename(
            title="Select Upscaled Video (.mp4 or .ogv)",
            filetypes=[
                ("Video Files (*.mp4;*.ogv)", "*.mp4;*.ogv"),
                ("MP4 Video (*.mp4)", "*.mp4"),
                ("Theora Video (*.ogv)", "*.ogv"),
                ("All Files (*.*)", "*.*"),
            ],
        )
        if not path:
            return

        try:
            new_chunk = rebuild_dgad_with_video(
                self.current_dgad_raw,
                path,
                stream_name="color",
            )
            self.archive.replace_entry(self.current_entry.hash, new_chunk)
            self.current_dgad_raw = new_chunk
            self._inspect_entry(self.current_entry)
            messagebox.showinfo(
                "Video Injected",
                f"Successfully injected {Path(path).name} into {self.current_entry.path}!\n\n"
                "Click 'Repack Archive' or 'Deploy to Game' to save your changes to the .pc file.",
            )
        except Exception as e:
            messagebox.showerror("Injection Failed", f"Failed to inject video:\n{e}")

    def _on_batch_export_face_videos(self):
        """Batch exports all MotionScan face videos matching the search filter to a directory."""
        if not self.archive:
            messagebox.showinfo("Open Archive", "Please open an archive first.")
            return

        dest_dir = filedialog.askdirectory(title="Select Destination Folder for Face Videos")
        if not dest_dir:
            return

        char_filter = self.search_var.get().strip()

        def run_export():
            try:
                count = batch_export_face_videos(
                    self.archive,
                    dest_dir,
                    character_filter=char_filter,
                    export_format="mp4",
                    progress_cb=lambda cur, tot, name: self._update_progress(
                        cur, tot, f"Exporting video: {name} ({cur}/{tot})"
                    ),
                )
                self.after(0, lambda: messagebox.showinfo(
                    "Batch Export Complete",
                    f"Successfully exported {count} face video(s) to:\n{dest_dir}\n\nA manifest.json was generated for auto-reimporting.",
                ))
            except Exception as e:
                self.after(0, lambda err=e: messagebox.showerror("Export Failed", f"Error during batch export:\n{err}"))
            finally:
                self.after(0, lambda: self.status_label.config(text="Ready."))

        self.progress_bar.config(mode="determinate", value=0)
        self.status_label.config(text="Exporting face videos...")
        threading.Thread(target=run_export, daemon=True).start()

    def _on_batch_import_face_videos(self):
        """Scans a folder of upscaled video files (.mp4/.ogv) and injects them into the archive."""
        if not self.archive:
            messagebox.showinfo("Open Archive", "Please open an archive first.")
            return

        src_dir = filedialog.askdirectory(title="Select Folder Containing Upscaled Face Videos (.mp4/.ogv)")
        if not src_dir:
            return

        def run_import():
            try:
                injected = batch_import_face_videos(
                    self.archive,
                    src_dir,
                    stream_name="color",
                    progress_cb=lambda cur, tot, name: self._update_progress(
                        cur, tot, f"Injecting upscaled video: {name} ({cur}/{tot})"
                    ),
                )
                if injected:
                    self.after(0, lambda: messagebox.showinfo(
                        "Batch Import Complete",
                        f"Successfully injected {len(injected)} face video(s) into memory!\n\n"
                        "Click 'Repack Archive' or 'Deploy to Game' to write changes to disk.",
                    ))
                    if self.current_entry:
                        self.after(0, lambda: self._inspect_entry(self.current_entry))
                else:
                    self.after(0, lambda: messagebox.showwarning(
                        "No Matches",
                        f"No matching face video files found in:\n{src_dir}\n\nEnsure video filenames match chunk names (e.g. idle_phelps_neutral_...mp4).",
                    ))
            except Exception as e:
                self.after(0, lambda err=e: messagebox.showerror("Import Failed", f"Error during batch import:\n{err}"))
            finally:
                self.after(0, lambda: self.status_label.config(text="Ready."))

        self.progress_bar.config(mode="determinate", value=0)
        self.status_label.config(text="Scanning and importing face videos...")
        threading.Thread(target=run_import, daemon=True).start()

    def _on_extract_selected(self):
        """Extracts the raw file entry (.chunk / .sges / .bin)."""
        if not self.current_entry or not self.archive:
            return

        dest = filedialog.asksaveasfilename(
            title="Extract Raw Entry",
            initialfile=Path(self.current_entry.path).name,
        )
        if dest:
            self.archive.extract_entry(self.current_entry, Path(dest))
            messagebox.showinfo("Extracted", f"Saved raw file to {Path(dest).name}")

    def _on_extract_all(self):
        if not self.archive:
            return

        dest = filedialog.askdirectory(title="Select Extraction Directory")
        if not dest:
            return

        total = len(self.archive.entries)
        self.progress_bar.config(maximum=total, value=0)

        def task():
            self.archive.extract_all(
                dest,
                progress_cb=lambda i, tot, name: self._update_progress(i, tot, f"Extracting {name}..."),
            )
            self.after(0, lambda: messagebox.showinfo("Completed", f"Extracted all {total} files to {dest}."))
            self.after(0, lambda: self.status_label.config(text="Extraction finished."))

        threading.Thread(target=task, daemon=True).start()

    def _on_extract_all_faces(self):
        if not self.archive:
            return

        dest = filedialog.askdirectory(title="Select Folder to Extract All Face Textures")
        if not dest:
            return

        dest_dir = Path(dest)

        def task():
            head_entries = [e for e in self.archive.entries if "_head" in (e.name if hasattr(e, "name") else e.path).lower()]
            total = len(head_entries)
            if total == 0:
                self.after(0, lambda: messagebox.showinfo("No Heads", "No character head chunks found in this archive."))
                return

            self.after(0, lambda: self.progress_bar.config(maximum=total, value=0))
            extracted = 0

            for i, entry in enumerate(head_entries):
                try:
                    raw_trm = self.archive.read_entry_bytes(entry, decompress=True)
                    if raw_trm[:4] == b"trM#":
                        trm = TRMContainer(raw_trm)
                        face_set = trm.get_face_texture_set()
                        if face_set and face_set.textures:
                            stem = Path(entry.path).stem
                            char_dir = dest_dir / stem
                            char_dir.mkdir(parents=True, exist_ok=True)
                            for tex in face_set.textures:
                                role = tex.name.lower()
                                w = tex.info.get("width", 0) if tex.info else 0
                                h = tex.info.get("height", 0) if tex.info else 0
                                fourcc = tex.info.get("fourcc", "DDS") if tex.info else "DDS"
                                if tex.image:
                                    tex.image.save(char_dir / f"{stem}_{role}_{w}x{h}_{fourcc}.png")
                                if tex.dds_bytes:
                                    (char_dir / f"{stem}_{role}_{w}x{h}_{fourcc}.dds").write_bytes(tex.dds_bytes)
                            extracted += 1
                except Exception:
                    pass
                self._update_progress(i + 1, total, f"Extracting faces: {i + 1}/{total}...")

            self.after(0, lambda: messagebox.showinfo("Extraction Complete", f"Successfully extracted face textures for {extracted} characters to:\n{dest_dir}"))
            self.after(0, lambda: self.status_label.config(text=f"Extracted {extracted} character face texture sets."))

        threading.Thread(target=task, daemon=True).start()

    def _on_batch_inject_folder(self):
        import re
        if not self.archive:
            return

        batch_dir_str = filedialog.askdirectory(title="Select Folder Containing Upscaled Face Textures (PNG or DDS)")
        if not batch_dir_str:
            return

        batch_dir = Path(batch_dir_str)

        def task():
            head_map = {}
            for entry in self.archive.entries:
                p_str = entry.name if hasattr(entry, "name") else entry.path
                if "_head" in p_str.lower():
                    stem = Path(p_str).stem.lower()
                    head_map[stem] = entry

            image_exts = {".png", ".dds", ".jpg", ".tga"}
            image_files = [f for f in batch_dir.rglob("*") if f.suffix.lower() in image_exts]
            if not image_files:
                self.after(0, lambda: messagebox.showinfo("No Images Found", f"No supported image files (.png, .dds) found in {batch_dir}."))
                return

            updates_by_entry = {}
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
                    continue

                entry = head_map[matched_stem]
                if entry.hash not in updates_by_entry:
                    updates_by_entry[entry.hash] = {"entry": entry, "slots": {}}
                updates_by_entry[entry.hash]["slots"][slot_idx] = (img_file, fourcc)

            total = len(updates_by_entry)
            if total == 0:
                self.after(0, lambda: messagebox.showinfo("No Matches", "Could not match any images in the folder to character heads in this archive."))
                return

            self.after(0, lambda: self.progress_bar.config(maximum=total, value=0))
            applied = 0

            for i, (entry_hash, update_info) in enumerate(updates_by_entry.items()):
                entry = update_info["entry"]
                slots = update_info["slots"]
                try:
                    raw_data = self.archive.read_entry_bytes(entry, decompress=True)
                    if raw_data[:4] == b"trM#":
                        trm = TRMContainer(raw_data)
                        face_set = trm.get_face_texture_set()
                        if face_set:
                            for slot_idx, (img_path, fourcc) in slots.items():
                                if img_path.suffix.lower() == ".dds":
                                    new_dds = img_path.read_bytes()
                                else:
                                    pil_img = Image.open(img_path)
                                    new_dds = encode_image_to_dds(pil_img, fourcc=fourcc, generate_mips=True)
                                face_set.replace_texture(slot_idx, new_dds)

                            trm.apply_face_texture_set(face_set)
                            new_trm, s1, s2 = trm.build()
                            self.archive.replace_entry(entry.hash, new_trm, size1=s1, size2=s2)
                            applied += 1
                except Exception:
                    pass
                self._update_progress(i + 1, total, f"Injecting textures: {i + 1}/{total}...")

            if self.current_entry:
                self.after(0, lambda: self._inspect_entry(self.current_entry))

            self.after(0, lambda: messagebox.showinfo(
                "Batch Injection Complete",
                f"Successfully injected textures into {applied} character heads in memory!\n\n"
                "Click 'Repack Archive' or 'Deploy to Game' to save to the .pc file.",
            ))
            self.after(0, lambda: self.status_label.config(text=f"Batch injected {applied} character head textures."))

        threading.Thread(target=task, daemon=True).start()

    def _on_repack_archive(self):
        if not self.archive:
            return

        dest = filedialog.asksaveasfilename(
            title="Save Repacked Archive",
            defaultextension=".big.pc",
            initialfile=self.archive.path.name,
            filetypes=[("Archive (*.pc)", "*.pc"), ("All Files (*.*)", "*.*")],
        )
        if not dest:
            return

        self._execute_repack(Path(dest))

    def _on_deploy_to_game(self):
        if not self.archive:
            return

        ans = messagebox.askyesno(
            "Deploy to Game",
            f"This will replace the archive in:\n{self.archive.path}\n\n"
            "An automatic backup (.bak) will be created.\nProceed?",
        )
        if ans:
            self._execute_repack(self.archive.path)

    def _execute_repack(self, dest_path: Path):
        total = len(self.archive.entries)
        self.progress_bar.config(maximum=total, value=0)

        def task():
            try:
                self.archive.repack(
                    dest_path,
                    compress=True,
                    backup_original=True,
                    progress_cb=lambda i, tot, name: self._update_progress(i, tot, f"Repacking entry {i}/{tot}..."),
                )
                self.after(0, lambda: self.load_archive(dest_path))
                self.after(0, lambda: messagebox.showinfo("Repack Complete", f"Successfully repacked to:\n{dest_path}"))
                self.after(0, lambda: self.status_label.config(text=f"Repacked {dest_path.name} successfully."))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Repack Error", f"Error during repacking:\n{e}"))

        threading.Thread(target=task, daemon=True).start()

    def _update_progress(self, current: int, total: int, text: str):
        self.after(0, lambda: (self.progress_bar.config(value=current), self.status_label.config(text=text)))

    @staticmethod
    def _format_hex(data: bytes, width: int = 16) -> str:
        lines = []
        for i in range(0, len(data), width):
            chunk = data[i : i + width]
            hex_part = " ".join(f"{b:02X}" for b in chunk).ljust(width * 3)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            lines.append(f"{i:08X}  {hex_part}  |{ascii_part}|")
        return "\n".join(lines)


def launch_gui():
    app = LANoireToolkitApp()
    app.mainloop()


if __name__ == "__main__":
    launch_gui()
