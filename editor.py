"""The Create screen and the map editor.

Like a DAW playlist: the song's vocals scroll along a timeline. Drag across it to drop a line
between two edges, drag an edge to fine-tune where it starts or ends, drag the middle to move it.
Pick who sings it and type the lyric underneath. Mixed into App (voicer.py) as EditorMixin.
"""
import glob
import json
import os
import re
import shutil
import tempfile
import tkinter as tk
import zipfile
from tkinter import colorchooser, filedialog, messagebox, ttk

import numpy as np
import sounddevice as sd
from PIL import Image

import theme
from theme import FONT, H, TEXT, Button

TL_X0, TL_X1 = 246, 1122      # timeline area
RULER_Y0, RULER_Y1 = 120, 144
LANE_Y0, LANE_Y1 = 144, 462
EDGE_GRAB = 6                 # pixels around an edge that grab it
MIN_LINE = 0.15               # seconds
BAR_W = 3
COLORS = ["#3fbf6f", "#5fb3ff", "#a77bff", "#ff6fae", "#ffb35c", "#ff4d4d", "#5abed4", "#d8c48a"]
AUDIO_TYPES = [("Audio", "*.mp3 *.wav *.ogg *.flac *.m4a"), ("All files", "*.*")]
EDITOR_EVENTS = ("<ButtonPress-1>", "<B1-Motion>", "<ButtonRelease-1>", "<MouseWheel>", "<Button-4>", "<Button-5>")


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "song"


def line_singers(line):
    return line["char"] if isinstance(line["char"], list) else [line["char"]]


def fmt_time(t):
    return f"{int(t // 60)}:{t % 60:05.2f}"


class EditorMixin:
    # ---------------- Create screen ----------------
    def create_screen(self):
        self.screen("Main Menu > Create", back=self.main_menu)
        self.panel(18, 82, 440, 534, "light")
        self.text(46, 114, "Edit a map", 14, TEXT, bold=True, anchor="w")
        self.c.create_line(40, 134, 436, 134, fill="#e2e2e2")
        for i, s in enumerate(self.songs):
            y, tag = 140 + i * 48, f"edsong{i}"
            row = self.c.create_rectangle(30, y, 446, y + 44, fill="#ffffff", outline="", tags=tag)
            self.text(46, y + 22, s.title, 15, TEXT, anchor="w", tags=tag)
            self.text(436, y + 22, f"{len(s.lines)} lines", 11, "#888", anchor="e", tags=tag)
            self.c.tag_bind(tag, "<Enter>", lambda e, r=row: self.c.itemconfig(r, fill="#c5e6ee"))
            self.c.tag_bind(tag, "<Leave>", lambda e, r=row: self.c.itemconfig(r, fill="#ffffff"))
            self.c.tag_bind(tag, "<Button-1>", lambda e, s=s: self.open_editor(s))
        Button(self.c, 46, 448, 190, 44, "Import .zip…", self.import_zip, style="small", size=14)
        Button(self.c, 244, 448, 190, 44, "Import folder…", self.import_folder, style="small", size=14)
        self.text(46, 512, "Add a song someone shared with you.", 11, "#888", anchor="w")
        Button(self.c, 46, 548, 230, 44, "Open maps folder", self.open_maps, style="small", size=14)

        title, artist, inst, voc = (tk.StringVar() for _ in range(4))
        self.panel(476, 82, 658, 534)
        self.text(506, 122, "New map", 26, bold=True, anchor="w")
        self.text(506, 176, "Song title", 13, "#ccc", anchor="w")
        self.entry(title, 640, 176, width=40)
        self.text(506, 220, "Artist", 13, "#ccc", anchor="w")
        self.entry(artist, 640, 220, width=40)
        for y, label, var in ((272, "Instrumental", inst), (322, "Vocals only", voc)):
            self.text(506, y, label, 13, "#ccc", anchor="w")
            Button(self.c, 640, y - 20, 130, 40, "Choose…", lambda v=var: self.pick_audio(v), style="small", size=13)
            shown = self.text(786, y, "no file", 12, "#888", anchor="w")
            var.trace_add("write", lambda *_, v=var, t=shown: self.c.itemconfig(
                t, text=os.path.basename(v.get()) or "no file", fill="#7fe08f" if v.get() else "#888"))
        self.text(506, 362, "The vocals file is the guide: it's muted for whoever sings a part. Only have the "
                            "full song? Split it into vocals + instrumental with UVR or Demucs first.",
                  12, "#bbb", anchor="nw", width=600)
        Button(self.c, 506, 430, 260, 60, "Create map", lambda: self.create_map(
            title.get(), artist.get(), inst.get(), voc.get()), style="small", size=17)

    def install_songs(self, root_dir):
        """Copy every map folder found under root_dir into the maps folder. Returns the song titles."""
        found = sorted({os.path.dirname(p) for p in glob.glob(os.path.join(root_dir, "**", "*.map.json"),
                                                              recursive=True)})
        titles = []
        for folder in found:
            dest = os.path.join(self.maps_dir, os.path.basename(folder.rstrip(os.sep)) or "song")
            if os.path.exists(dest):
                if not messagebox.askyesno("Song Voicer", f"You already have \"{os.path.basename(dest)}\". "
                                                          "Replace it with the imported one?"):
                    continue
                shutil.rmtree(dest)
            shutil.copytree(folder, dest)
            with open(glob.glob(os.path.join(dest, "*.map.json"))[0], encoding="utf-8") as f:
                titles.append(json.load(f).get("title", os.path.basename(dest)))
        return found, titles

    def finish_import(self, found, titles):
        if not found:
            return messagebox.showwarning("Song Voicer", "No song found there. A song folder has a .map.json "
                                                         "file plus its audio.")
        self.reload_songs()
        self.create_screen()
        if titles:
            messagebox.showinfo("Song Voicer", "Added: " + ", ".join(titles))

    def import_zip(self):
        path = filedialog.askopenfilename(title="Import a song", filetypes=[("Song .zip", "*.zip")])
        if not path:
            return
        with tempfile.TemporaryDirectory() as tmp:
            try:
                with zipfile.ZipFile(path) as z:
                    z.extractall(tmp)
            except zipfile.BadZipFile:
                return messagebox.showerror("Song Voicer", "That file isn't a valid .zip.")
            self.finish_import(*self.install_songs(tmp))

    def import_folder(self):
        path = filedialog.askdirectory(title="Choose a song folder")
        if path:
            self.finish_import(*self.install_songs(path))

    def export_zip(self):
        s = self.song
        if self.ed["dirty"]:
            self.ed_save()
        path = filedialog.asksaveasfilename(title="Share this song", defaultextension=".zip",
                                            initialfile=f"{s.key}.zip", filetypes=[("Song .zip", "*.zip")])
        if path:
            shutil.make_archive(os.path.splitext(path)[0], "zip", root_dir=os.path.dirname(s.folder),
                                base_dir=s.key)
            self.c.itemconfig(self.ed_saved_text, text=f"✓ exported {os.path.basename(path)}")

    def pick_audio(self, var):
        path = filedialog.askopenfilename(title="Choose an audio file", filetypes=AUDIO_TYPES)
        if path:
            var.set(path)

    def create_map(self, title, artist, inst, voc):
        if not title.strip() or not inst or not voc:
            return messagebox.showinfo("Song Voicer", "Give the map a title and choose both audio files.")
        base = os.path.join(self.maps_dir, slug(title))
        folder, n = base, 2
        while os.path.exists(folder):
            folder, n = f"{base}_{n}", n + 1
        os.makedirs(folder)
        names = {}
        for key, src in (("instrumental", inst), ("guide", voc)):
            names[key] = ("instrumental" if key == "instrumental" else "vocals") + os.path.splitext(src)[1].lower()
            shutil.copy(src, os.path.join(folder, names[key]))
        data = {"title": title.strip(), "artist": artist.strip(), "guide": names["guide"],
                "instrumental": names["instrumental"], "characters": [], "lines": []}
        with open(os.path.join(folder, os.path.basename(folder) + ".map.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        self.reload_songs()
        self.open_editor(next(s for s in self.songs if os.path.samefile(s.folder, folder)))

    # ---------------- editor ----------------
    def open_editor(self, song):
        self.song = song
        if not song.sr:
            self.screen(f"Create > {song.title}")
            self.text(576, 340, "Loading song…", 20, TEXT)
            self.root.update()
            song.load_audio()
        # The editor works on the map's own text; lyrics that only came from a .txt are kept
        # (saving writes them into the map), placeholders like "(line 3)" are not.
        for i, line in enumerate(song.lines):
            if song.map_text[i]:
                line["text"] = song.map_text[i]
            elif line.get("text") == f"(line {i + 1})":
                line["text"] = ""
        env = song.env100
        self.ed = {"zoom": 60.0, "scroll": 0.0, "pos": 0.0, "sel": None, "drag": None, "dirty": False,
                   "active": song.characters[0]["id"] if song.characters else None,
                   "env": env / max(1e-6, np.percentile(env, 99.5)), "play": None, "loading": False}
        self.editor()

    def editor(self):
        s = self.song
        self.screen(f"Create > {s.title}", back=self.leave_editor)
        self.view = "editor"
        ed = self.ed

        # toolbar
        self.play_btn = Button(self.c, 18, 72, 120, 40, "▶  Play", self.ed_toggle_play, style="small", size=14)
        Button(self.c, 146, 72, 100, 40, "■  Stop", self.ed_stop, style="small", size=14)
        Button(self.c, 254, 72, 44, 40, "−", lambda: self.ed_zoom(1 / 1.5), style="small", size=16)
        Button(self.c, 304, 72, 44, 40, "+", lambda: self.ed_zoom(1.5), style="small", size=16)
        self.ed_time_text = self.text(364, 92, "", 15, TEXT, bold=True, anchor="w")
        Button(self.c, 964, 72, 170, 40, "Share as .zip…", self.export_zip, style="small", size=13)
        self.text(560, H - 26, "Drag on the timeline to add a line  ·  drag edges to adjust  ·  Space play  ·  "
                               "Delete removes  ·  scroll / Ctrl+scroll to zoom", 10, "#ddd")

        # characters
        self.panel(18, 120, 216, 496)
        self.text(36, 146, "Characters", 15, bold=True, anchor="w")
        Button(self.c, 30, 404, 192, 38, "+  Add character", self.ed_add_char, style="small", size=13)
        self.text(36, 462, "Name", 12, "#aaa", anchor="w")
        self.ed_name = tk.StringVar()
        self.ed_name_entry = self.entry(self.ed_name, 80, 462, width=13)
        self.ed_name.trace_add("write", lambda *_: self.ed_rename())
        Button(self.c, 30, 484, 92, 36, "Color", self.ed_color, style="small", size=12)
        Button(self.c, 130, 484, 92, 36, "Sprite…", self.ed_sprite, style="small", size=12)
        Button(self.c, 30, 526, 192, 36, "Remove character", self.ed_remove_char, style="small", size=12)
        self.ed_sprite_text = self.text(126, 586, "", 11, "#888")

        # timeline
        self.panel(234, 112, 900, 364)
        self.ed_scrollbar = ttk.Scrollbar(self.c, orient="horizontal", command=self.ed_scrollbar_cmd)
        self.widget(self.ed_scrollbar, TL_X0, 466, anchor="nw", width=TL_X1 - TL_X0)
        for seq, fn in zip(EDITOR_EVENTS, (self.ed_press, self.ed_motion, self.ed_release,
                                           self.ed_wheel, self.ed_wheel, self.ed_wheel)):
            self.c.bind(seq, fn)
        self.root.bind("<space>", self.ed_key_space)
        self.root.bind("<Delete>", self.ed_key_delete)
        self.root.bind("<BackSpace>", self.ed_key_delete)

        # inspector
        self.panel(234, 490, 900, 126)
        self.ed_lyric = tk.StringVar()
        self.ed_lyric_entry = self.entry(self.ed_lyric, 330, 526, width=62)
        self.ed_lyric.trace_add("write", lambda *_: self.ed_set_lyric())
        self.ed_lyric_entry.bind("<Return>", lambda e: self.c.focus_set())
        self.ed_delete_btn = Button(self.c, 1000, 506, 120, 40, "Delete line", self.ed_delete_line,
                                    style="small", size=13)
        Button(self.c, W_SAVE_X, H - 46, 190, 40, "💾  Save", self.ed_save, style="pill", size=15)
        self.ed_saved_text = self.text(W_SAVE_X - 14, H - 12, "", 11, "#ffd76a", anchor="e")

        self.ed_draw_chars()
        self.ed_draw_inspector()
        self.ed_draw_timeline()
        self.loop(self.ed_tick)

    # ---------- geometry ----------
    def ed_visible(self):
        return (TL_X1 - TL_X0) / self.ed["zoom"]

    def ed_t(self, x):
        return self.ed["scroll"] + (x - TL_X0) / self.ed["zoom"]

    def ed_x(self, t):
        return TL_X0 + (t - self.ed["scroll"]) * self.ed["zoom"]

    def ed_clamp_scroll(self):
        self.ed["scroll"] = max(0.0, min(self.ed["scroll"], max(0.0, self.song.length - self.ed_visible())))

    # ---------- drawing ----------
    def ed_draw_timeline(self):
        c, s, ed = self.c, self.song, self.ed
        c.delete("tl")
        self.ed_clamp_scroll()
        t0, t1 = ed["scroll"], ed["scroll"] + self.ed_visible()

        # ruler
        c.create_rectangle(TL_X0, RULER_Y0, TL_X1, RULER_Y1, fill="#1c1c1c", width=0, tags="tl")
        step = next(v for v in (0.25, 0.5, 1, 2, 5, 10, 15, 30, 60) if v * ed["zoom"] >= 70)
        t = np.ceil(t0 / step) * step
        while t <= t1:
            x = self.ed_x(t)
            c.create_line(x, RULER_Y1 - 8, x, RULER_Y1, fill="#888", tags="tl")
            c.create_line(x, LANE_Y0, x, LANE_Y1, fill="#333", tags="tl")
            c.create_text(x + 3, RULER_Y0 + 9, text=f"{int(t // 60)}:{t % 60:04.1f}" if step < 1 else
                          f"{int(t // 60)}:{int(t % 60):02d}", anchor="w", fill="#aaa", font=(FONT, 9), tags="tl")
            t += step

        # who sings at each bar
        n_bars = int((TL_X1 - TL_X0) / BAR_W)
        mids = t0 + (np.arange(n_bars) + 0.5) * BAR_W / ed["zoom"]
        colors = np.full(n_bars, "#5a5a5a", dtype=object)
        visible = [l for l in s.lines if l["end"] >= t0 and l["t"] <= t1]
        for line in visible:
            who = line_singers(line)
            color = s.chars[who[0]]["color"] if len(who) == 1 and who[0] in s.chars else "#e8e8e8"
            colors[(mids >= line["t"]) & (mids <= line["end"])] = color

        # waveform
        env = ed["env"]
        mid, half = (LANE_Y0 + 30 + LANE_Y1) / 2, (LANE_Y1 - LANE_Y0 - 40) / 2
        a = np.clip(((mids - BAR_W / 2 / ed["zoom"]) * 100).astype(int), 0, len(env) - 1)
        b = np.clip(((mids + BAR_W / 2 / ed["zoom"]) * 100).astype(int) + 1, 1, len(env))
        for k in range(n_bars):
            h = max(1.0, min(1.0, float(env[a[k]:b[k]].max(initial=0))) * half)
            x = TL_X0 + k * BAR_W
            c.create_rectangle(x, mid - h, x + BAR_W - 1, mid + h, fill=colors[k], width=0, tags="tl")

        # lines: a label strip on top, an edge line at each end
        for line in visible:
            who = line_singers(line)
            color = s.chars[who[0]]["color"] if len(who) == 1 and who[0] in s.chars else "#e8e8e8"
            xa, xb = self.ed_x(line["t"]), self.ed_x(line["end"])
            sel = line is ed["sel"]
            c.create_rectangle(max(TL_X0, xa), LANE_Y0 + 4, min(TL_X1, xb), LANE_Y0 + 28, fill=color,
                               outline="white" if sel else "", width=2, tags="tl")
            names = " + ".join(s.chars[w]["name"] for w in who if w in s.chars)
            label = f"{names}: {line.get('text') or '…'}"
            room = int((min(TL_X1, xb) - max(TL_X0, xa) - 8) / 7)
            if room > 3:
                c.create_text(max(TL_X0, xa) + 4, LANE_Y0 + 16, text=label if len(label) <= room else
                              label[:room - 1] + "…", anchor="w", fill="#111", font=(FONT, 10, "bold"), tags="tl")
            for x in (xa, xb):
                if TL_X0 <= x <= TL_X1:
                    c.create_line(x, LANE_Y0, x, LANE_Y1, fill="white" if sel else color, width=3 if sel else 2,
                                  tags="tl")

        drag = ed["drag"]
        if drag and drag[0] == "new":
            xa, xb = sorted((self.ed_x(drag[1]), self.ed_x(drag[2])))
            color = s.chars[ed["active"]]["color"] if ed["active"] in s.chars else "white"
            for x in (xa, xb):
                c.create_line(x, LANE_Y0, x, LANE_Y1, fill=color, width=2, dash=(4, 3), tags="tl")
            c.create_text(xa + 4, LANE_Y0 + 16, text=f"{xb / ed['zoom'] - xa / ed['zoom']:.2f}s", anchor="w",
                          fill=color, font=(FONT, 10, "bold"), tags="tl")

        self.ed_playhead = c.create_line(0, RULER_Y0, 0, LANE_Y1, fill="#ffd34d", width=2, tags="tl")
        self.ed_move_playhead()
        length = max(1e-6, s.length)
        self.ed_scrollbar.set(t0 / length, min(1.0, t1 / length))

    def ed_move_playhead(self):
        x = self.ed_x(self.ed["pos"])
        shown = TL_X0 <= x <= TL_X1
        self.c.coords(self.ed_playhead, x, RULER_Y0, x, LANE_Y1)
        self.c.itemconfig(self.ed_playhead, state="normal" if shown else "hidden")
        self.c.itemconfig(self.ed_time_text, text=f"{fmt_time(self.ed['pos'])} / {fmt_time(self.song.length)}")

    def ed_draw_chars(self):
        c, s, ed = self.c, self.song, self.ed
        c.delete("chars")
        if not s.characters:
            self.text(36, 190, "No characters yet.\nAdd one, then drag\non the timeline.", 12, "#aaa",
                      anchor="nw", tags="chars")
        for i, ch in enumerate(s.characters[:7]):
            y, tag = 176 + i * 32, f"edchar{i}"
            active = ch["id"] == ed["active"]
            c.create_rectangle(28, y - 13, 224, y + 13, fill="#3c3c3c" if active else "#2b2b2b", width=0,
                               tags=("chars", tag))
            if active:
                c.create_rectangle(28, y - 13, 32, y + 13, fill="#e22828", width=0, tags=("chars", tag))
            c.create_rectangle(40, y - 7, 54, y + 7, fill=ch["color"], width=0, tags=("chars", tag))
            count = sum(ch["id"] in line_singers(l) for l in s.lines)
            self.text(62, y, ch["name"], 12, "white", bold=active, anchor="w", tags=("chars", tag))
            self.text(216, y, str(count), 11, "#888", anchor="e", tags=("chars", tag))
            c.tag_bind(tag, "<Button-1>", lambda e, cid=ch["id"]: self.ed_set_active(cid))
        active = s.chars.get(ed["active"])
        if self.ed_name.get().strip() != (active["name"] if active else ""):  # don't jump the cursor while typing
            ed["loading"] = True
            self.ed_name.set(active["name"] if active else "")
            ed["loading"] = False
        self.ed_name_entry.config(state="normal" if active else "disabled")
        sprite = active.get("sprite") if active else None
        c.itemconfig(self.ed_sprite_text, text=f"sprite: {os.path.basename(sprite)}" if sprite else
                     ("no sprite (grey silhouette)" if active else ""))

    def ed_draw_inspector(self):
        c, s, ed = self.c, self.song, self.ed
        c.delete("insp")
        line = ed["sel"]
        if self.ed_lyric.get() != (line.get("text", "") if line else ""):
            ed["loading"] = True
            self.ed_lyric.set(line.get("text", "") if line else "")
            ed["loading"] = False
        self.ed_lyric_entry.config(state="normal" if line else "disabled")
        self.ed_delete_btn.set_enabled(line is not None)
        self.text(252, 526, "Lyric", 13, "#ccc", anchor="w", tags="insp")
        if not line:
            name = s.chars[ed["active"]]["name"] if ed["active"] in s.chars else "a character"
            self.text(252, 576, f"Drag across the timeline to add a line for {name}. Click a line to edit it.",
                      12, "#aaa", anchor="w", tags="insp")
            return
        self.text(252, 580, "Sung by", 13, "#ccc", anchor="w", tags="insp")
        who = line_singers(line)
        x = 330
        for ch in s.characters:
            on = ch["id"] in who
            w = 18 + len(ch["name"]) * 8
            tag = f"chip{ch['id']}"
            c.create_rectangle(x, 566, x + w, 594, fill=ch["color"] if on else "#2b2b2b", outline=ch["color"],
                               width=2, tags=("insp", tag))
            self.text(x + w / 2, 580, ch["name"], 11, "#111" if on else ch["color"], bold=on, tags=("insp", tag))
            c.tag_bind(tag, "<Button-1>", lambda e, cid=ch["id"]: self.ed_toggle_singer(cid))
            x += w + 8
        self.text(1120, 580, f"{fmt_time(line['t'])} → {fmt_time(line['end'])}   "
                             f"({line['end'] - line['t']:.2f}s)", 12, "#aaa", anchor="e", tags="insp")

    # ---------- mouse & keys ----------
    def ed_press(self, e):
        if self.view != "editor" or not (TL_X0 <= e.x <= TL_X1 and RULER_Y0 <= e.y <= LANE_Y1):
            return
        self.c.focus_set()
        ed, lines = self.ed, self.song.lines
        t = max(0.0, min(self.song.length, self.ed_t(e.x)))
        if e.y < RULER_Y1:
            ed["drag"] = ("seek",)
            return self.ed_seek(t)
        order = ([ed["sel"]] if ed["sel"] else []) + [l for l in lines if l is not ed["sel"]]
        edge = next(((l, k) for l in order for k in ("t", "end") if abs(self.ed_x(l[k]) - e.x) <= EDGE_GRAB), None)
        if edge:
            ed["sel"], ed["drag"] = edge[0], ("edge", edge[0], edge[1])
        else:
            inside = next((l for l in order if l["t"] <= t <= l["end"]), None)
            if inside:
                ed["sel"], ed["drag"] = inside, ("move", inside, t - inside["t"])
            elif ed["active"] not in self.song.chars:
                return messagebox.showinfo("Song Voicer", "Add a character first (left panel).")
            else:
                ed["sel"], ed["drag"] = None, ("new", t, t)
        self.ed_draw_inspector()
        self.ed_draw_timeline()

    def ed_motion(self, e):
        ed = self.ed
        drag = ed["drag"]
        if self.view != "editor" or not drag:
            return
        # drag past the edge of the timeline to scroll it
        if e.x > TL_X1:
            ed["scroll"] += (e.x - TL_X1) / ed["zoom"] * 0.5
        elif e.x < TL_X0:
            ed["scroll"] -= (TL_X0 - e.x) / ed["zoom"] * 0.5
        self.ed_clamp_scroll()
        t = max(0.0, min(self.song.length, self.ed_t(min(max(e.x, TL_X0), TL_X1))))
        if drag[0] == "seek":
            return self.ed_seek(t)
        if drag[0] == "edge":
            line, key = drag[1], drag[2]
            if key == "t":
                line["t"] = round(min(t, line["end"] - MIN_LINE), 2)
            else:
                line["end"] = round(max(t, line["t"] + MIN_LINE), 2)
        elif drag[0] == "move":
            line, offset = drag[1], drag[2]
            length = line["end"] - line["t"]
            start = max(0.0, min(self.song.length - length, t - offset))
            line["t"], line["end"] = round(start, 2), round(start + length, 2)
        elif drag[0] == "new":
            ed["drag"] = ("new", drag[1], t)
        if drag[0] in ("edge", "move"):
            self.ed_changed()
            self.ed_draw_inspector()
        self.ed_draw_timeline()

    def ed_release(self, e):
        ed = self.ed
        drag = ed["drag"]
        ed["drag"] = None
        if self.view != "editor" or not drag:
            return
        if drag[0] == "new":
            a, b = sorted((drag[1], drag[2]))
            if b - a >= MIN_LINE:
                line = {"t": round(a, 2), "end": round(b, 2), "char": ed["active"], "text": ""}
                self.song.lines.append(line)
                ed["sel"] = line
                self.ed_changed()
                self.ed_lyric_entry.after(10, self.ed_lyric_entry.focus_set)
        self.song.lines.sort(key=lambda l: l["t"])
        self.ed_draw_chars()
        self.ed_draw_inspector()
        self.ed_draw_timeline()

    def ed_wheel(self, e):
        if self.view != "editor" or not (TL_X0 <= e.x <= TL_X1 and RULER_Y0 <= e.y <= LANE_Y1):
            return
        if e.num in (4, 5):
            steps = 1 if e.num == 4 else -1
        else:
            steps = e.delta / 120 if abs(e.delta) >= 120 else e.delta
        if e.state & 0x4:  # Ctrl: zoom around the mouse
            self.ed_zoom(1.25 ** steps, anchor_x=e.x)
        else:
            self.ed["scroll"] -= steps * self.ed_visible() * 0.1
            self.ed_draw_timeline()

    def ed_zoom(self, factor, anchor_x=None):
        ed = self.ed
        anchor_x = anchor_x if anchor_x is not None else (TL_X0 + TL_X1) / 2
        t = self.ed_t(anchor_x)
        ed["zoom"] = max(5.0, min(800.0, ed["zoom"] * factor))
        ed["scroll"] = t - (anchor_x - TL_X0) / ed["zoom"]
        self.ed_draw_timeline()

    def ed_scrollbar_cmd(self, action, value, unit=None):
        ed = self.ed
        if action == "moveto":
            ed["scroll"] = float(value) * self.song.length
        elif action == "scroll":
            ed["scroll"] += int(value) * self.ed_visible() * (0.9 if unit == "pages" else 0.1)
        self.ed_draw_timeline()

    def typing(self):
        return isinstance(self.root.focus_get(), (tk.Entry, ttk.Entry))

    def ed_key_space(self, e):
        if self.view == "editor" and not self.typing():
            self.ed_toggle_play()
            return "break"

    def ed_key_delete(self, e):
        if self.view == "editor" and not self.typing():
            self.ed_delete_line()

    # ---------- editing ----------
    def ed_changed(self):
        self.ed["dirty"] = True
        self.c.itemconfig(self.ed_saved_text, text="unsaved changes")

    def ed_set_active(self, cid):
        self.ed["active"] = cid
        self.ed_draw_chars()
        self.ed_draw_inspector()

    def ed_add_char(self):
        s = self.song
        n = len(s.characters)
        name = f"Singer {n + 1}"
        cid, k = slug(name), 2
        while cid in s.chars:
            cid, k = f"{slug(name)}_{k}", k + 1
        ch = {"id": cid, "name": name, "color": COLORS[n % len(COLORS)]}
        s.characters.append(ch)
        s.chars[cid] = ch
        self.ed["active"] = cid
        self.ed_changed()
        self.ed_draw_chars()
        self.ed_draw_inspector()
        self.ed_name_entry.focus_set()
        self.ed_name_entry.select_range(0, "end")

    def ed_rename(self):
        ch = self.song.chars.get(self.ed["active"])
        if self.ed["loading"] or not ch:
            return
        ch["name"] = self.ed_name.get().strip() or ch["name"]
        self.ed_changed()
        self.c.after_idle(self.ed_redraw_names)

    def ed_redraw_names(self):
        if self.view == "editor":
            focus = self.root.focus_get()
            self.ed_draw_chars()
            self.ed_draw_inspector()
            self.ed_draw_timeline()
            if focus:
                focus.focus_set()

    def ed_color(self):
        ch = self.song.chars.get(self.ed["active"])
        if not ch:
            return
        color = colorchooser.askcolor(ch["color"], title=f"Color for {ch['name']}")[1]
        if color:
            ch["color"] = color
            self.ed_changed()
            self.ed_redraw_names()

    def ed_sprite(self):
        ch = self.song.chars.get(self.ed["active"])
        if not ch:
            return
        path = filedialog.askopenfilename(title=f"Picture for {ch['name']}",
                                          filetypes=[("Images", "*.png *.webp *.jpg *.jpeg *.gif"), ("All", "*.*")])
        if not path:
            return
        os.makedirs(os.path.join(self.song.folder, "sprites"), exist_ok=True)
        rel = f"sprites/{ch['id']}.png"
        Image.open(path).convert("RGBA").save(os.path.join(self.song.folder, rel))
        ch["sprite"] = rel
        self.ed_changed()
        self.ed_draw_chars()

    def ed_remove_char(self):
        s, ed = self.song, self.ed
        ch = s.chars.get(ed["active"])
        if not ch:
            return
        used = [l for l in s.lines if ch["id"] in line_singers(l)]
        if used and not messagebox.askyesno("Song Voicer", f"Remove {ch['name']}? Their {len(used)} line(s) "
                                                           "lose this singer, and lines only they sing are deleted."):
            return
        for line in used:
            who = [w for w in line_singers(line) if w != ch["id"]]
            if who:
                line["char"] = who[0] if len(who) == 1 else who
            else:
                s.lines.remove(line)
                if ed["sel"] is line:
                    ed["sel"] = None
        s.characters.remove(ch)
        del s.chars[ch["id"]]
        ed["active"] = s.characters[0]["id"] if s.characters else None
        self.ed_changed()
        self.ed_redraw_names()

    def ed_set_lyric(self):
        line = self.ed["sel"]
        if self.ed["loading"] or not line:
            return
        line["text"] = self.ed_lyric.get()
        self.ed_changed()
        self.ed_draw_timeline()

    def ed_toggle_singer(self, cid):
        line = self.ed["sel"]
        who = line_singers(line)
        who = [w for w in who if w != cid] if cid in who else who + [cid]
        if not who:
            return  # a line needs at least one singer
        line["char"] = who[0] if len(who) == 1 else who
        self.ed_changed()
        self.ed_draw_chars()
        self.ed_draw_inspector()
        self.ed_draw_timeline()

    def ed_delete_line(self):
        line = self.ed["sel"]
        if line:
            self.song.lines.remove(line)
            self.ed["sel"] = None
            self.ed_changed()
            self.ed_draw_chars()
            self.ed_draw_inspector()
            self.ed_draw_timeline()

    # ---------- playback ----------
    def ed_seek(self, t):
        playing = self.ed["play"] is not None
        if playing:
            self.ed_pause()
        self.ed["pos"] = t
        self.ed_move_playhead()
        if playing:
            self.ed_toggle_play()

    def ed_toggle_play(self):
        if self.ed["play"]:
            return self.ed_pause()
        s = self.song
        pos = [int(self.ed["pos"] * s.sr)]
        n = len(s.inst)

        def cb(outdata, frames, time, status):
            a = pos[0]
            b = min(a + frames, n)
            outdata[:b - a] = s.inst[a:b] + s.voc[a:b]
            outdata[b - a:] = 0
            pos[0] = b
            if b >= n:
                raise sd.CallbackStop

        self.stop_audio()
        self.stream = sd.OutputStream(samplerate=s.sr, channels=2, callback=cb)
        self.stream.start()
        self.ed["play"] = pos
        self.play_btn.set_text("❚❚  Pause")

    def ed_pause(self):
        self.stop_audio()
        self.ed["play"] = None
        self.play_btn.set_text("▶  Play")

    def ed_stop(self):
        self.ed_pause()
        self.ed["pos"] = 0.0
        self.ed["scroll"] = 0.0
        self.ed_draw_timeline()

    def ed_tick(self):
        ed = self.ed
        if not ed["play"]:
            return
        ed["pos"] = ed["play"][0] / self.song.sr
        if not self.stream or not self.stream.active:
            self.ed_pause()
        # keep the playhead on screen, like a DAW page-follow
        if ed["pos"] > ed["scroll"] + self.ed_visible() * 0.92 or ed["pos"] < ed["scroll"]:
            ed["scroll"] = ed["pos"] - self.ed_visible() * 0.08
            self.ed_draw_timeline()
        else:
            self.ed_move_playhead()

    # ---------- save / leave ----------
    def ed_save(self):
        s = self.song
        s.lines.sort(key=lambda l: l["t"])
        data = dict(s.data, characters=s.characters, lines=s.lines, length=round(s.length, 2))
        path = glob.glob(os.path.join(s.folder, "*.map.json"))[0]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        self.ed["dirty"] = False
        self.c.itemconfig(self.ed_saved_text, text="✓ saved")

    def leave_editor(self):
        if self.ed["dirty"]:
            answer = messagebox.askyesnocancel("Song Voicer", "Save your changes to this map?")
            if answer is None:
                return
            if answer:
                self.ed_save()
        self.ed_pause()
        self.reload_songs()
        self.create_screen()


W_SAVE_X = theme.W - 200
