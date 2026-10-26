# PyInstaller build for Song Voicer.   Build with:  pyinstaller song_voicer.spec --noconfirm
# Must run on the platform you're building for: on a Mac it makes "dist/Song Voicer.app",
# on Windows "dist/Song Voicer/Song Voicer.exe", on Linux "dist/Song Voicer/Song Voicer".
# Python is bundled inside, users don't install it.
import os
import subprocess
import sys

binaries = []
if sys.platform.startswith("linux"):
    # sounddevice's Linux wheel has no PortAudio inside; bundle the build machine's libportaudio2.
    lib = subprocess.run(["sh", "-c", "ldconfig -p | grep -o '/.*libportaudio.so.2$' | head -n1"],
                         capture_output=True, text=True).stdout.strip()
    if lib:
        binaries.append((lib, "."))

a = Analysis(
    ["voicer.py"],
    binaries=binaries,
    # Songs get copied to the user's data folder on first run. Without a maps folder (e.g. a public repo
    # that leaves out the copyrighted audio) the app ships empty and players add their own song folders.
    datas=[("maps", "maps")] if os.path.isdir("maps") else [],
    excludes=["torch", "demucs", "rembg", "faster_whisper", "onnxruntime", "scipy", "matplotlib"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Song Voicer", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="Song Voicer")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Song Voicer.app",
        bundle_identifier="com.songvoicer.app",
        info_plist={
            # Without this macOS silently blocks the microphone.
            "NSMicrophoneUsageDescription": "Song Voicer records your voice while you sing your part.",
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": "1.0",
        },
    )
