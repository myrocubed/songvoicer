import sys

from PIL import Image, ImageDraw, ImageFilter, ImageTk

W, H = 1152, 680
HEADER_H, FOOTER_H = 60, 52
FONT = {"win32": "Segoe UI", "darwin": "Helvetica Neue"}.get(sys.platform, "DejaVu Sans")
TEXT = "#222222"
RED = (226, 40, 40, 255)
CYAN_LINE = (90, 190, 212, 255)
SS = 3  # supersampling 

_cache = {}


def _keep(key, make):
    if key not in _cache:
        _cache[key] = ImageTk.PhotoImage(make())
    return _cache[key]


def _gradient(w, h, top, bottom):
    img = Image.new("RGBA", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    return img


def _rounded(img, r):
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.width - 1, img.height - 1], r, fill=255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def background():
    def make():
        img = Image.new("RGBA", (W, H), (245, 245, 245, 255))
        d = ImageDraw.Draw(img)
        for y in range(0, H, 18):
            d.rectangle([0, y, W, y + 8], fill=(233, 233, 233, 255))
        swoosh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        s = ImageDraw.Draw(swoosh)
        s.arc([-300, 140, 900, 1100], 200, 330, fill=(140, 210, 226, 120), width=30)
        s.arc([520, -420, 1520, 600], 40, 170, fill=(140, 210, 226, 90), width=20)
        s.arc([320, 300, 1420, 1200], 190, 300, fill=(50, 60, 70, 45), width=12)
        img.alpha_composite(swoosh.filter(ImageFilter.GaussianBlur(1)))
        img.alpha_composite(_gradient(W, HEADER_H, (66, 66, 66), (104, 104, 104)), (0, 0))
        img.alpha_composite(_gradient(W, FOOTER_H, (112, 112, 112), (58, 58, 58)), (0, H - FOOTER_H))
        d.rectangle([0, HEADER_H, W, HEADER_H + 3], fill=CYAN_LINE)
        d.rectangle([0, H - FOOTER_H - 3, W, H - FOOTER_H], fill=CYAN_LINE)
        return img
    return _keep("bg", make)


STYLES = {
    "big": ((223, 243, 248), (156, 204, 216), 18),
    "small": ((216, 241, 247), (146, 198, 211), 3),
    "pill": ((252, 252, 252), (196, 196, 196), 99),
    "off": ((168, 168, 168), (120, 120, 120), 18),
    "down": ((170, 214, 225), (120, 178, 192), 18),
}


def button_image(w, h, style="big", ring=False):
    def make():
        top, bottom, radius = STYLES[style]
        if style in ("off", "down"):
            radius = 3 if h < 50 else radius
        pad = 4
        r = min(radius, (h - 2 * pad) // 2) * SS
        big = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        if ring:
            d.rounded_rectangle([0, 0, w * SS - 1, h * SS - 1], r + pad * SS, fill=RED)
        o = (pad - 2) * SS
        d.rounded_rectangle([o, o, w * SS - o - 1, h * SS - o - 1], r + 2 * SS, fill=(38, 38, 38, 255))
        inner = pad * SS
        bw, bh = w * SS - 2 * inner, h * SS - 2 * inner
        body = _gradient(bw, bh, top, bottom)
        gloss = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        ImageDraw.Draw(gloss).rounded_rectangle([3 * SS, 2 * SS, bw - 3 * SS, int(bh * 0.48)], r,
                                                fill=(255, 255, 255, 70))
        body.alpha_composite(gloss)
        ImageDraw.Draw(body).rounded_rectangle([0, 0, bw - 1, bh - 1], r, outline=(255, 255, 255, 210),
                                               width=2 * SS)
        big.alpha_composite(_rounded(body, r), (inner, inner))
        return big.resize((w, h), Image.LANCZOS)
    return _keep(("btn", w, h, style, ring), make)


def panel_image(w, h, kind="light"):
    def make():
        big = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        box = [0, 0, w * SS - 1, h * SS - 1]
        if kind == "dark":
            d.rounded_rectangle(box, 9 * SS, fill=(20, 20, 20, 255))
            d.rounded_rectangle([2 * SS, 2 * SS, box[2] - 2 * SS, box[3] - 2 * SS], 8 * SS, fill=(235, 235, 235, 255))
            d.rounded_rectangle([5 * SS, 5 * SS, box[2] - 5 * SS, box[3] - 5 * SS], 6 * SS, fill=(40, 40, 40, 255))
        else:
            r = (26 if kind == "light" else 14) * SS
            d.rounded_rectangle(box, r, fill=(48, 48, 48, 255))
            d.rounded_rectangle([2 * SS, 2 * SS, box[2] - 2 * SS, box[3] - 2 * SS], r - 2 * SS, fill=(214, 214, 214, 255))
            d.rounded_rectangle([5 * SS, 5 * SS, box[2] - 5 * SS, box[3] - 5 * SS], r - 5 * SS, fill=(255, 255, 255, 255))
        return big.resize((w, h), Image.LANCZOS)
    return _keep(("panel", w, h, kind), make)


def breadcrumb(canvas, text):
    w = int(len(text) * 10.5) + 44

    def make():
        big = Image.new("RGBA", (w * SS, 34 * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        d.rounded_rectangle([0, 0, w * SS - 1, 34 * SS - 1], 17 * SS, fill=(30, 30, 30, 230),
                            outline=(130, 130, 130, 255), width=SS)
        return big.resize((w, 34), Image.LANCZOS)
    canvas.create_image(6, 13, anchor="nw", image=_keep(("crumb", w), make))
    canvas.create_text(23, 31, anchor="w", text=text, fill="#111", font=(FONT, 14))
    canvas.create_text(22, 30, anchor="w", text=text, fill="white", font=(FONT, 14))


# The menu waveform (also the app icon), traced from a sketch 570 units wide:
# (x, height above the line, height below it).
MENU_WAVE = [
    (0, 50, 50), (10, 58, 75), (15, 65, 90), (25, 95, 135), (30, 105, 150), (40, 130, 180), (45, 140, 182),
    (55, 145, 185), (70, 135, 178), (80, 130, 170), (85, 125, 165), (95, 112, 150), (100, 105, 85),
    (105, 98, 75), (115, 65, 72), (125, 45, 70), (130, 35, 55), (140, 22, 25), (150, 15, 20), (170, 5, 13),
    (190, 3, 5), (200, 0, 0), (232, 0, 0),
    (235, 95, 80), (245, 100, 120), (255, 103, 135), (270, 103, 150), (280, 112, 135), (285, 125, 120),
    (298, 125, 110), (320, 70, 105), (340, 48, 70), (345, 40, 62), (370, 20, 35), (395, 5, 15), (405, 0, 0),
    (570, 0, 0),
]


def brackets(canvas, x0, y0, x1, y1, tag, color="#e22828", size=14, width=3):
    for x, y, dx, dy in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
        canvas.create_line(x, y + dy * size, x, y, x + dx * size, y, fill=color, width=width, tags=tag)


class Button:

    def __init__(self, canvas, x, y, w, h, text, command, style="big", size=20, tags=()):
        self.c, self.command, self.w, self.h, self.style = canvas, command, w, h, style
        self.enabled, self.selected, self.hover, self.down = True, False, False, False
        self.tag = f"btn{id(self)}"
        all_tags = (self.tag,) + tuple(tags)
        self.img = canvas.create_image(x, y, anchor="nw", tags=all_tags)
        self.txt = canvas.create_text(x + w / 2, y + h / 2, text=text, font=(FONT, size), fill=TEXT, tags=all_tags)
        canvas.tag_bind(self.tag, "<Enter>", lambda e: self._set(hover=True))
        canvas.tag_bind(self.tag, "<Leave>", lambda e: self._set(hover=False, down=False))
        canvas.tag_bind(self.tag, "<ButtonPress-1>", lambda e: self._set(down=True))
        canvas.tag_bind(self.tag, "<ButtonRelease-1>", self._release)
        self._draw()

    def _set(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self._draw()

    def _release(self, _):
        was_down = self.down
        self._set(down=False)
        if was_down and self.hover and self.enabled:
            self.command()

    def _draw(self):
        style = "off" if not self.enabled else ("down" if self.down and self.style != "pill" else self.style)
        ring = self.enabled and (self.hover or self.selected)
        self.c.itemconfig(self.img, image=button_image(self.w, self.h, style, ring))
        self.c.itemconfig(self.txt, fill=TEXT if self.enabled else "#5a5a5a")

    def set_enabled(self, on):
        self._set(enabled=on)

    def set_text(self, text):
        self.c.itemconfig(self.txt, text=text)
