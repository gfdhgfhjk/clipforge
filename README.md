# ClipForge

A simple program that can quickly export multiple videos at once with a wide variety of configurations.

Completely **open source** and free to use. Uses [FFmpeg](https://ffmpeg.org) for all the heavy lifting.

<img width="1013" height="783" alt="image" src="https://github.com/user-attachments/assets/8fc7ec11-12f7-4027-b6d8-4542aec77579" />

---

## Features

**Clips**
- Add many clips at once, reorder (▲ ▼), sort by name (natural order: `clip2` before `clip10`) or by date
- Trim any clip (double-click it): start/end in seconds or `h:mm:ss`
- Mixed resolutions, frame rates, vertical/phone footage and clips with no audio are all handled automatically
- Reads phone rotation metadata, so portrait clips come out upright

**Fast exporting**
- Hardware encoders: **NVIDIA NVENC**, **AMD AMF**, **Intel QSV** (H.264, HEVC, AV1) plus CPU x264 / x265 / SVT-AV1
- On launch it *test-encodes* with every encoder and only lists the ones that really work on your PC
- Hardware decoding (auto-falls back to software if the GPU decoder fails)
- Identical clips take a fast path (single decoder, low memory); **Stream copy** joins identical clips instantly with no re-encode
- Filters (scale / pad / fps) only run on clips that need them
- Live progress: percent, speed in x-realtime, encode FPS, ETA, estimated final size

**YouTube-friendly output**
- "YouTube-optimised" mode: closed GOP, keyframe every 0.5 s, BT.709 colour, 4:2:0, AAC @ 48 kHz, faststart
- Optional bitrate cap at YouTube's recommended levels (saves storage; YouTube re-encodes anyway)
- **1440p (YouTube VP9 boost)**: exporting at 1440p or higher is widely reported to get you YouTube's higher-bitrate VP9 playback, even from 1080p footage

**Controls**
- Encoder, resolution (Auto / 720p / 1080p / 1440p / 4K), frame rate (Auto / 24 / 30 / 60)
- Quality slider (one scale for every encoder; lower = better and bigger)
- Speed vs quality preset, audio bitrate (128-384k)

**Audio**
- **Merges multiple audio tracks into one** (e.g. game + microphone), with per-track volume and a limiter against clipping

**Overlay (file name / date)**
- Burns the file name and/or date into a corner of every clip
- Custom text template with tokens `{name} {file} {date} {time} {index}`
- Date from: file created / file modified / video metadata; any strftime date format
- 6 positions, font, colour, size, opacity, margin, drop shadow, backdrop box, "show for first N seconds"
- **Preview** button renders one frame so you can tune it before exporting

**Speed tester**
- Benchmarks every working encoder with your current settings and reports time, x-realtime, FPS, size, bitrate and an SSIM quality score
- Names the fastest encoder and the best quality-per-size, and "Use fastest" applies it in one click

---

## Quick start (Windows)

1. **Install Python 3.9+ if you haven't already** from <https://python.org>. Keep the default options (tkinter is included).
2. **Get FFmpeg** (a *full* build with GPU encoders): download `ffmpeg-release-essentials.zip` from
   <https://www.gyan.dev/ffmpeg/builds/>, open it, go into `bin`, create a new folder, and copy **`ffmpeg.exe`** and **`ffprobe.exe`**
   into the same folder as `clipforge.py`. (Or install with `winget install Gyan.FFmpeg` and restart your terminal.)
3. Open PowerShell in that folder using `cd (put the path to the file here)` and run:
   ```
   python clipforge.py
   ```
4. The status bar lists the encoders it found. If your GPU isn't listed, see *Troubleshooting*.

macOS / Linux: install FFmpeg with `brew install ffmpeg` / `sudo apt install ffmpeg`, then `python3 clipforge.py`.
(No NVENC/AMF on macOS; on Linux, NVENC needs the NVIDIA driver.)

## How to use it

1. **＋ Add clips**, then arrange them (▲ ▼ or Sort name / Sort date). Double-click a clip to trim it.
2. Pick an **Encoder** (the GPU one for your card is the fast choice), **Resolution** and **Frame rate**.
   For most clip dumps: *H.264 on your GPU, 1080p, Auto fps, YouTube-optimised on, bitrate cap on*.
3. Leave **Merge multiple audio tracks** ticked if your clips have separate game/mic tracks.
4. *(Optional)* **Overlay** tab: tick *Show overlay*, set the text/position, hit **Preview**.
5. Check the output path (defaults to your **Downloads** folder with a timestamped name), then **▶ EXPORT**.
6. Upload the resulting `.mp4` to YouTube.

**Saving storage:** Use HEVC or AV1 if your GPU supports them (RTX 40-series, RX 7000, Arc). Use the
**Speed tester** tab to compare encoders; look at SSIM per MB. The same quality-slider number is not exactly
the same visual quality on different encoders.


- Encoding runs on the GPU. The remaining cost is **CPU-side decode/filtering and disk speed**.
- It skips filters a clip doesn't need, uses fast bicubic scaling unless you choose *Best quality*, and
  "Fastest" mode drops NVENC lookahead/adaptive quantisation.
- Clips that all match take a single-decoder path; stream copy avoids re-encoding entirely.

**How to see your bottleneck:** open Task Manager → Performance while exporting. If the GPU's *Video Encode*
graph is near 100%, you're encoder-bound (that's as fast as that GPU goes). If your CPU is pegged and the GPU is
low, the CPU filters/decoder are the limit: try 1080p instead of 4K, matching clip resolutions, or Fastest preset.

Not implemented (possible future work): an all-GPU filter pipeline (`scale_cuda`) and encoding segments in
parallel. Both can be faster on some hardware but add complexity and fragility.

## FFmpeg is not included

You must download FFmpeg yourself from the links above in order to use ClipForge.


## Troubleshooting

| Problem | Fix |
|---|---|
| "ffmpeg/ffprobe not found" | Follow the instructions above to download FFmpeg and put both `.exe` files next to `clipforge.py`, or add FFmpeg to PATH |
| GPU encoder not listed | Update your GPU driver (NVIDIA / AMD Adrenalin). Check the **Log** tab, which says why each encoder was rejected |
| AV1 encoders missing | Needs RTX 40-series, RX 7000-series or Intel Arc, plus a recent FFmpeg |
| Overlay unavailable | Your FFmpeg build lacks the `drawtext` filter. Use a full build from gyan.dev |
| Export fails on many clips | Untick *Hardware decoding* (GPUs limit simultaneous decoders) |
| "Stream copy needs identical clips" | Clips differ (or are trimmed / overlay / audio merge is on). Choose an encoder instead |
| Audio too loud or quiet after merge | Adjust *Track 1 vol %* and *Track 2+ vol %* |
| Date on overlay is the copy date | Set *Date from* to **Video metadata** |

Every FFmpeg command ClipForge runs is printed in the **Log** tab, handy for reproducing or debugging.

## Notes

- This project was written with AI assistance (Claude, by Anthropic).
- FFmpeg, NVENC, AMF, QSV, YouTube and other names belong to their respective owners; this project is not affiliated with them.
