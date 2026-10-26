"""Song Voicer: pick a character, sing your parts, hear the cover.

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
import tkinter as tk
import traceback
import zlib
from tkinter import filedialog, messagebox, ttk


def data_dir():
    """Where maps, lyrics and error.log live.

    From source that's this folder. A packaged app (PyInstaller) can't write inside itself, so it uses
    a per-user folder and copies the bundled maps there on first run.
    """
    if not getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(__file__))
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    else:
        base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    path = os.path.join(base, "Song Voicer")
    bundled = os.path.join(sys._MEIPASS, "maps")
    if not os.path.isdir(os.path.join(path, "maps")) and os.path.isdir(bundled):
        shutil.copytree(bundled, os.path.join(path, "maps"))
    os.makedirs(os.path.join(path, "maps"), exist_ok=True)
    return path


DATA = data_dir()
ERROR_LOG = os.path.join(DATA, "error.log")


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
from fill_lyrics import parse_lyrics
from theme import FONT, H, W, Button

MAPS = os.path.join(DATA, "maps")
PAD = 0.25           # seconds of your recording kept around each of your lines
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


class App:
    def __init__(self, root):
        self.root = root
        root.title("Song Voicer")
        root.resizable(False, False)
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
        self.songs = [Song(d) for d in sorted(glob.glob(os.path.join(MAPS, "*")))
                      if glob.glob(os.path.join(d, "*.map.json"))]
        self.song = self.songs[0] if self.songs else None
        self.me = self.song.characters[0]["id"] if self.song else None
        self.guide_vol = tk.DoubleVar(value=0.8)
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
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.pump()
        self.main_menu()

    # ---------- screen plumbing ----------
    def screen(self, crumb, back=None):
        self.stop_audio()
        self.view = None
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
                                          ("Create", self.create_screen), ("Quit", self.quit)]):
            Button(self.c, 18, 82 + i * 92, 290, 86, label, cmd, size=24)
        self.panel(326, 82, 808, 534)
        self.text(730, 150, "Song Voicer", 42, bold=True)
        self.text(730, 205, "Pick a character. Sing your part. Hear the cover.", 15, "#bfe6ee")
        self.menu_wave(380, 240, 700, 330)
        count = len(self.songs)
        self.text(W - 20, H - 26, f"{count} song{'s' * (count != 1)} installed", 12, "#ddd", anchor="e")

    # The menu waveform, traced from a sketch 570 units wide: (x, height above the line, height below it).
    MENU_WAVE = [
        (0, 50, 50), (10, 58, 75), (15, 65, 90), (25, 95, 135), (30, 105, 150), (40, 130, 180), (45, 140, 182),
        (55, 145, 185), (70, 135, 178), (80, 130, 170), (85, 125, 165), (95, 112, 150), (100, 105, 85),
        (105, 98, 75), (115, 65, 72), (125, 45, 70), (130, 35, 55), (140, 22, 25), (150, 15, 20), (170, 5, 13),
        (190, 3, 5), (200, 0, 0), (232, 0, 0),
        (235, 95, 80), (245, 100, 120), (255, 103, 135), (270, 103, 150), (280, 112, 135), (285, 125, 120),
        (298, 125, 110), (320, 70, 105), (340, 48, 70), (345, 40, 62), (370, 20, 35), (395, 5, 15), (405, 0, 0),
        (570, 0, 0),
    ]

    def menu_wave(self, x, y, w, h, bar_w=3.7):
        """The sketched two-blip waveform, drawn as bars like the in-game waveform."""
        xs, tops, bottoms = (np.array(col, dtype=float) for col in zip(*self.MENU_WAVE))
        mid, scale = y + h / 2, (h / 2) / 185
        for k in range(int(w / bar_w)):
            sx = (k + 0.5) * bar_w / w * 570
            up, down = np.interp(sx, xs, tops) * scale, np.interp(sx, xs, bottoms) * scale
            bx = x + k * bar_w
            self.c.create_rectangle(bx, mid - max(1, up), bx + bar_w - 1, mid + max(1, down), fill="#ffffff", width=0)

    # ---------- multiplayer: host / join ----------
    def entry(self, var, x, y, width=28):
        return self.widget(tk.Entry(self.c, textvariable=var, font=(FONT, 13), width=width), x, y, anchor="w")

    def host_screen(self):
        self.leave_room()
        self.screen("Main Menu > Host", back=self.main_menu)
        playable = [s for s in self.songs if not s.missing_audio()]

        self.panel(18, 82, 549, 534)
        self.text(48, 122, "Host a room", 26, bold=True, anchor="w")
        self.text(48, 176, "Your name", 13, "#ccc", anchor="w")
        self.entry(self.player_name, 170, 176)
        self.text(48, 222, "Song", 13, "#ccc", anchor="w")
        song_var = tk.StringVar(value=playable[0].title if playable else "")
        self.widget(ttk.Combobox(self.c, textvariable=song_var, values=[s.title for s in playable],
                                 state="readonly", width=30), 170, 222, anchor="w")
        self.text(48, 268, "Port", 13, "#ccc", anchor="w")
        self.entry(self.port, 170, 268, width=8)
        Button(self.c, 48, 310, 300, 60, "Start room", lambda: self.host_room(song_var.get()), style="small", size=17)
        self.text(48, 400, f"Friends join with:  {net.local_ip()}:{self.port.get()}  (same Wi-Fi / network)\n\n"
                           f"Over the internet: forward TCP port {self.port.get()} on your router and give "
                           "them your public IP, or use playit.gg or Tailscale.\n\n"
                           "Live voice goes through your Discord call.", 12, "#bbb", anchor="nw", width=480)

        self.panel(585, 82, 549, 534)
        self.text(615, 122, "Join a room", 26, bold=True, anchor="w")
        self.text(615, 176, "Your name", 13, "#ccc", anchor="w")
        self.entry(self.player_name, 760, 176)
        self.text(615, 222, "Server address", 13, "#ccc", anchor="w")
        self.entry(self.address, 760, 222)
        self.text(615, 268, "Room code", 13, "#ccc", anchor="w")
        self.entry(self.code, 760, 268, width=8)
        Button(self.c, 615, 310, 300, 60, "Join", self.join_room, style="small", size=17)
        self.text(615, 400, "Ask the host for their address (like 192.168.1.5:7777) and the 4-letter room code. "
                            "You need the same song in your maps folder.", 12, "#bbb", anchor="nw", width=480)

    def connect(self, address):
        self.leave_room()
        songs = [s.key for s in self.songs if not s.missing_audio()]
        try:
            self.net = net.Client(address, self.player_name.get().strip() or "Player", songs)
        except (OSError, ValueError) as e:
            messagebox.showerror("Song Voicer", f"Couldn't connect to {address}:\n{e}")
            return False
        self.view = "joining"
        return True

    def host_room(self, title):
        song = next((s for s in self.songs if s.title == title), None)
        if not song:
            return messagebox.showinfo("Song Voicer", "Pick a song first.")
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
        self.text(198, 252, f"{len(r['players'])} player{'s' * (len(r['players']) != 1)}", 12, "#aaa")

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

    # ---------- create (not built yet) ----------
    def create_screen(self):
        self.screen("Main Menu > Create", back=self.main_menu)
        self.panel(18, 82, 1116, 534)
        self.text(60, 130, "Create a map", 28, bold=True, anchor="w")
        info = ("The map editor isn't built yet. For now, a map is a folder in maps/ containing:\n\n"
                "•  <name>.map.json   – characters, colors, sprites and the timing of every line\n"
                "•  instrumental + vocals audio   – the vocals file is the guide and gets muted for you\n"
                "•  sprites/   – one picture per character\n"
                "•  any .txt file   – lyrics with [CHARACTER] headers (or use Paste lyrics on the song screen)")
        self.text(60, 175, info, 15, "#ddd", anchor="nw", width=1000)
        Button(self.c, 60, 400, 260, 60, "Open maps folder", self.open_maps, style="small", size=17)

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
            nxt.set_enabled(False)

    def pick_song(self, song):
        self.song = song
        self.me = song.characters[0]["id"]
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
        self.start_meter()

    def start_meter(self):
        self.stop_audio()
        if self.mic is None:
            return

        def cb(indata, frames, time, status):
            self.level = float(np.abs(indata).max())

        self.stream = sd.InputStream(device=self.mic, channels=1, samplerate=self.song.sr, callback=cb)
        self.stream.start()

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
        return voice

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
        self.cover = (mix / max(1.0, float(np.abs(mix).max()))).astype(np.float32)

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
