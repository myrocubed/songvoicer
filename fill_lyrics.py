"""Fill a map's empty "text" fields from a lyrics file in [CHARACTER] format.

Usage: python fill_lyrics.py just_monika.map.json lyrics.txt just_monika.json

lyrics.txt is the lyrics as copied from Genius: a [NAME] header, then that
character's lines. Headers like [NATSUKI, spoken] and [ALL GIRLS, MONIKA] work.
Genius' "You might also like" block is skipped automatically.
"""
import json
import re
import sys

ALL_GIRLS = {"monika", "sayori", "yuri", "natsuki"}
IGNORED_WORDS = {"spoken"}


def header_chars(header):
    chars = set()
    for part in header.split(","):
        word = part.strip().lower()
        if word == "all girls":
            chars |= ALL_GIRLS
        elif word not in IGNORED_WORDS:
            chars.add(word)
    return chars


def parse_lyrics(path):
    lines = []
    current = None
    skipping = False
    try:
        with open(path, encoding="utf-8-sig") as f:
            raw_lines = f.readlines()
    except UnicodeDecodeError:  # saved as ANSI by an older editor
        with open(path, encoding="cp1252") as f:
            raw_lines = f.readlines()
    pending = []
    for raw in raw_lines:
        # Genius quirks: a "123Embed" tail on the last line, and headers glued onto a lyric line.
        line = re.sub(r"\d*Embed$", "", raw.strip()).strip()
        glued = re.fullmatch(r"(.*?\S)\s*(\[[^\[\]]+\])", line)
        if glued:
            pending += [glued.group(1), glued.group(2)]
        else:
            pending.append(line)
    for line in pending:
        if not line:
            continue
        m = re.fullmatch(r"\[(.+)\]", line)
        if m:
            skipping = False
            header = m.group(1)
            # Section markers like [Instrumental Break] have no singer.
            current = None if "instrumental" in header.lower() else header_chars(header)
            continue
        if line == "You might also like":
            skipping = True
            continue
        if skipping or current is None:
            continue
        lines.append((current, line))
    return lines


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    map_path, lyrics_path, out_path = sys.argv[1:]
    with open(map_path, encoding="utf-8") as f:
        song = json.load(f)
    lyrics = parse_lyrics(lyrics_path)

    if len(lyrics) != len(song["lines"]):
        print(f"warning: map has {len(song['lines'])} lines, lyrics have {len(lyrics)}")

    for i, (entry, (chars, text)) in enumerate(zip(song["lines"], lyrics), 1):
        expected = entry["char"] if isinstance(entry["char"], list) else [entry["char"]]
        if set(expected) != chars:
            sys.exit(f"line {i} ({entry['t']}s): map says {sorted(expected)}, "
                     f"lyrics say {sorted(chars)}: {text!r}")
        entry["text"] = text

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(song, f, indent=2, ensure_ascii=False)
    print(f"wrote {out_path} ({len(lyrics)} lines)")


if __name__ == "__main__":
    main()
