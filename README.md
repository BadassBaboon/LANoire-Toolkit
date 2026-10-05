# L.A. Noire PC Toolkit

A tool and graphical interface for extracting, inspecting, editing, and repacking L.A. Noire `.big.pc` and `.wad.pc` archives. It supports both MotionScan `DGAD` facial video performances and static `trM#` character skin textures in the PC version of L.A. Noire.

## Requirements

- Python 3.10 or newer
- FFmpeg installed and available on your system `PATH` (or specified via the `FFMPEG_PATH` environment variable). FFmpeg handles video demuxing, frame extraction, and Theora re-encoding.
- Windows 10 or 11 (64-bit)

## Installation

Clone the repository and install the Python dependencies:

```bash
git clone https://github.com/BadassBaboon/LANoire-Toolkit.git
cd LANoire-Toolkit
pip install -r requirements.txt
```

Verify that FFmpeg is accessible:

```bash
ffmpeg -version
```

If FFmpeg is in a custom directory, set the environment variable:

```bash
set FFMPEG_PATH=C:\path\to\ffmpeg\bin\ffmpeg.exe
```

## Supported formats

The tool reads and writes several formats used by Team Bondi's engine:

- `DGAD` facial performance chunks: 30 FPS synchronized streams combining 3D vertex deformation (Stream 1), dynamic normal maps (Stream 2), and color facial video (Stream 3). Color video defaults to 512x512 or 256x256 Theora video. Found in `da.big.pc` and case archives such as `cases_1_1.big.pc`.
- `trM#` character head models: Head models containing a 3-slot texture set in VRAM (Slot 0 diffuse at 256x256 DXT1, Slot 1 normals at 128x128 DXT5, Slot 2 specular at 128x128 DXT1). Found in `characters.big.pc`.
- `.big.pc` archives: The primary container format. Uses a table of contents stored at the end of the file, 16-byte alignment, and sequential DEFLATE (`sges`) compression chunks.
- `.wad.pc` archives: Secondary package format (such as `out.wad.pc`).
- DDS textures: DirectDraw Surface textures in DXT1, DXT5, and raw formats with full mipmap generation.

## Common use cases

### Upscaling MotionScan face performances
In the original game, facial performances are recorded at 512x512 or 256x256 resolution. You can export these video streams to MP4, upscale them with an AI video tool (Topaz Video AI, Real-ESRGAN, or similar software) to 1024x1024 or 2048x2048, and batch import them back into the archive. The toolkit updates the macroblock scale and stream headers so the engine renders the higher resolution video across the face mesh.

### Upscaling static character skin textures
Character heads contain separate static textures for diffuse, normal, and specular details. You can export them to PNG or DDS, edit skin pores or facial hair, and inject them back into the TRM model with automatic mipmap regeneration.

### Inspecting and repacking archives
You can open any `.big.pc` archive, search files by path or hash, inspect raw hex contents, extract chunks, and repack archives. An automatic `.bak` copy is created whenever you overwrite an active game file.

## Graphical interface walkthrough

Launch the application:

```bash
python run_toolkit.py
```

### 1. Opening an archive
Click `Open Archive...` and choose any `.big.pc` or `.wad.pc` file from your game folder (typically located at `L.A.Noire\final\pc`).

### 2. Searching and filtering
The search row sits directly below the main actions and stays visible at any window size:
- Type in the `Search:` box to filter by path, file stem, hex hash (`0x9b1d36d1`), or format (`dgad`, `fsb4`, `trm`).
- Click `✕` to clear the search filter.
- Use the category dropdown to isolate specific asset types:
  - `MotionScan (DGAD)`: Lists all 30 FPS facial performance chunks. In case archives like `cases_1_1.big.pc`, this detects both named entries and hashed `unknown/0x<hash>.bin` entries through 4-byte header sniffing.
  - `Heads/Faces`: Isolates character head chunks containing editable skin textures.
  - `Textures`: Shows standalone DDS textures and SGES texture segments.
  - `Models`: Shows TRM models and component meshes.
  - `Audio`: Lists FSB4 audio banks.
  - `Unknown/Hashed`: Shows entries whose original filenames are not present in the hash database.

### 3. Previewing assets
Select an entry in the list:
- For character heads (`trM#`), the right pane shows radio buttons for each texture slot (Diffuse, Normal, Specular) with resolution, format, and pixel preview.
- For MotionScan chunks (`DGAD`), the tool decodes frame 0 via FFmpeg and displays the video resolution, FPS, total frame count, and duration.
- For other chunks, the raw hex view shows an offset dump with byte values and ASCII text.

### 4. Single file export and injection
- To export a face video, click `Export Face Video (.mp4)` or `Export as .ogv`.
- To inject an edited video, click `Inject Video (.mp4/.ogv)`. Choose the upscaled file and the toolkit re-encodes the stream in memory.
- To export a texture, click `Export as PNG` or `Export as DDS`.
- To inject an edited texture, click `Inject Texture (PNG/DDS)`. The tool converts the image to DDS with mipmaps and rebuilds the TRM container.

### 5. Batch video upscaling workflow
For large cases containing dozens or hundreds of facial video files:
1. Open a case archive (such as `cases_1_1.big.pc`) or `da.big.pc`.
2. Optional: type a character filter in the search box (for example, `phelps`).
3. Click `Batch Export Videos...` and select an output folder. The toolkit exports each clip as an MP4 file and writes a `manifest.json` file recording original resolutions, frame counts, and file hashes.
4. Process the MP4 files in your video upscaler.
5. In the toolkit, click `Batch Import Videos...` and select the folder with the upscaled files. The tool matches each file using `manifest.json`. If the manifest was deleted or the upscaler renamed the files (such as adding `_upscaled` or `_4x`), it matches them by the hex hash prefix.
6. The toolkit re-encodes the upscaled frames to Theora, recalculates macroblock dimensions, and updates each chunk in memory.

### 6. Saving your changes
- `Save (In-Place .bak)`: Creates a `.bak` backup copy of your opened archive and overwrites the file with your repacked changes.
- `Repack as New...`: Prompts for a new file destination, leaving the original file untouched.

## Command line reference

You can automate exports and rebuilds without the GUI using `cli.py` or `run_toolkit.py <command>`.

### List archive contents
```bash
python run_toolkit.py list "C:\Games\L.A.Noire\final\pc\cases_1_1.big.pc"
```

### Extract all files from an archive
```bash
python run_toolkit.py extract "C:\Games\L.A.Noire\final\pc\characters.big.pc" ./extracted_characters
```

### Extract MotionScan face videos to MP4
Extract all face videos matching a filter:
```bash
python run_toolkit.py extract-face-video "C:\Games\L.A.Noire\final\pc\cases_1_1.big.pc" phelps --out ./phelps_videos --format mp4
```

Extract by hex hash for unnamed entries:
```bash
python run_toolkit.py extract-face-video "C:\Games\L.A.Noire\final\pc\cases_1_1.big.pc" 0x9b1d36d1 --out ./exported_face --format mp4
```

### Inject a single face video
```bash
python run_toolkit.py inject-face-video "C:\Games\L.A.Noire\final\pc\cases_1_1.big.pc" 0x9b1d36d1 ./upscaled_face.mp4
```

### Batch inject upscaled face videos
```bash
python run_toolkit.py batch-inject-face-videos "C:\Games\L.A.Noire\final\pc\cases_1_1.big.pc" ./upscaled_videos
```

### Extract character face textures
```bash
python run_toolkit.py extract-face "C:\Games\L.A.Noire\final\pc\characters.big.pc" phelps --out ./phelps_textures
```

### Inject a character head texture
```bash
python run_toolkit.py inject-face "C:\Games\L.A.Noire\final\pc\characters.big.pc" cole_phelps_head_new ./new_diffuse.png
```

### Batch inject a folder of textures
```bash
python run_toolkit.py batch-inject-faces "C:\Games\L.A.Noire\final\pc\characters.big.pc" ./upscaled_textures
```

### Repack an archive
```bash
python run_toolkit.py repack "C:\Games\L.A.Noire\final\pc\characters.big.pc" ./characters_modded.big.pc
```

## MotionScan format details

Disassembly of `LANoire.exe` (specifically the vtable at `0x0125523c` for `TheoraMovie`, DCT structures, and decoders at `0x01010bb0` and `0x00dbb440`) documents the storage structure used for facial animation:

1. Stream organization:
   Each performance chunk contains synchronized data across three parallel streams:
   - Stream 1: 3D vertex positions that deform the base head mesh over time.
   - Stream 2: Dynamic normal and wrinkle maps encoded as 128x128 Theora video.
   - Stream 3: Color, lip, and eye texture video encoded as 512x512 or 256x256 Theora video at 30 FPS.

2. Header layout:
   The chunk begins with a 52-byte header:
   - Bytes 0-3: Magic `DGAD` (0x44474144).
   - Bytes 4-7: Version integer (typically 3).
   - Bytes 8-11: Total frame count.
   - Bytes 12-15: Frame rate (30 FPS).
   - Bytes 16-19: Macroblock scale factor, defined as `pixel_width / 4`. For 512x512 video, this value is 128. For 1024x1024 video, this value is 256.
   - Bytes 20-23: Native pixel resolution (e.g. 512).

3. Frame table:
   Following the header is a table of 208-byte records for each frame. Each record provides the exact byte size of Stream 1, Stream 2, and Stream 3 payloads for that frame.

4. Theora video packets:
   Frame 0 embeds the three RFC 3533 Ogg Theora identification and setup headers (`\x80theora`, `\x81theora`, `\x82theora`), followed by an end-of-header delimiter (`0xFFFFFFFF`) and the first keyframe packet. Subsequent frames store sequential raw video packets.

5. Resolution scaling:
   When re-encoding Stream 3 from 512x512 to 1024x1024 or 2048x2048, the toolkit updates the pixel resolution field and scales the macroblock factor proportionally. This allows the game engine to stream and map the higher resolution video across the animated face mesh without texture alignment errors.
