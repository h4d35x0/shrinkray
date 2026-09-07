#!/usr/bin/env python3
"""shrinkray, with a window.

A folder picker, three presets, and a Start button. It drives the same scripts
the command line uses rather than reimplementing any of them, so there is one
encoder path and one verification path to get right.

Tkinter ships with Python, so this adds no dependencies. ffmpeg is still
required and is checked for at startup, because a missing ffmpeg is the failure
a non-technical user is most likely to hit and least likely to diagnose.
"""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# Windows gives every child process its own console window, which means one
# flashing window per ffmpeg and ffprobe call when a GUI drives this. The flag
# does not exist off Windows, hence the getattr default.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

HERE = Path(__file__).resolve().parent

FFMPEG_HELP = "https://ffmpeg.org/download.html"

# The point of presets is that nobody should need to know what CQ means.
PRESETS = {
    "Talk or lecture": {
        "blurb": "A person speaking, slides. Smallest files.",
        "args": ["--cq", "34", "--audio-kbps", "64", "--audio-channels", "1"],
    },
    "Music or performance": {
        "blurb": "Stereo sound at a real bitrate, more detail kept in motion.",
        "args": ["--cq", "30", "--audio-kbps", "160", "--audio-channels", "2"],
    },
    "Camera footage": {
        "blurb": "General video. Balanced quality and size.",
        "args": ["--cq", "30", "--audio-kbps", "128", "--audio-channels", "2"],
    },
}

# The output-size model lives in transcode.py so the window and the command
# line cannot drift apart on what they promise.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from transcode import predicted_kbps  # noqa: E402

# How many files to probe for a representative bitrate. Probing every file in a
# large library would make the window sit there doing nothing.
ESTIMATE_SAMPLE = 15

QUALITY = {
    "Smaller files": 4,
    "Balanced": 0,
    "Better quality": -4,
}


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n:.0f} B"
        n /= 1024
    return f"{n:.1f} TB"


def human_time(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("shrinkray")
        self.minsize(720, 560)

        self.source = tk.StringVar()
        self.dest = tk.StringVar()
        self.preset = tk.StringVar(value="Talk or lecture")
        self.quality = tk.StringVar(value="Balanced")
        self.make_audio = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Choose a folder of video to begin.")

        self.msgs: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        self.total = 0

        self._build()
        self.after(100, self._drain)
        self.after(200, self._check_ffmpeg)

    # ---------- layout ----------

    def _build(self) -> None:
        pad = {"padx": 12, "pady": 6}
        self.columnconfigure(0, weight=1)

        box = ttk.LabelFrame(self, text="1. What do you want to shrink?")
        box.grid(row=0, column=0, sticky="ew", **pad)
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Folder of video").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(box, textvariable=self.source).grid(row=0, column=1, sticky="ew", pady=6)
        ttk.Button(box, text="Choose...", command=self._pick_source).grid(
            row=0, column=2, padx=8, pady=6)

        ttk.Label(box, text="Save shrunk copy to").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(box, textvariable=self.dest).grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Button(box, text="Choose...", command=self._pick_dest).grid(
            row=1, column=2, padx=8, pady=6)

        box2 = ttk.LabelFrame(self, text="2. What kind of video is it?")
        box2.grid(row=1, column=0, sticky="ew", **pad)
        box2.columnconfigure(1, weight=1)

        ttk.Label(box2, text="Type").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        combo = ttk.Combobox(box2, textvariable=self.preset, state="readonly",
                             values=list(PRESETS))
        combo.grid(row=0, column=1, sticky="ew", pady=6)
        combo.bind("<<ComboboxSelected>>", lambda _e: self._on_preset())

        self.blurb = ttk.Label(box2, text=PRESETS["Talk or lecture"]["blurb"],
                               foreground="#666")
        self.blurb.grid(row=1, column=1, sticky="w", pady=(0, 6))

        ttk.Label(box2, text="Quality").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        qcombo = ttk.Combobox(box2, textvariable=self.quality, state="readonly",
                              values=list(QUALITY))
        qcombo.grid(row=2, column=1, sticky="ew", pady=6)
        # Quality shifts the target bitrate, so the estimate has to follow it.
        qcombo.bind("<<ComboboxSelected>>", lambda _e: self._estimate())

        ttk.Checkbutton(box2, text="Also make audio-only copies, for listening",
                        variable=self.make_audio).grid(
            row=3, column=1, sticky="w", pady=(0, 8))

        box3 = ttk.LabelFrame(self, text="3. Go")
        box3.grid(row=2, column=0, sticky="ew", **pad)
        box3.columnconfigure(0, weight=1)

        self.estimate = ttk.Label(box3, text="", foreground="#666")
        self.estimate.grid(row=0, column=0, sticky="w", padx=8, pady=(6, 0))

        self.bar = ttk.Progressbar(box3, mode="determinate")
        self.bar.grid(row=1, column=0, sticky="ew", padx=8, pady=6)

        ttk.Label(box3, textvariable=self.status).grid(
            row=2, column=0, sticky="w", padx=8)

        btns = ttk.Frame(box3)
        btns.grid(row=3, column=0, sticky="e", padx=8, pady=8)
        self.start_btn = ttk.Button(btns, text="Start", command=self._start)
        self.start_btn.pack(side="left", padx=4)
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self._cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=4)

        logbox = ttk.LabelFrame(self, text="Details")
        logbox.grid(row=3, column=0, sticky="nsew", **pad)
        self.rowconfigure(3, weight=1)
        logbox.columnconfigure(0, weight=1)
        logbox.rowconfigure(0, weight=1)

        self.log = tk.Text(logbox, height=10, wrap="none", state="disabled",
                           font=("Consolas", 9))
        self.log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(logbox, command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=sb.set)

    # ---------- helpers ----------

    def _say(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _on_preset(self) -> None:
        self.blurb.configure(text=PRESETS[self.preset.get()]["blurb"])
        self._estimate()

    def _pick_source(self) -> None:
        d = filedialog.askdirectory(title="Folder containing your video files")
        if d:
            self.source.set(d)
            if not self.dest.get():
                self.dest.set(str(Path(d).parent / (Path(d).name + " (shrunk)")))
            self._estimate()

    def _pick_dest(self) -> None:
        d = filedialog.askdirectory(title="Where to save the shrunk copy")
        if d:
            self.dest.set(d)

    def _check_ffmpeg(self) -> None:
        if shutil.which("ffmpeg") and shutil.which("ffprobe"):
            return
        if messagebox.askyesno(
            "ffmpeg is required",
            "shrinkray uses ffmpeg to do the actual encoding, and it was not "
            "found on this computer.\n\n"
            "It is free. Open the download page now?",
        ):
            webbrowser.open(FFMPEG_HELP)
        self.status.set("ffmpeg not found. Install it, then restart shrinkray.")
        self.start_btn.configure(state="disabled")

    def _preset_target_kbps(self) -> float:
        args = PRESETS[self.preset.get()]["args"]
        cq = int(args[args.index("--cq") + 1]) + QUALITY[self.quality.get()]
        audio = int(args[args.index("--audio-kbps") + 1])
        return predicted_kbps(cq, audio)

    def _estimate(self) -> None:
        """Kick off a sampled measurement; the answer arrives via the queue."""
        src = self.source.get()
        if not src or not Path(src).is_dir():
            return
        exts = {".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm", ".ts", ".mpg", ".mpeg"}
        files = [p for p in Path(src).rglob("*")
                 if p.is_file() and p.suffix.lower() in exts]
        if not files:
            self.estimate.configure(text="No video files found in that folder.")
            return
        self.estimate.configure(text=f"{len(files)} videos. Measuring...")
        target = self._preset_target_kbps()
        threading.Thread(target=self._measure, args=(files, target),
                         daemon=True).start()

    def _measure(self, files: list[Path], target: float) -> None:
        """Probe a sample to learn the real source bitrate, then predict."""
        total_bytes = sum(p.stat().st_size for p in files)
        step = max(1, len(files) // ESTIMATE_SAMPLE)
        sample = files[::step][:ESTIMATE_SAMPLE]

        secs = 0.0
        sampled_bytes = 0
        for p in sample:
            try:
                r = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=nw=1:nk=1", str(p)],
                    capture_output=True, text=True, timeout=30,
                    creationflags=NO_WINDOW)
                d = float(r.stdout.strip())
            except (ValueError, OSError, subprocess.SubprocessError):
                continue
            if d > 0:
                secs += d
                sampled_bytes += p.stat().st_size

        if secs <= 0:
            self.msgs.put(("estimate",
                           f"{len(files)} videos, {human_bytes(total_bytes)}. "
                           f"Could not read them to estimate."))
            return

        src_kbps = sampled_bytes * 8 / secs / 1000
        # Never predict a file larger than it already is.
        out_kbps = min(src_kbps, target)
        total_secs = total_bytes * 8 / (src_kbps * 1000)
        out_bytes = total_secs * out_kbps * 1000 / 8

        if src_kbps <= target * 1.15:
            text = (f"{len(files)} videos, {human_bytes(total_bytes)}. "
                    f"These are already compressed ({src_kbps:.0f} kbps). "
                    f"Shrinking them will save little and lose quality.")
        else:
            text = (f"{len(files)} videos, {human_bytes(total_bytes)} now, "
                    f"roughly {human_bytes(out_bytes)} after "
                    f"({out_bytes / total_bytes * 100:.0f}%). Estimate only.")
        self.msgs.put(("estimate", text))

    # ---------- running ----------

    def _start(self) -> None:
        src, dst = self.source.get().strip(), self.dest.get().strip()
        if not src or not Path(src).is_dir():
            messagebox.showerror("Pick a folder", "Choose the folder holding your video.")
            return
        if not dst:
            messagebox.showerror("Pick a destination", "Choose where to save the copy.")
            return
        if Path(dst).resolve() == Path(src).resolve():
            messagebox.showerror(
                "Same folder",
                "The destination must be a different folder from the source.")
            return

        self.cancelled = False
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.bar.configure(value=0, mode="indeterminate")
        self.bar.start(12)
        self.worker = threading.Thread(target=self._run, args=(src, dst), daemon=True)
        self.worker.start()

    def _cancel(self) -> None:
        self.cancelled = True
        self.status.set("Stopping after the current file...")
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _spawn(self, args: list[str], cwd: Path) -> int:
        """Run one toolkit script, streaming its output into the queue."""
        self.proc = subprocess.Popen(
            [sys.executable, "-u", *args], cwd=str(cwd),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", bufsize=1,
            creationflags=NO_WINDOW,
        )
        for line in self.proc.stdout:
            self.msgs.put(("log", line.rstrip()))
            self.msgs.put(("progress", line))
        self.proc.wait()
        return self.proc.returncode

    def _run(self, src: str, dst: str) -> None:
        try:
            work = Path(dst)
            work.mkdir(parents=True, exist_ok=True)
            lib = work / "library.json"

            self.msgs.put(("status", "Looking at your videos..."))
            rc = self._spawn(
                [str(HERE / "build_index.py"), "--scan", "--root", src,
                 "--out", str(work)], HERE)
            if rc != 0 or self.cancelled:
                return self.msgs.put(("done", "Stopped." if self.cancelled
                                      else "Could not read that folder."))

            self.total = len(json.loads(lib.read_text(encoding="utf-8")))
            self.msgs.put(("total", self.total))
            self.msgs.put(("status", f"Shrinking {self.total} videos. "
                                     f"This takes a while; you can leave it running."))

            preset = PRESETS[self.preset.get()]
            cq = int(preset["args"][preset["args"].index("--cq") + 1])
            cq += QUALITY[self.quality.get()]
            args = [a for a in preset["args"]]
            args[args.index("--cq") + 1] = str(cq)

            rc = self._spawn(
                [str(HERE / "transcode.py"), "--library", str(lib),
                 "--out", str(work), *args], HERE)
            if self.cancelled:
                return self.msgs.put(("done", "Stopped. Run again to pick up where "
                                              "it left off; finished files are kept."))
            if rc != 0:
                return self.msgs.put(("done", "Finished with some problems. "
                                              "See Details above."))

            self.msgs.put(("status", "Building the index page..."))
            self._spawn([str(HERE / "make_index.py"), "--library", str(lib),
                         "--out", str(work), "--title", Path(src).name], HERE)

            if self.make_audio.get() and not self.cancelled:
                self.msgs.put(("status", "Making audio-only copies..."))
                self._spawn([str(HERE / "extract_audio.py"), "--library", str(lib),
                             "--video", str(work), "--out", str(work / "audio")], HERE)

            self.msgs.put(("done", "Finished."))
        except Exception as exc:                      # noqa: BLE001
            self.msgs.put(("log", f"ERROR: {exc}"))
            self.msgs.put(("done", "Stopped because of an unexpected error."))

    # ---------- ui pump ----------

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.msgs.get_nowait()
                if kind == "estimate":
                    self.estimate.configure(text=str(payload))
                elif kind == "log":
                    self._say(str(payload))
                elif kind == "status":
                    self.status.set(str(payload))
                elif kind == "total":
                    self.bar.stop()
                    self.bar.configure(mode="determinate", maximum=int(payload), value=0)
                elif kind == "progress":
                    line = str(payload)
                    if line.startswith("[") and "/" in line:
                        try:
                            n = int(line[1:line.index("/")])
                            self.bar.configure(value=n)
                            self.status.set(f"Shrinking video {n} of {self.total}...")
                        except ValueError:
                            pass
                elif kind == "done":
                    self.bar.stop()
                    if self.bar["mode"] == "indeterminate":
                        self.bar.configure(mode="determinate", value=0)
                    self.status.set(str(payload))
                    self.start_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    if str(payload) == "Finished.":
                        self.bar.configure(value=self.bar["maximum"])
                        if messagebox.askyesno(
                            "Done", "Your shrunk copy is ready.\n\nOpen the folder?"
                        ):
                            self._open_folder(self.dest.get())
        except queue.Empty:
            pass
        self.after(100, self._drain)

    @staticmethod
    def _open_folder(path: str) -> None:
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", str(Path(path))])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError:
            pass


if __name__ == "__main__":
    App().mainloop()
