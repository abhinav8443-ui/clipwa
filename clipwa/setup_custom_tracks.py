"""
Setup helper to download and process the 3 viral background instrumentals.
"""

import glob
import os
import subprocess
from pathlib import Path
import yt_dlp

from .config import Config


def download_and_prepare_custom_tracks(force: bool = False) -> list[str]:
    """
    Downloads and prepares:
    1. track_1_4_raws.wav (4 Raws instrumental slowed edit)
    2. track_2_if_we_being_real.wav (Yeat - If We Being Real instrumental slowed)
    3. track_3_love_me.wav (Lil Wayne / Future - Love Me instrumental slowed)
    Saves them to assets/audio/custom_tracks/
    """
    custom_tracks_dir = Config.BASE_DIR / "assets" / "audio" / "custom_tracks"
    custom_tracks_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = Config.TEMP_DIR
    temp_dir.mkdir(parents=True, exist_ok=True)

    tracks_to_fetch = [
        ("track_1_4_raws.wav", "ytsearch1:4 raws instrumental slowed edit", "scsearch1:4 raws instrumental slowed"),
        ("track_2_if_we_being_real.wav", "ytsearch1:yeat if we being real instrumental slowed", "scsearch1:yeat if we being real instrumental slowed"),
        ("track_3_love_me.wav", "ytsearch1:lil wayne future love me instrumental slowed", "scsearch1:future lil wayne love me instrumental slowed"),
    ]

    ffmpeg_exe = str(Config.BIN_DIR / "ffmpeg.exe") if (Config.BIN_DIR / "ffmpeg.exe").exists() else Config.FFMPEG_PATH

    prepared_paths = []

    for filename, yt_query, sc_query in tracks_to_fetch:
        final_path = custom_tracks_dir / filename
        if final_path.exists() and final_path.stat().st_size > 100000 and not force:
            print(f"[CustomTracks] Already exists: {final_path.name} ({final_path.stat().st_size} bytes)")
            prepared_paths.append(str(final_path))
            continue

        print(f"\n[CustomTracks] Fetching: {filename}...")
        temp_out = str(temp_dir / f"temp_{filename}_raw.%(ext)s")
        # Clean any stale files
        for f in glob.glob(str(temp_dir / f"temp_{filename}_raw.*")):
            try:
                os.remove(f)
            except Exception:
                pass

        ydl_opts = {
            "format": "ba/b",
            "outtmpl": temp_out,
            "ffmpeg_location": ffmpeg_exe,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
            "retries": 5,
        }

        # Try YouTube search first, fall back to SoundCloud search if bot blocked
        download_success = False
        for query in [yt_query, sc_query]:
            try:
                print(f"[CustomTracks] Trying query: '{query}'...")
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([query])
                cands = glob.glob(str(temp_dir / f"temp_{filename}_raw.*"))
                if cands:
                    download_success = True
                    break
            except Exception as e:
                print(f"[CustomTracks] Query '{query}' failed: {e}")

        cands = glob.glob(str(temp_dir / f"temp_{filename}_raw.*"))
        if not cands:
            print(f"[CustomTracks] Warning: Failed to download source audio for {filename}")
            continue

        raw_downloaded = cands[0]
        print(f"[CustomTracks] Processing with FFmpeg (skip 12s, duration 65s, anti-content-ID filter): {raw_downloaded}")

        # Skip first 12s, duration 65s, anti-content-ID filter
        filter_str = "asetrate=48000*0.96,aresample=48000,atempo=1.04,lowpass=f=4500,highpass=f=60"
        cmd = [
            ffmpeg_exe,
            "-y",
            "-ss", "12",
            "-t", "65",
            "-i", raw_downloaded,
            "-af", filter_str,
            "-ar", "48000",
            "-ac", "2",
            "-c:a", "pcm_s16le",
            str(final_path),
        ]

        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if p.returncode != 0:
            print(f"[CustomTracks] FFmpeg error for {filename}: {p.stderr}")
        else:
            print(f"[CustomTracks] Successfully created {final_path.name} ({final_path.stat().st_size} bytes)!")
            prepared_paths.append(str(final_path))

        try:
            os.remove(raw_downloaded)
        except Exception:
            pass

    return prepared_paths


if __name__ == "__main__":
    download_and_prepare_custom_tracks()
