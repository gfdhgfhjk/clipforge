#!/usr/bin/env python3
"""ClipForge - join many clips into one YouTube-ready video, fast.
Needs: Python 3.9+ (tkinter included) and a full FFmpeg build (ffmpeg + ffprobe)
on PATH or next to this file. gyan.dev "full" builds include NVENC / AMF / QSV."""
import json, math, os, re, shutil, subprocess, sys, tempfile, threading, queue, time
from collections import deque
from datetime import datetime, timezone
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog, colorchooser

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find(n):
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(base, n + (".exe" if os.name == "nt" else ""))
    return p if os.path.exists(p) else shutil.which(n)


FF, FP = find("ffmpeg"), find("ffprobe")
HEAD = [FF, "-y", "-hide_banner", "-nostdin", "-loglevel", "warning", "-progress", "pipe:1", "-nostats"]
BATCH = 8  # max clips open in one FFmpeg process (each open clip = its own decoder + buffers)
COPY = "⚡ Stream copy (instant - identical clips only)"
ENCODERS = [("h264_nvenc", "NVIDIA NVENC H.264"), ("hevc_nvenc", "NVIDIA NVENC HEVC"), ("av1_nvenc", "NVIDIA NVENC AV1"),
            ("h264_amf", "AMD AMF H.264"), ("hevc_amf", "AMD AMF HEVC"), ("av1_amf", "AMD AMF AV1"),
            ("h264_qsv", "Intel QSV H.264"), ("hevc_qsv", "Intel QSV HEVC"), ("av1_qsv", "Intel QSV AV1"),
            ("libx264", "CPU x264 H.264"), ("libx265", "CPU x265 HEVC"), ("libsvtav1", "CPU SVT-AV1")]
LABEL = dict(ENCODERS)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", creationflags=NOWIN)


def detect(logf=print):
    """Only list encoders that actually work on this machine (real 3-frame test encode)."""
    have = run([FF, "-hide_banner", "-encoders"]).stdout
    ok = []
    for n, _ in ENCODERS:
        if n not in have:
            continue
        r = run([FF, "-v", "error", "-f", "lavfi", "-i", "color=black:s=1280x720:r=30", "-frames:v", "3",
                 "-pix_fmt", "yuv420p", "-c:v", n, "-f", "null", "-"])
        if r.returncode == 0:
            ok.append(n)
        else:
            logf(f"{n}: in this FFmpeg build but not usable here ({(r.stderr.strip().splitlines() or ['?'])[-1]})")
    return ok


def probe(path):
    r = run([FP, "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,pix_fmt,sample_rate,channels:stream_side_data=rotation:format=duration:format_tags=creation_time",
             "-of", "json", path])
    d = json.loads(r.stdout)
    v = next((s for s in d.get("streams", []) if s["codec_type"] == "video"), None)
    a = next((s for s in d.get("streams", []) if s["codec_type"] == "audio"), None)
    na = sum(1 for s in d.get("streams", []) if s["codec_type"] == "audio")
    if not v:
        raise ValueError("no video stream")
    n, m = map(int, v["r_frame_rate"].split("/"))
    fps = n / m if m else 30.0
    an, am = map(int, v.get("avg_frame_rate", "0/1").split("/"))
    vfr = bool(am) and abs(an / am - fps) > 0.5
    w, h = v["width"], v["height"]
    rot = next((abs(int(float(x["rotation"]))) for x in v.get("side_data_list", []) if "rotation" in x), 0)
    if rot in (90, 270):
        w, h = h, w
    sig = (v["codec_name"], v["width"], v["height"], round(fps, 2), v.get("pix_fmt"), rot,
           (a["codec_name"], a.get("sample_rate"), a.get("channels")) if a else None)
    stt = os.stat(path)
    ct = getattr(stt, "st_birthtime", stt.st_ctime if os.name == "nt" else stt.st_mtime)
    meta = None
    try:
        meta = datetime.strptime(d["format"]["tags"]["creation_time"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        meta = meta if meta > 631152000 else None
    except Exception:
        pass
    return dict(path=path, name=os.path.basename(path), dur=float(d["format"]["duration"]), w=w, h=h, fps=fps,
                sig=sig + (na,), audio=bool(a), na=na, vfr=vfr, ctime=ct, mtime=stt.st_mtime, meta=meta, ss=0.0, to=None)


def ptime(s):
    s = s.strip()
    if not s:
        return None
    t = 0.0
    for p in s.split(":"):
        t = t * 60 + float(p)
    return t


def fmt(t):
    m, s = divmod(int(t), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"


def rec_mbps(H, fps, enc):
    """YouTube's recommended SDR upload bitrates (30fps; x1.5 for high frame rate)."""
    b = 40 if H >= 2160 else 16 if H >= 1440 else 8 if H >= 1000 else 5 if H >= 700 else 2.5
    b *= 1.5 if fps > 35 else 1
    return b * (0.7 if ("265" in enc or "hevc" in enc or "av1" in enc or "svt" in enc) else 1)


def vopts(enc, q, speed, fps, H, cap, yt, tag=True):
    """Video encoder options. speed: 0 fast, 1 balanced, 2 best quality. q: x264-style quality (lower=better)."""
    off = {"libsvtav1": 9, "libx265": 3}.get(enc, 7 if "av1" in enc else 2 if "hevc" in enc else 0)
    Q = int(q) + off
    rb = rec_mbps(H, fps, enc)
    mr, bs = f"{rb * 1.5:.1f}M", f"{rb * 3:.1f}M"
    capa = ["-maxrate", mr, "-bufsize", bs] if cap else []
    a = ["-c:v", enc]
    if "nvenc" in enc:
        a += ["-preset", ["p3", "p5", "p7"][speed], "-tune", "hq", "-rc", "vbr", "-cq", str(Q), "-b:v", "0"] + capa
        if speed:  # Balanced / Best quality only; "Fastest" skips lookahead + AQ
            a += [aq("spatial"), "1", aq("temporal"), "1", "-rc-lookahead", "24"]
    elif "amf" in enc:
        a += ["-quality", ["speed", "balanced", "quality"][speed]]
        a += (["-rc", "vbr_peak", "-b:v", f"{rb:.1f}M", "-maxrate", mr] if cap
              else ["-rc", "cqp", "-qp_i", str(Q), "-qp_p", str(Q + 2)])
    elif "qsv" in enc:
        a += ["-preset", ["veryfast", "medium", "veryslow"][speed], "-global_quality", str(Q)] + capa
    elif enc in ("libx264", "libx265"):
        a += ["-preset", ["veryfast", "medium", "slow"][speed], "-crf", str(Q)] + capa
        if enc == "libx265":
            a += ["-x265-params", "log-level=error"]
    else:
        a += ["-preset", ["10", "8", "5"][speed], "-crf", str(Q)]
    if enc.startswith("h264") or enc == "libx264":
        a += ["-profile:v", "high"]
        if "nvenc" in enc or enc == "libx264":
            a += ["-bf", "2"]
    if tag and ("265" in enc or enc.startswith("hevc")):
        a += ["-tag:v", "hvc1"]
    a += ["-pix_fmt", "yuv420p", "-g", str(max(1, round(fps / 2)) if yt else fps * 2)]
    if yt:  # YouTube recommended: closed GOP, keyframe every 0.5s, BT.709
        a += ["-flags", "+cgop", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
    return a


def vchain(W, H, fps, ovf="", c=None, hq=False):
    """Per-clip video filter chain. With clip info `c`, skips fps/scale/pad when the clip already matches
    (running them anyway costs 2-3x the filter time). hq=True uses lanczos scaling, otherwise faster bicubic."""
    f, alg = [], "lanczos" if hq else "bicubic"
    if not c or c.get("vfr") or abs(c["fps"] - fps) > 0.01:
        f.append(f"fps={fps}")
    if not c or (c["w"], c["h"]) != (W, H):
        if c and abs(c["w"] / c["h"] - W / H) < 0.003:
            f.append(f"scale={W}:{H}:flags={alg}:out_color_matrix=bt709")
        else:
            f.append(f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags={alg}:out_color_matrix=bt709")
            f.append(f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2")
    f.append("setsar=1")
    if ovf:
        f.append(ovf)
    f.append("format=yuv420p")
    return ",".join(f)


def mixparts(i, n, v1, v2, out):
    """Mix all n audio tracks of input i into one stereo track (limiter prevents clipping)."""
    ps = [f"[{i}:a:{k}]aresample=48000,aformat=channel_layouts=stereo,volume={(v1 if k == 0 else v2):.2f}[m{i}_{k}]" for k in range(n)]
    ins = "".join(f"[m{i}_{k}]" for k in range(n))
    return ";".join(ps + [f"{ins}amix=inputs={n}:duration=longest:normalize=0,alimiter=limit=0.97,aresample=48000[{out}]"])


def find_fonts():
    dirs = [r"C:\Windows\Fonts", "/Library/Fonts", "/System/Library/Fonts/Supplemental", "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/TTF"]
    cand = {"Segoe UI": "segoeui.ttf", "Segoe UI Semibold": "seguisb.ttf", "Arial": "arial.ttf", "Arial Bold": "arialbd.ttf", "Calibri": "calibri.ttf",
            "Consolas": "consola.ttf", "Verdana": "verdana.ttf", "Tahoma": "tahoma.ttf", "Trebuchet MS": "trebuc.ttf", "Impact": "impact.ttf",
            "Courier New": "cour.ttf", "DejaVu Sans": "DejaVuSans.ttf", "Helvetica": "Helvetica.ttc"}
    found = {}
    for n, f in cand.items():
        p = next((os.path.join(d, f) for d in dirs if os.path.exists(os.path.join(d, f))), None)
        if p:
            found[n] = p
    return found


POS = {"Bottom-right": ("w-tw-{m}", "h-th-{m}"), "Bottom-left": ("{m}", "h-th-{m}"), "Bottom-center": ("(w-tw)/2", "h-th-{m}"),
       "Top-right": ("w-tw-{m}", "{m}"), "Top-left": ("{m}", "{m}"), "Top-center": ("(w-tw)/2", "{m}")}


def num(v, d):
    try:
        return float(v.get())
    except Exception:
        return d


def overlay_text(ov, c, i):
    ts = c["meta"] if (ov["src"].startswith("Video") and c["meta"]) else (c["mtime"] if ov["src"] == "File modified" else c["ctime"])
    dt = datetime.fromtimestamp(ts)
    try:
        date = dt.strftime(ov["datefmt"])
    except ValueError:
        date = dt.strftime("%Y-%m-%d")
    return (ov["tpl"].replace("{name}", os.path.splitext(c["name"])[0]).replace("{file}", c["name"]).replace("{date}", date)
            .replace("{time}", dt.strftime("%H:%M")).replace("{index}", str(i + 1)))


def overlay_filter(ov, c, i, H, tmp):
    """drawtext filter for one clip. Text goes through a file so odd characters in names can't break the filter."""
    fs, m, op = max(8, round(H * ov["size"] / 100)), round(H * ov["margin"] / 100), ov["opacity"] / 100
    fn = f"ov{i}.txt"
    open(os.path.join(tmp, fn), "w", encoding="utf-8").write(overlay_text(ov, c, i))
    x, y = (e.format(m=m) for e in POS[ov["pos"]])
    f = f"drawtext=textfile='{fn}':expansion=none:fontsize={fs}:fontcolor=0x{ov['color'].lstrip('#')}@{op:.2f}:x={x}:y={y}"
    if ov["font"]:
        f += ":fontfile='" + ov["font"].replace("\\", "/").replace(":", "\\:") + "'"
    if ov["shadow"]:
        sh = max(1, round(fs / 14)); f += f":shadowcolor=black@{min(1, op):.2f}:shadowx={sh}:shadowy={sh}"
    if ov["box"]:
        f += f":box=1:boxcolor=black@{0.45 * op:.2f}:boxborderw={max(2, fs // 3)}"
    if ov["secs"] > 0:
        f += f":enable='lt(t,{ov['secs']:g})'"
    return f


def target(st):
    cl = st["clips"] or [dict(w=1920, h=1080, fps=60)]
    m = re.match(r"(\d+)p", st["res"])
    if m:
        H = int(m[1]); W = H * 16 // 9
        if sum(c["h"] > c["w"] for c in cl) > len(cl) / 2:
            W, H = H, W  # mostly portrait clips -> portrait canvas
    else:
        W, H = max(c["w"] for c in cl), max(c["h"] for c in cl)
    f = st["fps"]
    fps = min(60, round(max(c["fps"] for c in cl))) if f.startswith("Auto") else int(f.split()[0])
    return W - W % 2, H - H % 2, fps


def qpath(p):
    return "file '" + p.replace("\\", "/").replace("'", "'\\''") + "'"


def downloads():
    d = os.path.join(os.path.expanduser("~"), "Downloads")
    return d if os.path.isdir(d) else os.path.expanduser("~")


def default_out():
    return os.path.join(downloads(), "clip_dump_" + time.strftime("%Y%m%d_%H%M%S") + ".mp4")


_AQ = {}


def aq(name):
    """FFmpeg renamed nvenc options (spatial_aq -> spatial-aq); pick whichever this build understands."""
    if not _AQ:
        h = run([FF, "-hide_banner", "-h", "encoder=h264_nvenc"]).stdout
        _AQ["sep"] = "-" if "-spatial-aq" in h else "_"
    return f"-{name}{_AQ['sep']}aq"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ClipForge - fast clip-dump exporter")
        self.geometry("1020x760")
        ttk.Style(self).theme_use("clam")
        self.clips, self.q, self.proc, self.avail, self.busy, self.fastest = [], queue.Queue(), None, [], False, None
        self.tail = deque(maxlen=8); self.stop = False
        self.emap = {COPY: "copy"}
        self.fonts, self.ov_ok, self.ov_color = find_fonts(), True, "#ffffff"
        self.build_ui()
        self.out.set(default_out())
        self.after(100, self.poll)
        if not (FF and FP):
            messagebox.showerror("FFmpeg missing", "ffmpeg/ffprobe not found.\nInstall a full build (e.g. gyan.dev) and put it on PATH or next to clipforge.py.")
            return
        self.status.set("Detecting hardware encoders...")
        threading.Thread(target=self.do_detect, daemon=True).start()

    # ---------- UI ----------
    def build_ui(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        ed, ovt, bn, lg = ttk.Frame(nb), ttk.Frame(nb), ttk.Frame(nb), ttk.Frame(nb)
        nb.add(ed, text="  Editor / Export  "); nb.add(ovt, text="  Overlay (name / date)  "); nb.add(bn, text="  Speed tester  "); nb.add(lg, text="  Log  ")
        self.build_overlay(ovt)

        bar = ttk.Frame(ed); bar.pack(fill="x", padx=8, pady=6)
        for t, f in [("＋ Add clips", self.add), ("Remove", self.remove), ("▲", lambda: self.move(-1)), ("▼", lambda: self.move(1)),
                     ("Sort name", lambda: self.sort("name")), ("Sort date", lambda: self.sort("date")), ("Trim…", self.trim), ("Clear", self.clear)]:
            ttk.Button(bar, text=t, command=f).pack(side="left", padx=2)
        cols = [("No", 40), ("Clip", 380), ("Length", 80), ("Size", 100), ("FPS", 60), ("Audio", 70), ("Trim", 150)]
        self.tv = ttk.Treeview(ed, columns=[c for c, _ in cols], show="headings", height=9, selectmode="extended")
        for c, w in cols:
            self.tv.heading(c, text=c); self.tv.column(c, width=w, anchor="w")
        self.tv.pack(fill="both", expand=True, padx=8)
        self.tv.bind("<Double-1>", self.trim)
        self.info = tk.StringVar(value="Add clips to begin.")
        ttk.Label(ed, textvariable=self.info).pack(anchor="w", padx=8, pady=2)

        f = ttk.LabelFrame(ed, text="Export settings"); f.pack(fill="x", padx=8, pady=4)
        self.enc = tk.StringVar(); self.res = tk.StringVar(value="1080p"); self.fps = tk.StringVar(value="Auto (max of clips, cap 60)")
        self.speed = ttk.Combobox(f, state="readonly", width=22, values=["Fastest", "Balanced", "Best quality"]); self.speed.current(1)
        self.ab = tk.StringVar(value="192k"); self.qv = tk.DoubleVar(value=21)
        self.yt, self.cap, self.hw = tk.BooleanVar(value=True), tk.BooleanVar(value=True), tk.BooleanVar(value=False)
        self.encbox = ttk.Combobox(f, textvariable=self.enc, state="readonly", width=44)
        rows = [("Encoder", self.encbox), ("Resolution", ttk.Combobox(f, textvariable=self.res, state="readonly", width=26,
                values=["Auto (largest clip)", "720p", "1080p", "1440p (YouTube VP9 boost)", "2160p (4K)"])),
                ("Frame rate", ttk.Combobox(f, textvariable=self.fps, state="readonly", width=26,
                values=["Auto (max of clips, cap 60)", "24 fps", "30 fps", "60 fps"])),
                ("Speed vs quality", self.speed),
                ("Audio bitrate", ttk.Combobox(f, textvariable=self.ab, state="readonly", width=8, values=["128k", "192k", "256k", "384k"]))]
        for i, (t, w) in enumerate(rows):
            ttk.Label(f, text=t).grid(row=i % 3, column=(i // 3) * 2, sticky="w", padx=6, pady=3)
            w.grid(row=i % 3, column=(i // 3) * 2 + 1, sticky="w", padx=6, pady=3)
        ttk.Label(f, text="Quality (lower = better, bigger)").grid(row=3, column=0, sticky="w", padx=6)
        qf = ttk.Frame(f); qf.grid(row=3, column=1, columnspan=3, sticky="we")
        self.ql = ttk.Label(qf, text="21", width=3)
        ttk.Scale(qf, from_=14, to=32, variable=self.qv, length=300, command=lambda v: self.ql.config(text=f"{float(v):.0f}")).pack(side="left", padx=6)
        self.ql.pack(side="left")
        for i, (t, v) in enumerate([("YouTube-optimised (closed GOP, 0.5s keyframes, BT.709, faststart)", self.yt),
                                    ("Cap bitrate at YouTube-recommended level (saves storage)", self.cap),
                                    ("Hardware decoding (off = safest; on can be faster on some PCs)", self.hw)]):
            ttk.Checkbutton(f, text=t, variable=v).grid(row=4 + i, column=0, columnspan=4, sticky="w", padx=6)

        self.merge, self.v1, self.v2 = tk.BooleanVar(value=True), tk.StringVar(value="100"), tk.StringVar(value="100")
        af = ttk.Frame(f); af.grid(row=7, column=0, columnspan=4, sticky="w", padx=6, pady=2)
        ttk.Checkbutton(af, text="Merge multiple audio tracks into one (e.g. game + mic)", variable=self.merge).pack(side="left")
        ttk.Label(af, text="   Track 1 vol %").pack(side="left")
        ttk.Spinbox(af, from_=0, to=300, increment=5, textvariable=self.v1, width=5).pack(side="left")
        ttk.Label(af, text="  Track 2+ vol %").pack(side="left")
        ttk.Spinbox(af, from_=0, to=300, increment=5, textvariable=self.v2, width=5).pack(side="left")

        of = ttk.Frame(ed); of.pack(fill="x", padx=8, pady=6)
        self.out = tk.StringVar()
        ttk.Label(of, text="Output").pack(side="left")
        ttk.Entry(of, textvariable=self.out).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(of, text="Browse…", command=self.browse).pack(side="left")
        self.bexp = ttk.Button(of, text="▶  EXPORT", command=self.export); self.bexp.pack(side="left", padx=6)
        self.bcan = ttk.Button(of, text="Cancel", command=self.cancel, state="disabled"); self.bcan.pack(side="left")

        top = ttk.Frame(bn); top.pack(fill="x", padx=8, pady=8)
        ttk.Label(top, text="Encodes ~10s of your first clip (or a test pattern) with every working encoder using the current settings.", wraplength=900).pack(anchor="w")
        self.ssim = tk.BooleanVar(value=True)
        self.bbench = ttk.Button(top, text="Run speed test", command=self.run_bench); self.bbench.pack(side="left", pady=6)
        ttk.Checkbutton(top, text="Measure quality (SSIM)", variable=self.ssim).pack(side="left", padx=10)
        self.buse = ttk.Button(top, text="Use fastest", command=self.use_fastest, state="disabled"); self.buse.pack(side="left")
        bc = [("Encoder", 230), ("Time", 70), ("Speed", 80), ("Enc FPS", 80), ("Size", 90), ("Bitrate", 90), ("SSIM", 80)]
        self.bt = ttk.Treeview(bn, columns=[c for c, _ in bc], show="headings", height=14)
        for c, w in bc:
            self.bt.heading(c, text=c); self.bt.column(c, width=w, anchor="w")
        self.bt.pack(fill="both", expand=True, padx=8)
        self.bsum = tk.StringVar()
        ttk.Label(bn, textvariable=self.bsum, wraplength=950, justify="left").pack(anchor="w", padx=8, pady=8)

        self.txt = tk.Text(lg, wrap="none", height=20); self.txt.pack(fill="both", expand=True)
        self.pb = ttk.Progressbar(self, maximum=1000); self.pb.pack(fill="x", padx=8, pady=(4, 0))
        self.status = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.status).pack(anchor="w", padx=8, pady=4)

    def poll(self):
        while True:
            try:
                fn = self.q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as e:
                print("UI callback error:", e, file=sys.stderr)
        self.after(100, self.poll)

    def log(self, m):
        self.tail.append(m)
        self.q.put(lambda: (self.txt.insert("end", m + "\n"), self.txt.see("end")))

    def upd(self, p, s):
        self.pb["value"] = p * 1000; self.status.set(s)

    def set_busy(self, b):
        self.busy = b
        for w, on in [(self.bexp, not b), (self.bbench, not b), (self.bcan, b)]:
            w.config(state="normal" if on else "disabled")
        if not b:
            self.pb["value"] = 0

    def build_overlay(self, f):
        self.ov_on, self.ov_tpl = tk.BooleanVar(value=False), tk.StringVar(value="{date}  {name}")
        self.ov_datefmt, self.ov_src, self.ov_pos = tk.StringVar(value="%Y-%m-%d %H:%M"), tk.StringVar(value="File created"), tk.StringVar(value="Bottom-right")
        self.ov_font = tk.StringVar(value="Segoe UI" if "Segoe UI" in self.fonts else next(iter(self.fonts), ""))
        self.ov_size, self.ov_op, self.ov_margin, self.ov_secs = (tk.StringVar(value=v) for v in ("2.2", "70", "1.5", "0"))
        self.ov_shadow, self.ov_box = tk.BooleanVar(value=True), tk.BooleanVar(value=False)
        g = ttk.LabelFrame(f, text="File name / date overlay"); g.pack(fill="x", padx=8, pady=8)
        def put(r, c, t, w):
            ttk.Label(g, text=t).grid(row=r, column=c, sticky="w", padx=6, pady=4); w.grid(row=r, column=c + 1, sticky="w", padx=6, pady=4)
        ttk.Checkbutton(g, text="Show overlay on every clip", variable=self.ov_on).grid(row=0, column=0, columnspan=4, sticky="w", padx=6, pady=4)
        put(1, 0, "Text", ttk.Entry(g, textvariable=self.ov_tpl, width=46))
        ttk.Label(g, text="Tokens: {name}  {file}  {date}  {time}  {index}", foreground="#666").grid(row=2, column=1, columnspan=3, sticky="w", padx=6)
        put(3, 0, "Date format", ttk.Entry(g, textvariable=self.ov_datefmt, width=22))
        put(3, 2, "Date from", ttk.Combobox(g, textvariable=self.ov_src, state="readonly", width=40,
            values=["File created", "File modified", "Video metadata (falls back to file created)"]))
        put(4, 0, "Position", ttk.Combobox(g, textvariable=self.ov_pos, state="readonly", width=14, values=list(POS)))
        put(4, 2, "Show for first (sec, 0 = whole clip)", ttk.Spinbox(g, from_=0, to=600, increment=1, textvariable=self.ov_secs, width=6))
        self.fontbox = ttk.Combobox(g, textvariable=self.ov_font, state="readonly", width=22, values=list(self.fonts))
        put(5, 0, "Font", self.fontbox)
        fr = ttk.Frame(g); fr.grid(row=5, column=3, sticky="w")
        ttk.Button(fr, text="Font file…", command=self.pick_font).pack(side="left", padx=6)
        self.cbtn = tk.Button(fr, text="  Color  ", bg=self.ov_color, command=self.pick_color); self.cbtn.pack(side="left")
        put(6, 0, "Size (% of video height)", ttk.Spinbox(g, from_=0.5, to=10, increment=0.1, textvariable=self.ov_size, width=6))
        put(6, 2, "Opacity %", ttk.Spinbox(g, from_=5, to=100, increment=5, textvariable=self.ov_op, width=6))
        put(7, 0, "Margin (% of height)", ttk.Spinbox(g, from_=0, to=10, increment=0.5, textvariable=self.ov_margin, width=6))
        ttk.Checkbutton(g, text="Drop shadow", variable=self.ov_shadow).grid(row=8, column=0, sticky="w", padx=6)
        ttk.Checkbutton(g, text="Dark backdrop box", variable=self.ov_box).grid(row=8, column=1, sticky="w", padx=6)
        ttk.Button(g, text="Preview on selected clip", command=self.preview).grid(row=9, column=0, columnspan=2, sticky="w", padx=6, pady=8)
        ttk.Label(f, text="Overlay is burned in per clip. It forces the filter-graph export path (slightly slower than the fast identical-clip path).",
                  foreground="#666", wraplength=900).pack(anchor="w", padx=10)

    def pick_color(self):
        c = colorchooser.askcolor(color=self.ov_color, parent=self)[1]
        if c:
            self.ov_color = c; self.cbtn.config(bg=c)

    def pick_font(self):
        p = filedialog.askopenfilename(filetypes=[("Fonts", "*.ttf *.otf *.ttc"), ("All", "*.*")])
        if p:
            n = os.path.basename(p); self.fonts[n] = p; self.fontbox["values"] = list(self.fonts); self.ov_font.set(n)

    def preview(self):
        if not self.clips:
            return messagebox.showinfo("Preview", "Add a clip first.")
        if not self.ov_ok:
            return messagebox.showerror("Preview", "This FFmpeg build has no 'drawtext' filter. Install a full build (gyan.dev).")
        st = self.snap(); sel = self.tv.selection(); i = int(sel[0]) if sel else 0
        c = st["clips"][i]; W, H, fps = target(st); tmp = tempfile.mkdtemp()
        try:
            png = os.path.join(tmp, "p.png")
            vf = vchain(W, H, fps, overlay_filter(st["ov"], c, i, H, tmp), c, st["speed"] == 2) + ",scale=w=960:h=640:force_original_aspect_ratio=decrease"
            r = subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{min(1.0, c['dur'] / 2):.2f}", "-i", c["path"], "-frames:v", "1", "-vf", vf, png],
                               capture_output=True, text=True, errors="replace", cwd=tmp, creationflags=NOWIN)
            if r.returncode:
                return messagebox.showerror("Preview failed", r.stderr[-500:])
            img = tk.PhotoImage(file=png)
            w = tk.Toplevel(self); w.title(f"Preview - {c['name']}")
            lb = ttk.Label(w, image=img); lb.image = img; lb.pack()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def browse(self):
        p = filedialog.asksaveasfilename(defaultextension=".mp4", filetypes=[("MP4", "*.mp4")],
                                         initialdir=os.path.dirname(self.out.get()) or downloads(),
                                         initialfile=os.path.basename(self.out.get()))
        if p:
            self.out.set(p)

    # ---------- clip list ----------
    def add(self):
        fs = filedialog.askopenfilenames(filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.ts *.flv *.wmv"), ("All", "*.*")])
        if fs:
            self.status.set("Reading clips...")
            threading.Thread(target=self.probe_all, args=(fs,), daemon=True).start()

    def probe_all(self, fs):
        for f in fs:
            try:
                c = probe(f)
                self.q.put(lambda c=c: self.addclip(c))
            except Exception as e:
                self.log(f"Skipped {f}: {e}")
        self.q.put(lambda: self.status.set("Ready"))

    def addclip(self, c):
        self.clips.append(c)
        self.refresh()

    def refresh(self):
        self.tv.delete(*self.tv.get_children())
        for i, c in enumerate(self.clips):
            tr = f"{fmt(c['ss'])} - {fmt(c['to'] or c['dur'])}" if (c["ss"] or c["to"]) else ""
            self.tv.insert("", "end", iid=str(i), values=(i + 1, c["name"], fmt(c["dur"]), f"{c['w']}x{c['h']}", f"{c['fps']:.2f}", f"{c['na']} track" + ("s" if c["na"] != 1 else "") if c["na"] else "none", tr))
        tot = sum((c["to"] or c["dur"]) - c["ss"] for c in self.clips)
        same = len({c["sig"] for c in self.clips}) == 1 and not any(c["ss"] or c["to"] for c in self.clips)
        multi = sum(c["na"] >= 2 for c in self.clips)
        self.info.set(f"{len(self.clips)} clips, total {fmt(tot)}" + (f"  -  {multi} with 2+ audio tracks" if multi else "") +
                      ("  -  all identical: fast concat / stream copy available" if same and self.clips else
                       "  -  mixed formats: clips will be normalised (scale/pad/fps)" if self.clips else ""))

    def remove(self):
        for i in sorted((int(i) for i in self.tv.selection()), reverse=True):
            del self.clips[i]
        self.refresh()

    def clear(self):
        self.clips.clear(); self.refresh()

    def move(self, d):
        sel = sorted(int(i) for i in self.tv.selection())
        if not sel or (d < 0 and sel[0] == 0) or (d > 0 and sel[-1] == len(self.clips) - 1):
            return
        for i in (sel if d < 0 else reversed(sel)):
            self.clips[i], self.clips[i + d] = self.clips[i + d], self.clips[i]
        self.refresh(); self.tv.selection_set([str(i + d) for i in sel])

    def sort(self, how):
        if how == "name":
            self.clips.sort(key=lambda c: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", c["name"])])
        else:
            self.clips.sort(key=lambda c: os.path.getmtime(c["path"]))
        self.refresh()

    def trim(self, *_):
        sel = self.tv.selection()
        if not sel:
            return
        c = self.clips[int(sel[0])]
        s = simpledialog.askstring("Trim", f"{c['name']}  (length {fmt(c['dur'])})\nStart , End   (seconds or h:mm:ss; blank = keep)",
                                   initialvalue=f"{c['ss'] or ''},{c['to'] or ''}", parent=self)
        if s is None:
            return
        try:
            a, _, b = s.partition(",")
            ss, to = ptime(a) or 0.0, ptime(b)
            if to is not None and to >= c["dur"]:
                to = None
            if ss < 0 or (to is not None and to <= ss) or ss >= c["dur"]:
                raise ValueError
            c["ss"], c["to"] = ss, to
        except ValueError:
            messagebox.showerror("Trim", "Couldn't read that range.")
        self.refresh()

    # ---------- encoders ----------
    def do_detect(self):
        av = detect(self.log)
        dt = "drawtext" in run([FF, "-hide_banner", "-filters"]).stdout
        def done():
            self.ov_ok = dt
            if not dt:
                self.log("drawtext filter missing in this FFmpeg build - the name/date overlay is unavailable.")
            self.avail = av
            self.emap = {COPY: "copy"}
            self.emap.update({f"{LABEL[n]}  ({n})": n for n in av})
            self.encbox["values"] = list(self.emap)
            best = next((n for n in av if n.startswith("h264") or n == "libx264"), av[0] if av else None)
            self.enc.set(next((k for k, v in self.emap.items() if v == best), COPY))
            self.status.set("Ready - encoders found: " + (", ".join(av) or "none"))
        self.q.put(done)

    def snap(self):
        return dict(clips=[dict(c) for c in self.clips], enc=self.emap.get(self.enc.get(), "libx264"), res=self.res.get(), fps=self.fps.get(),
                    q=self.qv.get(), speed=self.speed.current(), ab=self.ab.get(), yt=self.yt.get(), cap=self.cap.get(), hw=self.hw.get(),
                    merge=self.merge.get(), v1=num(self.v1, 100) / 100, v2=num(self.v2, 100) / 100,
                    ov=dict(on=self.ov_on.get(), tpl=self.ov_tpl.get(), datefmt=self.ov_datefmt.get() or "%Y-%m-%d", src=self.ov_src.get(), pos=self.ov_pos.get(),
                            size=max(0.5, num(self.ov_size, 2.2)), opacity=min(100, max(5, num(self.ov_op, 70))), margin=max(0, num(self.ov_margin, 1.5)),
                            secs=max(0, num(self.ov_secs, 0)), color=self.ov_color, font=self.fonts.get(self.ov_font.get()),
                            shadow=self.ov_shadow.get(), box=self.ov_box.get()))

    # ---------- export ----------
    def export(self):
        if not self.clips:
            return messagebox.showwarning("ClipForge", "Add some clips first.")
        out = self.out.get().strip()
        if not out:
            return messagebox.showwarning("ClipForge", "Choose an output file.")
        self.set_busy(True)
        threading.Thread(target=self.do_export, args=(self.snap(), out), daemon=True).start()

    def fgargs(self, fg, tmp):
        if len(fg) <= 6000:  # avoid command-line length limits with hundreds of clips
            return ["-filter_complex", fg]
        p = os.path.join(tmp, "fg.txt"); open(p, "w", encoding="utf-8").write(fg)
        m = re.search(r"version n?(\d+)", run([FF, "-version"]).stdout)
        return ["-/filter_complex" if (not m or int(m[1]) >= 7) else "-filter_complex_script", p]

    def build(self, st, out, hw, tmp, tgt=None, off=0, seg=False):
        cl, enc, ov = st["clips"], st["enc"], st["ov"]
        W, H, fps = tgt or target(st)
        hq = st["speed"] == 2
        multi = st["merge"] and any(c["na"] >= 2 for c in cl)
        same = len({c["sig"] for c in cl}) == 1 and not any(c["ss"] or c["to"] for c in cl)
        if ov["on"] and not self.ov_ok:
            raise RuntimeError("This FFmpeg build has no 'drawtext' filter. Install a full build (gyan.dev) or turn the overlay off.")
        if enc == "copy" and not (same and not multi and not ov["on"]):
            raise RuntimeError("Stream copy needs identical clips, no trims, no overlay and no audio merging. Pick an encoder instead.")
        aud = (["-c:a", "flac", "-ar", "48000", "-ac", "2"] if seg else
               ["-c:a", "aac", "-b:a", st["ab"], "-ar", "48000", "-ac", "2", "-movflags", "+faststart"])
        v = vopts(enc, st["q"], st["speed"], fps, H, st["cap"], st["yt"], not seg) if enc != "copy" else []
        cmd = list(HEAD)
        if same and not ov["on"]:  # concat demuxer: one decoder, constant memory, fastest path
            lst = os.path.join(tmp, "list.txt")
            open(lst, "w", encoding="utf-8").write("\n".join(qpath(c["path"]) for c in cl))
            cmd += (["-hwaccel", "auto"] if hw else []) + ["-f", "concat", "-safe", "0", "-i", lst]
            if enc == "copy":
                return cmd + ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", out]
            if multi:
                fg = f"[0:v]{vchain(W, H, fps, '', cl[0], hq)}[v];" + mixparts(0, cl[0]["na"], st["v1"], st["v2"], "a")
                return cmd + self.fgargs(fg, tmp) + ["-map", "[v]", "-map", "[a]"] + v + aud + [out]
            return cmd + ["-vf", vchain(W, H, fps, "", cl[0], hq), "-map", "0:v:0", "-map", "0:a:0?"] + v + aud + [out]
        parts, cat = [], ""
        for i, c in enumerate(cl):
            d = (c["to"] or c["dur"]) - c["ss"]
            ovf = overlay_filter(ov, c, i + off, H, tmp) if ov["on"] else ""
            cmd += (["-hwaccel", "auto"] if hw else []) + (["-ss", f"{c['ss']:.3f}"] if c["ss"] else []) + (["-to", f"{c['to']:.3f}"] if c["to"] else []) + ["-i", c["path"]]
            parts.append(f"[{i}:v]{vchain(W, H, fps, ovf, c, hq)}[v{i}]")
            if c["audio"] and st["merge"] and c["na"] >= 2:
                parts.append(mixparts(i, c["na"], st["v1"], st["v2"], f"a{i}"))
            elif c["audio"]:
                parts.append(f"[{i}:a:0]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
            else:
                parts.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{d:.3f}[a{i}]")
            cat += f"[v{i}][a{i}]"
        fg = ";".join(parts + [f"{cat}concat=n={len(cl)}:v=1:a=1[v][a]"])
        return cmd + self.fgargs(fg, tmp) + ["-map", "[v]", "-map", "[a]"] + v + aud + [out]

    def runff(self, cmd, total, show=True, cwd=None, base=0.0, grand=None, t0g=None, label=""):
        self.proc = p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", creationflags=NOWIN | (0x4000 if os.name == "nt" else 0), cwd=cwd)  # 0x4000 = below-normal priority
        t = fr = size = 0
        t0 = time.perf_counter()
        for line in p.stdout:
            line = line.strip()
            m = re.fullmatch(r"([a-z_0-9]+)=\s*(\S*)", line)
            if not m:
                self.log(line); continue
            k, v = m.groups()
            if k in ("out_time_us", "out_time_ms") and v.lstrip("-").isdigit():
                t = max(t, int(v) / 1e6)
            elif k == "frame" and v.isdigit():
                fr = int(v)
            elif k == "total_size" and v.isdigit():
                size = int(v)
            elif k == "progress" and show and t > 0:
                now = time.perf_counter(); el = now - t0; gt = grand or total; done = base + t
                sp = done / (now - (t0g or t0)) if now > (t0g or t0) else 0
                s = f"{label}{min(1, done / gt):.0%}  |  {sp:.1f}x realtime  |  {fr / max(el, 1e-6):.0f} fps  |  ETA {fmt((gt - done) / sp if sp else 0)}"
                if not grand:
                    s += f"  |  est. size {size / t * total / 1e6:.0f} MB"
                self.q.put(lambda s=s, pr=min(1, done / gt): self.upd(pr, s))
        rc = p.wait()
        return rc, t, fr, time.perf_counter() - t0, size

    def try_batch(self, st, idx, seg, tmp, tgt, base, tot, t0, label):
        sub = dict(st, clips=[st["clips"][i] for i in idx])
        bd = sum((c["to"] or c["dur"]) - c["ss"] for c in sub["clips"])
        for hw in ([True, False] if st["hw"] else [False]):
            cmd = self.build(sub, seg, hw, tmp, tgt, idx[0], True)
            self.log("$ " + subprocess.list2cmdline(cmd))
            rc = self.runff(cmd, bd, cwd=tmp, base=base, grand=tot, t0g=t0, label=label)[0]
            if rc == 0 or self.stop:
                break
            self.log("Batch failed" + (" - retrying without hardware decoding" if hw else ""))
        return rc == 0 and not self.stop

    def export_segmented(self, st, out, tmp, tot):
        """Big exports: encode small batches one at a time (flat memory use), then join them with no re-encode."""
        cl, enc = st["clips"], st["enc"]
        tgt = target(st); W, H, fps = tgt
        need = (2 * rec_mbps(H, fps, enc) / 8 + 0.12) * tot * 1.25  # MB: segments + final file + lossless audio
        free = shutil.disk_usage(tmp).free / 1e6
        if free < need:
            raise RuntimeError(f"Not enough free disk space next to the output file: need about {need / 1000:.1f} GB, have {free / 1000:.1f} GB.")
        batches = [list(range(i, min(i + BATCH, len(cl)))) for i in range(0, len(cl), BATCH)]
        segs, skipped, base, t0 = [], [], 0.0, time.perf_counter()
        for bi, idx in enumerate(batches):
            lab = f"Batch {bi + 1}/{len(batches)}  |  "
            seg = os.path.join(tmp, f"seg{len(segs):05d}.mkv")
            if self.try_batch(st, idx, seg, tmp, tgt, base, tot, t0, lab):
                segs.append(seg)
            elif not self.stop:
                for i in idx:  # isolate a bad clip instead of failing the whole export
                    s1 = os.path.join(tmp, f"seg{len(segs):05d}.mkv")
                    if len(idx) > 1 and self.try_batch(st, [i], s1, tmp, tgt, base, tot, t0, lab):
                        segs.append(s1)
                    else:
                        skipped.append(cl[i]["name"]); self.log(f"SKIPPED unreadable clip: {cl[i]['name']}")
            if self.stop:
                return 1, 0, skipped
            base += sum((cl[i]["to"] or cl[i]["dur"]) - cl[i]["ss"] for i in idx)
        if not segs:
            return 1, 0, skipped
        lst = os.path.join(tmp, "join.txt")
        open(lst, "w", encoding="utf-8").write("\n".join(qpath(p) for p in segs))
        cmd = HEAD + ["-f", "concat", "-safe", "0", "-i", lst, "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", "-c:a", "aac", "-b:a", st["ab"],
                      "-ar", "48000", "-ac", "2"] + (["-tag:v", "hvc1"] if ("265" in enc or enc.startswith("hevc")) else []) + ["-movflags", "+faststart", out]
        self.log("$ " + subprocess.list2cmdline(cmd))
        self.q.put(lambda: self.status.set("Joining batches..."))
        rc = self.runff(cmd, tot, cwd=tmp, label="Joining  |  ")[0]
        return rc, time.perf_counter() - t0, skipped

    def do_export(self, st, out):
        tmp = None
        try:
            out = os.path.abspath(out)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            cl, enc = st["clips"], st["enc"]
            tot = sum((c["to"] or c["dur"]) - c["ss"] for c in cl)
            sig1 = len({c["sig"] for c in cl}) == 1
            demux = sig1 and not st["ov"]["on"] and not any(c["ss"] or c["to"] for c in cl)
            seg = enc != "copy" and not demux and len(cl) > BATCH
            tmp = tempfile.mkdtemp(prefix=".clipforge_", dir=os.path.dirname(out) if seg else None)
            self.stop, skipped = False, []
            if seg:
                rc, el, skipped = self.export_segmented(st, out, tmp, tot)
            else:
                hws = [True, False] if (st["hw"] and enc != "copy" and (demux or len(cl) <= BATCH)) else [False]
                for hw in hws:
                    cmd = self.build(st, out, hw, tmp)
                    self.log("$ " + subprocess.list2cmdline(cmd))
                    rc, t, fr, el, _ = self.runff(cmd, tot, cwd=tmp)
                    if rc == 0 or self.proc is None:
                        break
                    self.log("Encode failed" + (" - retrying without hardware decoding" if hw else ""))
            if rc == 0:
                mb = os.path.getsize(out) / 1e6
                msg = f"Done in {fmt(el)} ({tot / el:.1f}x realtime) - {mb:.0f} MB ({mb / (tot / 60):.0f} MB/min)"
                if skipped:
                    msg += f"\n\nSKIPPED {len(skipped)} unreadable clip(s):\n" + "\n".join(skipped[:10])
                self.q.put(lambda: self.finished(msg, out))
            else:
                err = "Cancelled" if self.stop else "\n".join(list(self.tail)[-4:])
                self.q.put(lambda: (self.set_busy(False), self.status.set("Export failed/cancelled"), messagebox.showerror("Export failed", err or "Cancelled")))
        except Exception as e:
            self.q.put(lambda e=e: (self.set_busy(False), messagebox.showerror("Error", str(e))))
        finally:
            self.proc = None
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)

    def finished(self, msg, out):
        self.set_busy(False); self.status.set(msg)
        if os.path.basename(out).startswith("clip_dump_"):
            self.out.set(default_out())  # next export gets a fresh name, never overwrites
        if messagebox.askyesno("Export finished", f"{msg}\n\n{out}\n\nOpen the folder?") and os.name == "nt":
            os.startfile(os.path.dirname(os.path.abspath(out)))

    def cancel(self):
        self.stop = True
        if self.proc:
            p, self.proc = self.proc, None
            p.terminate()

    # ---------- speed tester ----------
    def run_bench(self):
        if not self.avail:
            return messagebox.showwarning("ClipForge", "No working encoders detected yet.")
        self.bt.delete(*self.bt.get_children()); self.bsum.set(""); self.set_busy(True)
        threading.Thread(target=self.do_bench, args=(self.snap(), list(self.avail), self.ssim.get()), daemon=True).start()

    def do_bench(self, st, encs, use_ssim):
        tmp = tempfile.mkdtemp(); res = []
        try:
            cl = st["clips"]
            _, H, fps = target(st)
            if cl:
                c = cl[0]
                src = ["-ss", f"{c['dur'] * .3:.2f}" if c["dur"] > 20 else "0", "-t", "10", "-i", c["path"]]
            else:
                src = ["-f", "lavfi", "-t", "10", "-i", "testsrc2=s=1920x1080:r=60"]
            chain = f"fps={fps},scale=-2:{H}:flags=bicubic,format=yuv420p"
            for n, enc in enumerate(encs):
                self.q.put(lambda enc=enc, n=n: self.upd(n / len(encs), f"Testing {LABEL[enc]} ({n + 1}/{len(encs)})..."))
                out = os.path.join(tmp, enc + ".mp4")
                for hw in ([True, False] if (st["hw"] and cl) else [False]):
                    cmd = HEAD + (["-hwaccel", "auto"] if hw else []) + src + ["-vf", chain] + \
                          vopts(enc, st["q"], st["speed"], fps, H, st["cap"], st["yt"]) + ["-an", out]
                    rc, t, fr, el, _ = self.runff(cmd, 10, show=False)
                    if rc == 0:
                        break
                if rc != 0 or not os.path.exists(out) or t <= 0:
                    self.q.put(lambda enc=enc: self.bt.insert("", "end", values=(LABEL[enc], "failed", "", "", "", "", "")))
                    continue
                sz = os.path.getsize(out); br = sz * 8 / t / 1e6
                ss = None
                if use_ssim:
                    r = run([FF, "-hide_banner", "-nostdin", "-i", out] + src + ["-lavfi", f"[0:v]format=yuv420p[d];[1:v]{chain}[r];[d][r]ssim", "-f", "null", "-"])
                    m = re.search(r"All:([\d.]+)", r.stderr)
                    ss = float(m[1]) if m else None
                row = dict(enc=enc, el=el, x=t / el, fps=fr / el, mb=sz / 1e6, br=br, ssim=ss)
                res.append(row)
                self.q.put(lambda r=row: self.bt.insert("", "end", values=(LABEL[r["enc"]], f"{r['el']:.1f}s",
                           f"{r['x']:.2f}x", f"{r['fps']:.0f}", f"{r['mb']:.1f} MB", f"{r['br']:.1f} Mbps", f"{r['ssim']:.4f}" if r["ssim"] else "-")))
                try: os.remove(out)
                except OSError: pass
            if res:
                fast = max(res, key=lambda r: r["x"])
                s = f"Fastest: {LABEL[fast['enc']]} at {fast['x']:.1f}x realtime ({fast['fps']:.0f} fps)."
                q = [r for r in res if r["ssim"]]
                if q:
                    eff = max(q, key=lambda r: -math.log10(max(1 - r["ssim"], 1e-6)) / r["br"])
                    s += f"   Best quality per MB: {LABEL[eff['enc']]}."
                s += "\nNote: speeds are for your current settings; same CQ value is not exactly equal quality across encoders - compare SSIM."
                self.fastest = fast["enc"]
            else:
                s = "No encoder finished - check the Log tab."
            self.q.put(lambda: (self.bsum.set(s), self.buse.config(state="normal" if res else "disabled"), self.set_busy(False), self.status.set("Speed test complete")))
        except Exception as e:
            self.q.put(lambda e=e: (self.set_busy(False), messagebox.showerror("Error", str(e))))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def use_fastest(self):
        k = next((k for k, v in self.emap.items() if v == self.fastest), None)
        if k:
            self.enc.set(k)


if __name__ == "__main__":
    App().mainloop()
