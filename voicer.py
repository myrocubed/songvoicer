"""Song Voicer: STILL IN BETA

Run:   python voicer.py
Needs: pip install numpy sounddevice soundfile pillow
Songs live in maps/<song>/ (a *.map.json, its audio, sprites/ and a .txt with the lyrics).
"""
import difflib
import glob
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
import zlib
from tkinter import filedialog, messagebox, ttk


def data_dir():
    """Where maps, lyrics and error.log live.

    From source that's this folder. A packaged app keeps them somewhere easy to find, so people can drop
    song folders in: next to the .exe / Linux executable, or Documents/Song Voicer on a Mac (an .app can't
    be written into). The songs bundled with the app are copied there on first run.
    """
    if not getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(__file__))
    if sys.platform == "darwin":
        path = os.path.expanduser("~/Documents/Song Voicer")
    else:
        path = os.path.dirname(sys.executable)
        if not os.access(path, os.W_OK):  # e.g. installed under Program Files
            base = os.environ.get("APPDATA") or os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~")
            path = os.path.join(base, "Song Voicer")
    bundled = os.path.join(sys._MEIPASS, "maps")
    if not os.path.isdir(os.path.join(path, "maps")) and os.path.isdir(bundled):
        shutil.copytree(bundled, os.path.join(path, "maps"))
    os.makedirs(os.path.join(path, "maps"), exist_ok=True)
    return path


DATA = data_dir()
ERROR_LOG = os.path.join(DATA, "error.log")
SETTINGS = os.path.join(DATA, "settings.json")


def report_error(exc_type, exc, tb):
    """Launched with pythonw there is no console, so save crashes to error.log and show them."""
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    with open(ERROR_LOG, "a", encoding="utf-8") as f:
        f.write(text + "\n")
    messagebox.showerror("Song Voicer error", f"{exc_type.__name__}: {exc}\n\nFull details are in error.log.")


sys.excepthook = report_error

if getattr(sys, "frozen", False) and sys.platform.startswith("linux"):
    # sounddevice finds PortAudio via ctypes.util.find_library, which can't see libraries bundled
    # inside the packaged app; point it at the bundled copy.
    import ctypes.util
    _bundled_pa = glob.glob(os.path.join(sys._MEIPASS, "libportaudio.so*"))
    if _bundled_pa:
        _find_library = ctypes.util.find_library
        ctypes.util.find_library = lambda name: _bundled_pa[0] if name == "portaudio" else _find_library(name)

import numpy as np
import sounddevice as sd
import soundfile as sf
from PIL import Image, ImageDraw, ImageTk

import net
import theme
from editor import EDITOR_EVENTS, EditorMixin
from fill_lyrics import parse_lyrics
from theme import FONT, H, W, Button

MAPS = os.path.join(DATA, "maps")
PAD = 0.25           # seconds of your recording kept around each of your lines
METER_DB = 50        # the Settings level meter shows -50 dB .. 0 dB
FADE = 0.03          # crossfade length in seconds
ENV_RATE = 50        # waveform points per second
WAVE_SECONDS = 6.0   # time span visible in the waveform
SPRITE_H = 230
TEXT_DARK = theme.TEXT
CYAN = "#5abed4"


def singers(line):
    return line["char"] if isinstance(line["char"], list) else [line["char"]]


def load_stereo(path):
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    return data[:, :2], sr


def envelope(mono, sr, rate):
    hop = sr // rate
    n = len(mono) // hop
    return np.abs(mono[:n * hop]).reshape(n, hop).max(axis=1)


def ramp_window(arr, a, b, f, mode):
    """Raise (mode="max") or cut (mode="min") arr to a soft-edged window over [a, b)."""
    a0, b1 = max(0, a - f), min(len(arr), b + f)
    if b1 <= a0:
        return
    t = np.arange(a0, b1, dtype=np.float32)
    w = np.clip(np.minimum((t - (a - f)) / f, ((b + f) - t) / f), 0, 1)
    seg = arr[a0:b1]
    if mode == "max":
        np.maximum(seg, w, out=seg)
    else:
        np.minimum(seg, 1 - w, out=seg)


def silhouette(height):
    w = int(height * 0.62)
    img = Image.new("RGBA", (w, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([w * 0.27, height * 0.05, w * 0.73, height * 0.38], fill=(96, 98, 112, 255))
    d.rounded_rectangle([w * 0.12, height * 0.42, w * 0.88, height * 0.98], int(w * 0.2), fill=(96, 98, 112, 255))
    return img


def fade_frames(img, height, steps=10):
    img = img.resize((max(1, round(img.width * height / img.height)), height), Image.LANCZOS)
    alpha = img.getchannel("A")
    frames = []
    for k in range(steps + 1):
        f = img.copy()
        f.putalpha(alpha.point(lambda v, k=k: v * k // steps))
        frames.append(ImageTk.PhotoImage(f))
    return frames


class Song:
    def __init__(self, folder):
        self.folder = folder
        with open(glob.glob(os.path.join(folder, "*.map.json"))[0], encoding="utf-8") as f:
            self.data = json.load(f)
        self.title = self.data["title"]
        self.artist = self.data.get("artist", "")
        self.characters = self.data["characters"]
        self.chars = {c["id"]: c for c in self.characters}
        self.lines = self.data["lines"]
        self.map_text = [line.get("text", "") for line in self.lines]
        self.lyrics_path = os.path.join(folder, "lyrics.txt")
        self.lyrics_status = self.load_lyrics()
        self.key = os.path.basename(folder)  # how rooms refer to this song
        self.sr = None
        self.mask_cache = {}

    def lyrics_file(self):
        """lyrics.txt if present, otherwise any .txt file in the map folder."""
        if os.path.exists(self.lyrics_path):
            return self.lyrics_path
        found = sorted(glob.glob(os.path.join(self.folder, "*.txt")))
        return found[0] if found else None

    def load_lyrics(self):
        """Fill line texts from the folder's lyrics file. Returns "ok", "missing" or a warning."""
        for i, line in enumerate(self.lines):
            line["text"] = self.map_text[i] or f"(line {i + 1})"
        # Lines whose text is already in the map (like an announcer) take no lyrics.
        open_idx = [i for i, text in enumerate(self.map_text) if not text]
        if not open_idx:
            return "ok"
        path = self.lyrics_file()
        if not path:
            return "missing"
        name = os.path.basename(path)
        # Headers can be "[MONIKA]" or Genius-style "[Verse 1: EpicLLOYD as Adolf Hitler]";
        # lines under headers naming nobody in the map, or only preset characters, are skipped.
        preset_only = {cid for cid in self.chars if not any(cid in singers(self.lines[i]) for i in open_idx)}
        lyrics = [(self.resolve_singers(chars), text) for chars, text in parse_lyrics(path)]
        lyrics = [(chars, text) for chars, text in lyrics if chars and not chars <= preset_only]
        if not lyrics:
            return f"No lyrics with [CHARACTER] headers found in {name}."

        # Line counts don't have to match: line up the singer order of the lyrics with the map's,
        # so extra or missing lines only affect their own spot.
        # Solo lines match by singer; any shared line ("[ALL GIRLS]", "[Chorus: All]") matches any group line.
        key = lambda chars: next(iter(chars)) if len(chars) == 1 else "group"
        want = [key(singers(self.lines[i])) for i in open_idx]
        got = [key(chars) for chars, _ in lyrics]
        owner = {}  # lyric index -> map line index
        for a, b, n in difflib.SequenceMatcher(None, want, got, autojunk=False).get_matching_blocks():
            for k in range(n):
                owner[b + k] = open_idx[a + k]
                self.lines[open_idx[a + k]]["text"] = lyrics[b + k][1]
        filled = len(owner)
        # A leftover lyric line by the same singer is usually one map line split in two: join it on.
        skipped = 0
        for b, (chars, text) in enumerate(lyrics):
            if b in owner:
                continue
            if b - 1 in owner and lyrics[b - 1][0] == chars:
                owner[b] = owner[b - 1]
                self.lines[owner[b]]["text"] += " " + text
            else:
                skipped += 1
        if filled == len(open_idx) and not skipped:
            return "ok"
        msg = f"Loaded {filled} of {len(open_idx)} lines from {name}."
        if skipped:
            msg += f" {skipped} extra lyric line(s) skipped."
        if filled < len(open_idx):
            msg += f" {len(open_idx) - filled} line(s) without lyrics show as (line N)."
        return msg

    def resolve_singers(self, header_chars):
        """Map a lyrics header ("[Verse 1: EpicLLOYD as Adolf Hitler]", "[Chorus: All]") to character ids."""
        words = set(re.findall(r"[a-z0-9']+", " ".join(header_chars)))
        if words & {"all", "everyone", "both", "together", "group"}:
            return set(self.chars)
        return {cid for cid, c in self.chars.items()
                if cid in words or any(len(w) > 2 and w in words for w in re.findall(r"[a-z0-9']+", c["name"].lower()))}

    def playable(self):
        return bool(self.characters and self.lines) and not self.missing_audio()

    def missing_audio(self):
        return [self.data[k] for k in ("instrumental", "guide")
                if not os.path.exists(os.path.join(self.folder, self.data[k]))]

    def sprite(self, cid):
        c = self.chars[cid]
        if c.get("sprite"):
            return Image.open(os.path.join(self.folder, c["sprite"])).convert("RGBA")
        return silhouette(SPRITE_H)

    def line_count(self, cid):
        return sum(cid in singers(line) for line in self.lines)

    def load_audio(self):
        if self.sr:
            return
        inst, sr = load_stereo(os.path.join(self.folder, self.data["instrumental"]))
        voc, _ = load_stereo(os.path.join(self.folder, self.data["guide"]))
        n = max(len(inst), len(voc))
        self.inst = np.pad(inst, ((0, n - len(inst)), (0, 0)))
        self.voc = np.pad(voc, ((0, n - len(voc)), (0, 0)))
        self.sr = sr
        self.length = n / sr
        mono = self.voc.mean(axis=1)
        self.env = envelope(mono, sr, ENV_RATE)
        self.env /= max(1e-6, np.percentile(self.env, 99.5))
        self.env100 = envelope(mono, sr, 100)
        self.bin_colors = ["#5a5a5a"] * len(self.env)
        for line in self.lines:
            who = singers(line)
            color = self.chars[who[0]]["color"] if len(who) == 1 else "#e8e8e8"
            for i in range(int(line["t"] * ENV_RATE), min(len(self.env), int(line["end"] * ENV_RATE))):
                self.bin_colors[i] = color

    def masks(self, me):
        """Gain for the original vocals (0 where `me` sings alone) and the window where your mic is kept.

        The original singer often holds a note past the line's end time, so the mute runs until the
        vocal track goes quiet or the next line without you starts.
        """
        if me in self.mask_cache:
            return self.mask_cache[me]
        sr, n, f = self.sr, len(self.inst), int(FADE * self.sr)
        orig = np.ones(n, np.float32)
        mic = np.zeros(n, np.float32)
        quiet = 0.1 * np.percentile(self.env100, 99)
        for k, line in enumerate(self.lines):
            who = singers(line)
            if me not in who:
                continue
            cap = next((l["t"] for l in self.lines[k + 1:] if me not in singers(l)), self.length)
            e = line["end"]
            while e < cap - 0.05 and self.env100[min(int(e * 100), len(self.env100) - 1)] > quiet:
                e += 0.01
            e = max(line["end"], min(e + 0.08, cap - 0.03))
            a, b = int(line["t"] * sr), int(e * sr)
            ramp_window(mic, a - int(PAD * sr), b + int(PAD * sr), f, "max")
            if who == [me]:
                ramp_window(orig, a - int(0.1 * sr), b, f, "min")
        self.mask_cache[me] = orig, mic
        return orig, mic


def soft_clip(x, knee=0.85):
    """Leave everything below `knee` untouched and squeeze louder peaks smoothly under 1.0 (no distortion)."""
    y = x.copy()
    over = np.abs(x) > knee
    y[over] = np.sign(x[over]) * (knee + (1 - knee) * np.tanh((np.abs(x[over]) - knee) / (1 - knee)))
    return y


def encode_voice(voice, window):
    """Pack the parts of a voice track inside `window` as (segments, zlib'd int16 bytes) for upload."""
    edges = np.flatnonzero(np.diff(np.concatenate(([0], (window > 0).astype(np.int8), [0]))))
    runs = list(zip(edges[::2], edges[1::2]))
    pcm = np.concatenate([voice[a:b] for a, b in runs]) if runs else np.zeros(0, np.float32)
    data = zlib.compress((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes(), 6)
    return [[int(a), int(b - a)] for a, b in runs], data


def decode_voice(segments, data, n):
    pcm = np.frombuffer(zlib.decompress(data), "<i2").astype(np.float32) / 32767 if data else np.zeros(0)
    voice = np.zeros(n, np.float32)
    i = 0
    for start, length in segments:
        length = max(0, min(length, n - start))
        voice[start:start + length] = pcm[i:i + length]
        i += length
    return voice


class App(EditorMixin):
    def __init__(self, root):
        self.root = root
        root.title("Song Voicer")
        root.resizable(False, False)
        bundle = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        icon = os.path.join(bundle, "assets", "icon.png")
        if os.path.exists(icon):
            self.icon = tk.PhotoImage(file=icon)
            root.iconphoto(True, self.icon)
        self.c = tk.Canvas(root, width=W, height=H, highlightthickness=0, bg="#f4f4f4")
        self.c.pack()
        self.widgets, self.images = [], []
        self.gen = 0
        self.stream = None
        self.level = 0.0
        self.pos = 0
        self.finished = False
        self.rec = self.cover = None
        self.latency = 0.0
        self.maps_dir = MAPS
        self.song = None
        self.reload_songs()
        self.guide_vol = tk.DoubleVar(value=0.8)
        self.mic_gain = tk.DoubleVar(value=1.5)  # your voice in the cover, relative to the original singer
        self.capture = None      # list of mic blocks while recording a test in Settings
        self.test_clip = None
        self.offset_ms = tk.IntVar(value=0)
        self.inputs = [(i, d["name"]) for i, d in enumerate(sd.query_devices())
                       if d["max_input_channels"] > 0 and d["hostapi"] == sd.default.hostapi]
        ids = [i for i, _ in self.inputs]
        self.mic = sd.default.device[0] if sd.default.device[0] in ids else (ids[0] if ids else None)
        # multiplayer
        self.view = None              # "joining", "lobby", "playing" or "results" while in a room
        self.net = None               # net.Client
        self.room = None              # latest room state from the server
        self.remote = {}              # player id -> (name, char, voice track)
        self.server_port = None       # set while this app runs a server
        self.player_name = tk.StringVar(value=os.environ.get("USERNAME") or os.environ.get("USER") or "Player")
        self.address = tk.StringVar()
        self.code = tk.StringVar()
        self.port = tk.StringVar(value=str(net.PORT))
        self.connected_to = ""
        self.load_settings()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.pump()
        self.main_menu()

    def reload_songs(self):
        """Read every map from disk again (after the editor saved or made one)."""
        key = self.song.key if self.song else None
        self.songs = [Song(d) for d in sorted(glob.glob(os.path.join(MAPS, "*")))
                      if glob.glob(os.path.join(d, "*.map.json"))]
        self.song = next((s for s in self.songs if s.key == key), self.songs[0] if self.songs else None)
        self.me = self.song.characters[0]["id"] if self.song and self.song.characters else None

    # ---------- screen plumbing ----------
    def screen(self, crumb, back=None):
        self.stop_audio()
        self.capture = None
        self.view = None
        for seq in EDITOR_EVENTS:
            self.c.unbind(seq)
        for key in ("<space>", "<Delete>", "<BackSpace>"):
            self.root.unbind(key)
        self.gen += 1
        self.c.delete("all")
        for w in self.widgets:
            w.destroy()
        self.widgets, self.images = [], []
        self.c.create_image(0, 0, anchor="nw", image=theme.background())
        theme.breadcrumb(self.c, crumb)
        if back:
            Button(self.c, 8, H - 46, 132, 40, "↩  Back", back, style="pill", size=15)

    def loop(self, fn, ms=33):
        gen = self.gen

        def run():
            if self.gen != gen:
                return
            fn()
            if self.gen == gen:
                self.root.after(ms, run)
        self.root.after(ms, run)

    def widget(self, w, x, y, **kw):
        self.widgets.append(w)
        self.c.create_window(x, y, window=w, **kw)
        return w

    def photo(self, img):
        p = ImageTk.PhotoImage(img)
        self.images.append(p)
        return p

    def sprite_photo(self, cid, height, song=None):
        img = (song or self.song).sprite(cid)
        return self.photo(img.resize((max(1, round(img.width * height / img.height)), height), Image.LANCZOS))

    def panel(self, x, y, w, h, kind="dark"):
        self.c.create_image(x, y, anchor="nw", image=theme.panel_image(w, h, kind))

    def text(self, x, y, text, size=14, color="white", bold=False, **kw):
        return self.c.create_text(x, y, text=text, fill=color, font=(FONT, size, "bold" if bold else ""), **kw)

    def stop_audio(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        sd.stop()

    def quit(self):
        self.stop_audio()
        self.leave_room()
        self.root.destroy()

    # ---------- main menu ----------
    def main_menu(self):
        self.screen("Main Menu")
        for i, (label, cmd) in enumerate([("Play", self.song_select), ("Host", self.host_screen),
                                          ("Create", self.create_screen), ("Settings", self.settings_screen),
                                          ("Quit", self.quit)]):
            Button(self.c, 18, 82 + i * 107, 290, 99, label, cmd, size=24)
        self.panel(326, 82, 808, 534)
        self.text(730, 150, "Song Voicer", 42, bold=True)
        self.text(730, 205, "Still in early Beta. Inspired by Choicer Voicer :)", 15, "#bfe6ee")
        self.menu_wave(380, 240, 700, 330)
        count = len(self.songs)
        self.text(W - 20, H - 26, f"{count} song{'s' * (count != 1)} installed", 12, "#ddd", anchor="e")

    def menu_wave(self, x, y, w, h, bar_w=3.7):
        """The sketched two-blip waveform, drawn as bars like the in-game waveform."""
        xs, tops, bottoms = (np.array(col, dtype=float) for col in zip(*theme.MENU_WAVE))
        mid, scale = y + h / 2, (h / 2) / 185
        for k in range(int(w / bar_w)):
            sx = (k + 0.5) * bar_w / w * 570
            up, down = np.interp(sx, xs, tops) * scale, np.interp(sx, xs, bottoms) * scale
            bx = x + k * bar_w
            self.c.create_rectangle(bx, mid - max(1, up), bx + bar_w - 1, mid + max(1, down), fill="#ffffff", width=0)

    # ---------- settings ----------
    def settings_screen(self):
        self.screen("Main Menu > Settings", back=lambda: (self.save_settings(), self.main_menu()))
        self.panel(18, 82, 1116, 534)
        self.text(52, 124, "Microphone", 26, bold=True, anchor="w")
        names = [name for _, name in self.inputs]
        if not names:
            self.text(52, 180, "No microphone found. Plug one in and reopen Settings.", 15, "#ffb35c", anchor="w")
            return
        mic_var = tk.StringVar(value=next((nm for i, nm in self.inputs if i == self.mic), names[0]))
        box = self.widget(ttk.Combobox(self.c, textvariable=mic_var, values=names, state="readonly", width=70),
                          52, 176, anchor="w")
        box.bind("<<ComboboxSelected>>", lambda e: self.set_mic(names.index(mic_var.get())))

        # level meter, in dB so quiet voices still move it
        self.text(52, 226, "Input level", 13, "#ccc", anchor="w")
        x0, y0, x1, y1 = 52, 244, 1100, 278
        self.c.create_rectangle(x0, y0, x1, y1, fill="#1b1b1b", outline="#666")
        for db, label in ((-40, "-40 dB"), (-20, "-20 dB"), (-6, "-6"), (0, "0")):
            x = x0 + (x1 - x0) * (db + METER_DB) / METER_DB
            self.c.create_line(x, y1, x, y1 + 6, fill="#888")
            self.text(x, y1 + 16, label, 9, "#888")
        self.meter_box = (x0 + 1, y0 + 1, x1 - 1, y1 - 1)
        self.meter = self.c.create_rectangle(x0 + 1, y0 + 1, x0 + 1, y1 - 1, fill="#3fbf6f", width=0)
        self.peak_line = self.c.create_line(x0, y0 + 1, x0, y1 - 1, fill="white", width=2)
        self.level_status = self.text(52, 318, "", 13, "#bbb", anchor="w")
        self.peak_frac, self.peak_time, self.level_hist = 0.0, 0.0, []

        self.text(52, 372, "Voice volume in the cover", 13, "#ccc", anchor="w")
        value = self.text(640, 372, f"{round(self.mic_gain.get() * 100)}%", 14, bold=True, anchor="w")
        scale = self.widget(ttk.Scale(self.c, from_=0.5, to=3.0, variable=self.mic_gain, length=360,
                                      command=lambda v: self.c.itemconfig(value, text=f"{round(float(v) * 100)}%")),
                            262, 372, anchor="w")
        scale.bind("<ButtonRelease-1>", lambda e: self.save_settings())
        self.text(52, 400, "Your singing is first matched to the original singer's loudness, then set to this "
                           "volume. 100% = as loud as the original.", 11, "#999", anchor="w")

        self.text(52, 452, "Test", 13, "#ccc", anchor="w")
        self.rec_btn = Button(self.c, 52, 470, 220, 54, "●  Record 3 s", self.record_test, style="small", size=15)
        self.play_test_btn = Button(self.c, 284, 470, 200, 54, "▶  Play test", self.play_test, style="small",
                                    size=15)
        self.play_test_btn.set_enabled(self.test_clip is not None)
        self.test_status = self.text(504, 497, "Record yourself singing, then play it back at the volume above.",
                                     12, "#bbb", anchor="w")
        self.text(52, 560, "Use headphones, like during a song, or the playback will feed back into the mic.",
                  11, "#999", anchor="w")
        self.start_meter()
        self.loop(self.settings_tick)

    def settings_tick(self):
        now = time.time()
        x0, y0, x1, y1 = self.meter_box
        level = min(1.0, self.level)
        frac = max(0.0, (20 * np.log10(max(level, 1e-6)) + METER_DB) / METER_DB)
        color = "#e04848" if level >= 0.98 else ("#ffd34d" if level >= 0.5 else "#3fbf6f")
        self.c.coords(self.meter, x0, y0, x0 + (x1 - x0) * frac, y1)
        self.c.itemconfig(self.meter, fill=color)
        if frac >= self.peak_frac or now - self.peak_time > 1.5:
            self.peak_frac, self.peak_time = frac, now
        px = x0 + (x1 - x0) * self.peak_frac
        self.c.coords(self.peak_line, px, y0, px, y1)
        self.level_hist = [(t, v) for t, v in self.level_hist if now - t < 3] + [(now, level)]
        loudest = max(v for _, v in self.level_hist)
        self.level *= 0.85

        if self.capture is not None:
            left = self.rec_end - now
            if left > 0:
                self.c.itemconfig(self.test_status, text=f"Recording… {left:.1f} s", fill="#ff6b6b")
            else:
                data = np.concatenate(self.capture) if self.capture else np.zeros(1, np.float32)
                self.capture = None
                self.test_clip = (data, int(self.stream.samplerate) if self.stream else 44100)
                self.rec_btn.set_enabled(True)
                self.play_test_btn.set_enabled(True)
                self.c.itemconfig(self.test_status, text="Recorded. Press Play test to hear it.", fill="#7fe08f")

        if loudest >= 0.98:
            msg, fill = "Too loud: it's clipping. Move back a little or lower the mic's input level.", "#ff6b6b"
        elif loudest < 0.02:
            msg, fill = "Too quiet: sing or speak, or pick another microphone above.", "#ffb35c"
        else:
            msg, fill = "Sounds good.", "#7fe08f"
        self.c.itemconfig(self.level_status, text=msg, fill=fill)

    def record_test(self):
        if not self.stream:
            return
        sd.stop()
        self.capture = []
        self.rec_end = time.time() + 3
        self.rec_btn.set_enabled(False)
        self.play_test_btn.set_enabled(False)

    def play_test(self):
        data, rate = self.test_clip
        # Same treatment as in a cover: bring it to a typical singer level, then apply the volume setting.
        rms = float(np.sqrt(np.mean(data ** 2)))
        out = data * (0.1 / rms if rms > 1e-5 else 1.0) * self.mic_gain.get()
        sd.play(np.clip(out, -1, 1), rate)

    # ---------- multiplayer: host / join ----------
    def entry(self, var, x, y, width=28):
        return self.widget(tk.Entry(self.c, textvariable=var, font=(FONT, 13), width=width), x, y, anchor="w")

    def host_screen(self):
        self.leave_room()
        self.screen("Main Menu > Host", back=self.main_menu)
        playable = [s for s in self.songs if s.playable()]

        self.panel(18, 82, 549, 534)
        self.text(48, 122, "Host a room", 26, bold=True, anchor="w")
        self.text(48, 170, "Your name", 13, "#ccc", anchor="w")
        self.entry(self.player_name, 170, 170)
        self.text(48, 214, "Song", 13, "#ccc", anchor="w")
        song_var = tk.StringVar(value=playable[0].title if playable else "")
        self.widget(ttk.Combobox(self.c, textvariable=song_var, values=[s.title for s in playable],
                                 state="readonly", width=30), 170, 214, anchor="w")
        self.text(48, 258, "Server", 13, "#ccc", anchor="w")
        self.entry(self.address, 170, 258)
        self.text(48, 302, "Port", 13, "#ccc", anchor="w")
        self.entry(self.port, 170, 302, width=8)
        self.text(260, 302, "only used without a server", 11, "#888", anchor="w")
        Button(self.c, 48, 334, 300, 60, "Start room", lambda: self.host_room(song_var.get()), style="small", size=17)
        self.text(48, 414, "Server: your Netlify site (like songvoicer.netlify.app). Everyone uses the same "
                           "address and nobody has to open ports.\n\n"
                           f"Leave it empty to host on this PC instead: friends on your Wi-Fi join with "
                           f"{net.local_ip()}:{self.port.get()}.", 12, "#bbb", anchor="nw", width=480)

        self.panel(585, 82, 549, 534)
        self.text(615, 122, "Join a room", 26, bold=True, anchor="w")
        self.text(615, 170, "Your name", 13, "#ccc", anchor="w")
        self.entry(self.player_name, 760, 170)
        self.text(615, 214, "Server address", 13, "#ccc", anchor="w")
        self.entry(self.address, 760, 214)
        self.text(615, 258, "Room code", 13, "#ccc", anchor="w")
        self.entry(self.code, 760, 258, width=8)
        Button(self.c, 615, 334, 300, 60, "Join", self.join_room, style="small", size=17)
        self.text(615, 414, "Ask the host for the server address (their Netlify site, or an address like "
                            "192.168.1.5:7777) and the 4-letter room code. You need the same song in your maps "
                            "folder.", 12, "#bbb", anchor="nw", width=480)

    def connect(self, address):
        self.leave_room()
        songs = [s.key for s in self.songs if s.playable()]
        self.root.config(cursor="watch")
        self.root.update()
        try:
            self.net = net.make_client(address, self.player_name.get().strip() or "Player", songs)
        except (OSError, ValueError) as e:
            messagebox.showerror("Song Voicer", f"Couldn't connect to {address}:\n{e}")
            return False
        finally:
            self.root.config(cursor="")
        self.connected_to = address.strip()
        self.save_settings()
        self.view = "joining"
        return True

    def load_settings(self):
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        self.player_name.set(saved.get("name") or self.player_name.get())
        self.address.set(saved.get("server", ""))
        self.mic_gain.set(saved.get("mic_gain", self.mic_gain.get()))
        # Device numbers change when devices are plugged in or out, so the mic is remembered by name.
        self.mic = next((i for i, name in self.inputs if name == saved.get("mic")), self.mic)

    def save_settings(self):
        mic = next((name for i, name in self.inputs if i == self.mic), None)
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump({"name": self.player_name.get().strip(), "server": self.address.get().strip(),
                           "mic": mic, "mic_gain": round(self.mic_gain.get(), 2)}, f)
        except OSError:
            pass

    def host_room(self, title):
        song = next((s for s in self.songs if s.title == title), None)
        if not song:
            return messagebox.showinfo("Song Voicer", "Pick a song first.")
        if self.address.get().strip():  # rooms live on the shared server (e.g. the Netlify site)
            if self.connect(self.address.get()):
                self.net.send({"type": "create", "song": song.key})
            return
        try:
            port = int(self.port.get())
        except ValueError:
            return messagebox.showerror("Song Voicer", "The port has to be a number, like 7777.")
        if self.server_port != port:
            try:
                net.start_in_background(port)
            except OSError as e:
                return messagebox.showerror("Song Voicer", f"Couldn't start a server on port {port}:\n{e}")
            self.server_port = port
        if self.connect(f"127.0.0.1:{port}"):
            self.net.send({"type": "create", "song": song.key})

    def join_room(self):
        if not self.address.get().strip() or not self.code.get().strip():
            return messagebox.showinfo("Song Voicer", "Enter the host's server address and the room code.")
        if self.connect(self.address.get()):
            self.net.send({"type": "join", "code": self.code.get()})

    def leave_room(self):
        if self.net:
            self.net.close()
        self.net, self.room, self.remote = None, None, {}

    def pump(self):
        """Handle messages from the server on the Tk thread."""
        while self.net:
            try:
                head, data = self.net.inbox.get_nowait()
            except queue.Empty:
                break
            self.on_net(head, data)
        self.root.after(50, self.pump)

    def my_player(self):
        return next((p for p in (self.room or {}).get("players", []) if p["id"] == self.net.id), None)

    def on_net(self, head, data):
        kind = head["type"]
        if kind == "room":
            self.room = head
            if self.view in ("joining", "lobby"):
                self.lobby()
            elif self.view == "results":
                self.show_waiting()
        elif kind == "error":
            messagebox.showwarning("Song Voicer", head["msg"])
            if self.view == "joining" and not self.room:
                self.leave_room()
        elif kind == "start":
            me = self.my_player()
            if not me or not me["char"]:
                return
            self.me, self.remote = me["char"], {}
            self.song = self.song_by_key(self.room["song"])
            self.screen(f"{self.song.title} > get ready")
            self.text(W / 2, H / 2, "Get ready…", 40, TEXT_DARK, bold=True)
            if not self.song.sr:
                self.root.update()
                self.song.load_audio()
            delay = max(0.0, head["delay"] - self.net.rtt / 2)
            self.root.after(int(delay * 1000), self.play)
        elif kind == "voice" and self.song and self.song.sr:
            voice = decode_voice(head.get("segments", []), data, len(self.song.inst))
            self.remote[head["from"]] = (head["name"], head["char"], voice)
            if self.view == "results":
                self.build_cover()
                self.show_waiting()
        elif kind == "disconnected":
            self.leave_room()
            messagebox.showwarning("Song Voicer", "Lost the connection to the server.")
            self.main_menu()

    def song_by_key(self, key):
        return next(s for s in self.songs if s.key == key)

    def lobby(self):
        r = self.room
        s = self.song = self.song_by_key(r["song"])
        if not s.sr:
            self.screen(f"Host > Room {r['code']}")
            self.text(W / 2, H / 2, "Loading song…", 20, TEXT_DARK)
            self.root.update()
            s.load_audio()
        self.screen(f"Host > Room {r['code']}", back=lambda: (self.leave_room(), self.host_screen()))
        self.view = "lobby"
        me = self.my_player()
        is_host = r["host"] == self.net.id

        self.panel(18, 82, 360, 200)
        self.text(198, 112, "ROOM CODE", 12, "#aaa")
        self.text(198, 165, r["code"], 52, bold=True)
        self.text(198, 226, s.title, 15, "#bfe6ee", bold=True)
        self.text(198, 250, f"{len(r['players'])} player{'s' * (len(r['players']) != 1)}", 12, "#aaa")
        if net.is_web_address(self.connected_to):
            self.text(198, 270, self.connected_to, 10, "#888")

        self.panel(18, 294, 360, 322)
        self.text(44, 322, "Players", 15, bold=True, anchor="w")
        for i, p in enumerate(r["players"]):
            y = 358 + i * 32
            name = p["name"] + ("  ★" if p["id"] == r["host"] else "") + ("  (you)" if p["id"] == self.net.id else "")
            self.text(44, y, name, 13, "white", anchor="w")
            char = s.chars.get(p["char"])
            self.text(352, y, char["name"] if char else "picking…", 13, char["color"] if char else "#888",
                      bold=bool(char), anchor="e")

        taken = {p["char"]: p["name"] for p in r["players"] if p["char"] and p["id"] != self.net.id}
        n = len(s.characters)
        cw = (738 - (n - 1) * 10) // n
        for i, c in enumerate(s.characters):
            x, tag = 396 + i * (cw + 10), f"lcard{i}"
            self.c.create_image(x, 82, anchor="nw", image=theme.panel_image(cw, 300, "card"), tags=tag)
            img = s.sprite(c["id"])
            height = int(min(200, (cw - 20) * img.height / img.width))
            self.c.create_image(x + cw / 2, 290, anchor="s", image=self.sprite_photo(c["id"], height), tags=tag)
            self.text(x + cw / 2, 316, c["name"], 15, c["color"], bold=True, tags=tag, width=cw - 10, justify="center")
            if c["id"] in taken:
                shade = self.photo(Image.new("RGBA", (cw - 8, 292), (0, 0, 0, 150)))
                self.c.create_image(x + 4, 86, anchor="nw", image=shade)
                self.text(x + cw / 2, 352, f"taken by {taken[c['id']]}", 11, "#ffb35c", width=cw - 10, justify="center")
            else:
                self.text(x + cw / 2, 352, f"{s.line_count(c['id'])} lines", 11, "#777", tags=tag)
                self.c.tag_bind(tag, "<Button-1>", lambda e, cid=c["id"]: self.net.send({"type": "pick", "char": cid}))
            if me and me["char"] == c["id"]:
                theme.brackets(self.c, x - 3, 79, x + cw + 3, 385, "sel", size=20, width=4)

        self.panel(396, 400, 738, 216)
        self.text(422, 432, "Microphone", 13, "#ccc", anchor="w")
        names = [name for _, name in self.inputs]
        mic_var = tk.StringVar(value=next((nm for i, nm in self.inputs if i == self.mic), ""))
        box = self.widget(ttk.Combobox(self.c, textvariable=mic_var, values=names, state="readonly", width=40),
                          530, 432, anchor="w")
        box.bind("<<ComboboxSelected>>", lambda e: self.set_mic(names.index(mic_var.get())))
        self.c.create_rectangle(422, 456, 1108, 470, fill="#1b1b1b", outline="#666")
        self.meter_box = (423, 457, 1107, 469)
        self.meter = self.c.create_rectangle(423, 457, 423, 469, fill="#3fbf6f", width=0)
        self.text(422, 500, "Guide vocals", 13, "#ccc", anchor="w")
        self.widget(ttk.Scale(self.c, from_=0, to=1, variable=self.guide_vol, length=220), 530, 500, anchor="w")
        self.text(422, 540, "Everyone sings on their own PC while you hear each other in Discord.\n"
                            "Use headphones! After the song, the full cover plays for everyone.",
                  12, "#ffd76a", anchor="nw")

        if is_host:
            ready = r["state"] == "lobby" and all(p["char"] for p in r["players"])
            start = Button(self.c, W - 200, H - 46, 190, 40, "Start  ▶", lambda: self.net.send({"type": "start"}),
                           style="pill", size=15)
            start.set_enabled(ready)
            if not ready:
                self.text(W - 215, H - 26, "Everyone needs a character" if r["state"] == "lobby"
                          else "Waiting for everyone's recordings…", 12, "#ddd", anchor="e")
        else:
            self.text(W - 20, H - 26, "Waiting for the host to start…", 13, "#ddd", anchor="e")
        self.start_meter()
        self.loop(self.meter_tick)

    # ---------- create: see editor.py ----------
    def open_maps(self):
        if sys.platform == "win32":
            os.startfile(MAPS)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", MAPS])

    # ---------- song select ----------
    def song_select(self):
        self.screen("Main Menu > Play", back=self.main_menu)
        self.panel(18, 82, 440, 534, "light")
        self.text(46, 114, "Songs", 14, TEXT_DARK, bold=True, anchor="w")
        self.c.create_line(40, 134, 436, 134, fill="#e2e2e2")
        for i, s in enumerate(self.songs):
            y = 140 + i * 48
            tag = f"song{i}"
            if s is self.song:
                self.c.create_rectangle(30, y, 446, y + 44, fill="#c5e6ee", outline="", tags=tag)
            self.text(46, y + 22, s.title, 15, TEXT_DARK, anchor="w", tags=tag)
            self.c.tag_bind(tag, "<Button-1>", lambda e, s=s: self.pick_song(s))
        if not self.song:
            self.text(46, 170, "No maps found in maps/", 13, "#888", anchor="w")
            return

        s = self.song
        self.panel(476, 82, 658, 534)
        self.text(506, 120, s.title, 26, bold=True, anchor="w")
        self.text(506, 156, s.artist, 13, "#bbb", anchor="w")
        self.text(506, 182, f"{len(s.lines)} lines  ·  {len(s.characters)} characters", 13, "#bbb", anchor="w")
        for i, c in enumerate(s.characters):
            x = 506 + i * 122
            self.c.create_image(x + 55, 370, anchor="s", image=self.sprite_photo(c["id"], 150))
            self.text(x + 55, 388, c["name"], 14, c["color"], bold=True)
            self.text(x + 55, 410, f"{s.line_count(c['id'])} lines", 11, "#aaa")
        self.lyrics_box(506, 450, 600, self.song_select)
        nxt = Button(self.c, W - 200, H - 46, 190, 40, "Next  ▶", self.character_select, style="pill", size=15)
        missing = s.missing_audio()
        if missing:
            self.text(506, 540, f"Missing audio in maps/{os.path.basename(s.folder)}/:  {',  '.join(missing)}",
                      12, "#ff6b6b", anchor="w")
        elif not s.playable():
            self.text(506, 540, "This map has no characters or lines yet. Finish it in Create.", 12, "#ff6b6b",
                      anchor="w")
        nxt.set_enabled(s.playable())

    def pick_song(self, song):
        self.song = song
        self.me = song.characters[0]["id"] if song.characters else None
        self.song_select()

    def lyrics_box(self, x, y, width, redraw):
        status = self.song.lyrics_status
        if status == "ok":
            self.text(x, y + 20, "✓ Lyrics loaded", 14, "#7fe08f", anchor="w")
        else:
            folder = os.path.basename(self.song.folder)
            msg = (f"No lyrics yet. Put a .txt with the lyrics in maps/{folder}/, or copy them "
                   "(with [CHARACTER] headers) and press Paste lyrics." if status == "missing" else status)
            self.text(x, y + 6, msg, 12, "#ffb35c", anchor="nw", width=width - 200)
        Button(self.c, x + width - 180, y, 180, 44, "Paste lyrics", lambda: self.paste_lyrics(redraw),
               style="small", size=14)

    def paste_lyrics(self, redraw):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            text = ""
        if "[" not in text:
            messagebox.showinfo("Paste lyrics", "Copy the lyrics first, including the [CHARACTER] headers.")
            return
        with open(self.song.lyrics_path, "w", encoding="utf-8") as f:
            f.write(text)
        self.song.lyrics_status = self.song.load_lyrics()
        redraw()

    # ---------- character select ----------
    def character_select(self):
        s = self.song
        self.screen(f"Play > {s.title}", back=self.song_select)
        if not s.sr:
            self.text(W / 2, H / 2, "Loading song…", 20, TEXT_DARK)
            self.root.update()
            s.load_audio()
            self.screen(f"Play > {s.title}", back=self.song_select)

        n = len(s.characters)
        cw = (1116 - (n - 1) * 12) // n
        self.cards = {}
        for i, c in enumerate(s.characters):
            x, tag = 18 + i * (cw + 12), f"card{i}"
            self.c.create_image(x, 82, anchor="nw", image=theme.panel_image(cw, 320, "card"), tags=tag)
            self.c.create_image(x + cw / 2, 330, anchor="s", image=self.sprite_photo(c["id"], 230), tags=tag)
            self.text(x + cw / 2, 352, c["name"], 18, c["color"], bold=True, tags=tag)
            self.text(x + cw / 2, 378, f"{s.line_count(c['id'])} lines", 12, "#777", tags=tag)
            self.c.tag_bind(tag, "<Button-1>", lambda e, cid=c["id"]: self.pick_character(cid))
            self.cards[c["id"]] = (x, 82, x + cw, 402)
        self.pick_character(self.me)

        self.panel(18, 416, 1116, 200)
        self.text(44, 446, "Microphone", 13, "#ccc", anchor="w")
        names = [name for _, name in self.inputs]
        mic_var = tk.StringVar(value=next((nm for i, nm in self.inputs if i == self.mic), ""))
        box = self.widget(ttk.Combobox(self.c, textvariable=mic_var, values=names, state="readonly", width=44),
                          150, 446, anchor="w")
        box.bind("<<ComboboxSelected>>", lambda e: self.set_mic(names.index(mic_var.get())))
        self.c.create_rectangle(470, 438, 900, 454, fill="#1b1b1b", outline="#666")
        self.meter_box = (471, 439, 899, 453)
        self.meter = self.c.create_rectangle(471, 439, 471, 453, fill="#3fbf6f", width=0)

        self.text(44, 490, "Guide vocals", 13, "#ccc", anchor="w")
        self.widget(ttk.Scale(self.c, from_=0, to=1, variable=self.guide_vol, length=220), 150, 490, anchor="w")
        self.text(390, 490, "Your character is muted while you sing.  Use headphones!", 12, "#ffd76a", anchor="w")
        self.lyrics_box(44, 530, 1060, self.character_select)

        Button(self.c, W - 200, H - 46, 190, 40, "Start  ▶", self.play, style="pill", size=15)
        self.start_meter()
        self.loop(self.meter_tick)

    def pick_character(self, cid):
        self.me = cid
        self.c.delete("sel")
        x0, y0, x1, y1 = self.cards[cid]
        theme.brackets(self.c, x0 - 4, y0 - 4, x1 + 4, y1 + 4, "sel", size=22, width=4)

    def set_mic(self, idx):
        self.mic = self.inputs[idx][0]
        self.save_settings()
        self.start_meter()

    def start_meter(self):
        """Listen to the mic for the level meter; while self.capture is a list, also keep what it hears."""
        self.stop_audio()
        if self.mic is None:
            return

        def cb(indata, frames, time, status):
            self.level = float(np.abs(indata).max())
            if self.capture is not None:
                self.capture.append(indata[:, 0].copy())

        rate = self.song.sr if self.song and self.song.sr else int(sd.query_devices(self.mic)["default_samplerate"])
        try:
            self.stream = sd.InputStream(device=self.mic, channels=1, samplerate=rate, callback=cb)
            self.stream.start()
        except sd.PortAudioError as e:
            self.stream = None
            messagebox.showwarning("Song Voicer", f"Couldn't open that microphone:\n{e}")

    def meter_tick(self):
        x0, y0, x1, y1 = self.meter_box
        self.c.coords(self.meter, x0, y0, x0 + (x1 - x0) * min(1.0, self.level * 1.5), y1)
        self.level *= 0.85

    # ---------- game ----------
    def play(self):
        s, me = self.song, self.me
        self.screen(f"{s.title} > singing as {s.chars[me]['name']}")
        self.view = "playing" if self.net else None
        Button(self.c, 8, H - 46, 132, 40, "■  Stop", self.stop_play, style="pill", size=15)
        self.orig_gain, self.mic_win = s.masks(me)

        # stage: every character fades in while singing
        n = len(s.characters)
        slot = 1116 / n
        self.stage = []
        for i, c in enumerate(s.characters):
            cx = 18 + slot * (i + 0.5)
            frames = fade_frames(s.sprite(c["id"]), SPRITE_H)
            self.images.extend(frames)
            item = self.c.create_image(cx, 318, anchor="s", image=frames[0])
            label = "YOU" if c["id"] == me else c["name"]
            self.text(cx, 330, label, 11, c["color"] if c["id"] == me else "#8a8a8a", bold=c["id"] == me)
            self.stage.append([c["id"], item, frames, 0.0])

        # lyrics
        self.panel(18, 342, 1116, 104)
        self.sing_bg = self.c.create_rectangle(26, 350, 1126, 438, fill="#4a4318", width=0, state="hidden")
        self.lyric_name = self.text(44, 372, "", 13, bold=True, anchor="w")
        self.lyric_big = self.text(44, 404, "", 22, bold=True, anchor="w", width=930)
        self.sing_badge = self.text(1110, 372, "SING!", 16, "#ffd34d", bold=True, anchor="e", state="hidden")
        self.lyric_next = self.text(1110, 428, "", 12, "#9a9a9a", anchor="e")

        # waveform
        self.panel(18, 454, 1116, 162)
        self.wx0, self.wx1, self.wy0, self.wy1 = 30, 1122, 466, 604
        self.n_bars = int(WAVE_SECONDS * ENV_RATE)
        bw = (self.wx1 - self.wx0) / self.n_bars
        mid = (self.wy0 + self.wy1) / 2
        self.bars = [self.c.create_rectangle(self.wx0 + k * bw, mid, self.wx0 + (k + 1) * bw - 1, mid,
                                             width=0, fill="#5a5a5a") for k in range(self.n_bars)]
        self.bar_fill = ["#5a5a5a"] * self.n_bars
        third = bw / 3
        self.mic_bars = [self.c.create_rectangle(self.wx0 + k * bw + third, mid, self.wx0 + k * bw + 2 * third, mid,
                                                 width=0, fill="white") for k in range(self.n_bars // 4)]
        px = self.wx0 + 0.25 * (self.wx1 - self.wx0)
        self.c.create_line(px, self.wy0, px, self.wy1, fill="white", width=2)

        self.time_text = self.text(W - 20, H - 26, "", 13, "#eee", anchor="e")
        self.c.create_rectangle(W - 560, H - 32, W - 130, H - 20, outline="#ddd", fill="#333")
        self.progress = self.c.create_rectangle(W - 559, H - 31, W - 559, H - 21, fill=CYAN, width=0)

        self.start_song()
        self.loop(self.game_tick)

    def start_song(self):
        s = self.song
        self.pos = 0
        self.finished = False
        self.rec = np.zeros(len(s.inst), dtype="float32")
        guide, gain, inst, voc = self.guide_vol.get(), self.orig_gain, s.inst, s.voc

        def cb(indata, outdata, frames, time, status):
            p = self.pos
            end = min(p + frames, len(inst))
            n = end - p
            outdata[:n] = inst[p:end] + voc[p:end] * (gain[p:end, None] * guide)
            outdata[n:] = 0
            self.rec[p:end] = indata[:n, 0]
            self.level = float(np.abs(indata).max())
            self.pos = end
            if end >= len(inst):
                raise sd.CallbackStop

        self.stream = sd.Stream(device=(self.mic, None), channels=(1, 2), samplerate=s.sr, callback=cb,
                                finished_callback=lambda: setattr(self, "finished", True))
        # What you sing reaches the recording this much later than the song sample you heard.
        self.latency = sum(self.stream.latency)
        self.stream.start()

    def stop_play(self):
        if self.net or self.pos / self.song.sr > 2:  # in a room, always send what you have
            self.results()
        else:
            self.character_select()

    def game_tick(self):
        if self.finished:
            self.results()
            return
        s, me = self.song, self.me
        now = self.pos / s.sr
        lines = s.lines

        # sprites
        singing = set()
        for line in lines:
            if line["t"] - 0.15 <= now <= line["end"] + 0.15:
                singing.update(singers(line))
        for entry in self.stage:
            cid, item, frames, a = entry
            a = min(1.0, a + 0.15) if cid in singing else max(0.0, a - 0.08)
            entry[3] = a
            self.c.itemconfig(item, image=frames[round(a * 10)])

        # lyrics
        cur = next((i for i, l in enumerate(lines) if l["t"] - 0.15 > now), len(lines)) - 1
        active = cur >= 0 and now <= lines[cur]["end"] + 0.1
        if active:
            line = lines[cur]
            who = singers(line)
            name = s.chars[who[0]]["name"] if len(who) == 1 else "Everyone"
            color = s.chars[who[0]]["color"] if len(who) == 1 else "#f0f0f0"
            mine = me in who
            self.c.itemconfig(self.lyric_name, text=name + ("  (you)" if mine else ""), fill=color)
            self.c.itemconfig(self.lyric_big, text=line["text"], fill="white" if mine else color)
            state = "normal" if mine else "hidden"
        else:
            self.c.itemconfig(self.lyric_name, text="")
            self.c.itemconfig(self.lyric_big, text="")
            state = "hidden"
        self.c.itemconfig(self.sing_bg, state=state)
        self.c.itemconfig(self.sing_badge, state=state)
        nxt = lines[cur + 1] if cur + 1 < len(lines) else None
        if nxt:
            who = singers(nxt)
            name = s.chars[who[0]]["name"] if len(who) == 1 else "Everyone"
            wait = nxt["t"] - now
            if me in who and wait < 6:
                self.c.itemconfig(self.lyric_next, text=f"Your line in {wait:.1f}s:  {nxt['text']}", fill="#ffd34d")
            else:
                self.c.itemconfig(self.lyric_next, text=f"Next  {name}:  {nxt['text']}", fill="#9a9a9a")
        else:
            self.c.itemconfig(self.lyric_next, text="")

        self.draw_wave(now)
        total = s.length
        self.c.itemconfig(self.time_text, text=f"{int(now // 60)}:{int(now % 60):02d} / "
                                               f"{int(total // 60)}:{int(total % 60):02d}")
        self.c.coords(self.progress, W - 559, H - 31, W - 559 + 428 * now / total, H - 21)

    def draw_wave(self, now):
        s = self.song
        c = self.c
        first = int((now - 0.25 * WAVE_SECONDS) * ENV_RATE)
        bw = (self.wx1 - self.wx0) / self.n_bars
        mid, half = (self.wy0 + self.wy1) / 2, (self.wy1 - self.wy0) / 2 - 4

        # your lines get a highlighted band, every line gets its singer's name
        c.delete("band")
        t0, t1 = now - 0.25 * WAVE_SECONDS, now + 0.75 * WAVE_SECONDS
        x_of = lambda t: self.wx0 + (t - t0) / WAVE_SECONDS * (self.wx1 - self.wx0)
        for line in s.lines:
            if line["end"] < t0 or line["t"] > t1:
                continue
            who = singers(line)
            xa, xb = max(self.wx0, x_of(line["t"])), min(self.wx1, x_of(line["end"]))
            if self.me in who:
                c.create_rectangle(xa, self.wy0, xb, self.wy1, fill="#4a4318", width=0, tags="band")
            if x_of(line["t"]) >= self.wx0:
                name = s.chars[who[0]]["name"] if len(who) == 1 else "Everyone"
                color = s.chars[who[0]]["color"] if len(who) == 1 else "#e8e8e8"
                c.create_text(xa + 3, self.wy0 + 2, text=name, anchor="nw", fill=color, font=(FONT, 9, "bold"),
                              tags="band")
        c.tag_lower("band", self.bars[0])

        idx = np.arange(first, first + self.n_bars)
        ok = (idx >= 0) & (idx < len(s.env))
        heights = np.where(ok, s.env[np.clip(idx, 0, len(s.env) - 1)], 0) * half
        for k, bar in enumerate(self.bars):
            x = self.wx0 + k * bw
            h = max(1.0, min(half, heights[k]))
            c.coords(bar, x, mid - h, x + bw - 1, mid + h)
            fill = s.bin_colors[idx[k]] if ok[k] else "#5a5a5a"
            if fill != self.bar_fill[k]:
                c.itemconfig(bar, fill=fill)
                self.bar_fill[k] = fill

        # your microphone, drawn up to the playhead
        hop = s.sr // ENV_RATE
        for k, bar in enumerate(self.mic_bars):
            i = first + k
            h = 0.0
            if i >= 0 and (i + 1) * hop <= self.pos:
                h = min(half, float(np.abs(self.rec[i * hop:(i + 1) * hop]).max()) * 2.5 * half)
            x = self.wx0 + k * bw + bw / 3
            c.coords(bar, x, mid - h, x + bw / 3, mid + h)

    # ---------- results ----------
    def my_voice(self):
        """Your recording, delay-corrected, cut to your lines and matched to the original singer's loudness."""
        s = self.song
        shift = int((self.latency + self.offset_ms.get() / 1000) * s.sr)
        rec = np.zeros_like(self.rec)
        if shift >= 0:
            rec[:len(rec) - shift] = self.rec[shift:]
        else:
            rec[-shift:] = self.rec[:shift]
        voice = rec * self.mic_win
        keep = self.mic_win > 0.5
        mine = np.sqrt(np.mean(voice[keep] ** 2)) if keep.any() else 0
        if mine > 1e-5:
            voice *= min(30.0, np.sqrt(np.mean(s.voc.mean(axis=1)[keep] ** 2)) / mine)
        return voice * self.mic_gain.get()

    def build_cover(self):
        """Mix the instrumental, the original vocals of anyone not sung, your voice and everyone else's."""
        s = self.song
        self.voice = self.my_voice()
        voices = [(self.me, self.voice)] + [(char, v) for _, char, v in self.remote.values()]
        gain = np.ones(len(s.inst), np.float32)
        for char, _ in voices:
            np.minimum(gain, s.masks(char)[0], out=gain)
        mix = s.inst + s.voc * gain[:, None]
        for _, v in voices:
            mix += v[:, None]
        # Only the loudest peaks get rounded off; the song never turns down because a voice is loud.
        self.cover = soft_clip(mix).astype(np.float32)

    def upload_voice(self):
        segments, data = encode_voice(self.voice, self.mic_win)
        threading.Thread(target=self.net.send, args=({"type": "upload", "segments": segments}, data),
                         daemon=True).start()

    def rebuild_cover(self):
        sd.stop()
        self.build_cover()
        if self.net:
            self.upload_voice()

    def show_waiting(self):
        if self.view != "results" or not self.room:
            return
        missing = [p["name"] for p in self.room["players"] if not p["uploaded"] and p["id"] != self.net.id]
        text = ("Waiting for: " + ", ".join(missing)) if missing else "Everyone's voices are in!"
        self.c.itemconfig(self.waiting, text=text, fill="#ffd76a" if missing else "#7fe08f")

    def results(self):
        s, me = self.song, self.me
        self.stop_audio()
        self.build_cover()
        if self.net:
            self.upload_voice()
            back = lambda: (self.leave_room(), self.main_menu())
        else:
            back = self.main_menu
        self.screen(f"{s.title} > Your cover", back=back)
        self.panel(18, 82, 400, 534, "light")
        self.c.create_image(218, 520, anchor="s", image=self.sprite_photo(me, 380))
        self.text(218, 562, s.chars[me]["name"], 22, s.chars[me]["color"], bold=True)

        self.panel(436, 82, 698, 534)
        self.text(470, 126, "Cover ready!", 30, bold=True, anchor="w")
        self.text(470, 166, f"You sang {s.line_count(me)} lines as {s.chars[me]['name']}.", 14, "#ccc", anchor="w")
        if self.net:
            self.view = "results"
            self.waiting = self.text(1100, 126, "", 13, anchor="e")
            self.show_waiting()
        Button(self.c, 470, 200, 200, 54, "▶  Play cover", lambda: sd.play(self.cover, s.sr), style="small", size=15)
        Button(self.c, 682, 200, 140, 54, "■  Stop", sd.stop, style="small", size=15)
        Button(self.c, 834, 200, 200, 54, "Save cover…", self.save_cover, style="small", size=15)

        self.text(470, 300, "Sync fix", 15, bold=True, anchor="w")
        self.text(470, 326, "If your voice sounds early or late, move this and press Play cover again.",
                  12, "#bbb", anchor="w")
        value = self.text(1100, 362, f"{self.offset_ms.get():+d} ms", 13, "#eee", anchor="e")
        scale = self.widget(ttk.Scale(self.c, from_=-300, to=300, length=520,
                                      command=lambda v: (self.offset_ms.set(round(float(v))),
                                                         self.c.itemconfig(value, text=f"{round(float(v)):+d} ms"))),
                            470, 362, anchor="w")
        scale.set(self.offset_ms.get())
        scale.bind("<ButtonRelease-1>", lambda e: self.rebuild_cover())
        self.text(470, 392, f"Measured audio delay: {self.latency * 1000:.0f} ms (already corrected)",
                  11, "#888", anchor="w")

        if self.net:
            Button(self.c, 470, 470, 270, 74, "Back to room", self.lobby, size=20)
            Button(self.c, 760, 470, 270, 74, "Leave room", back, size=20)
        else:
            Button(self.c, 470, 470, 270, 74, "Sing again", self.play, size=20)
            Button(self.c, 760, 470, 270, 74, "Main menu", self.main_menu, size=20)

    def save_cover(self):
        path = filedialog.asksaveasfilename(defaultextension=".wav", initialfile="my_cover.wav",
                                            filetypes=[("WAV audio", "*.wav")])
        if path:
            sf.write(path, self.cover, self.song.sr)


if __name__ == "__main__":
    root = tk.Tk()
    root.report_callback_exception = report_error
    App(root)
    root.mainloop()
