"""
Setup helper to download and process viral background instrumentals from both YouTube and Instagram:
1. track_1_4_raws.wav (EsDeeKid - 4 Raws)
2. track_2_if_we_being_real.wav (Yeat - If We Being Real)
3. track_3_jmsn_love_me.wav (JMSN - Love Me)
4. track_4_sao_paulo.wav (The Weeknd - São Paulo)
5. track_5_love_me_not.wav (Ravyn Lenae - Love Me Not)
6. track_6_insta_viral_ref1.wav (Instagram Viral Reel - DfXmsP9BXth)
7. track_7_insta_viral_ref2.wav (Instagram Viral Reel - DbudQ6TR8IH)
"""

import glob
import os
import subprocess
import time
from pathlib import Path
from typing import List, Optional
import yt_dlp

from .config import Config


def download_and_prepare_insta_tracks(force: bool = False) -> List[str]:
    """
    Downloads and extracts viral audio from reference Instagram Reels into assets/audio/custom_tracks/.
    Ensures minimum 65s length (via seamless loop if needed) and anti-content-ID EQ/pitch filtering.
    """
    custom_tracks_dir = Config.BASE_DIR / "assets" / "audio" / "custom_tracks"
    custom_tracks_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = Config.TEMP_DIR
    temp_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_exe = str(Config.BIN_DIR / "ffmpeg.exe") if (Config.BIN_DIR / "ffmpeg.exe").exists() else Config.FFMPEG_PATH

    insta_tracks = [
        ("track_6_insta_viral_ref1.wav", "https://www.instagram.com/reel/DfXmsP9BXth/"),
        ("track_7_insta_viral_ref2.wav", "https://www.instagram.com/reel/DbudQ6TR8IH/"),
    ]

    prepared = []
    filter_str = "asetrate=48000*0.96,aresample=48000,atempo=1.04,lowpass=f=4500,highpass=f=60"

    for filename, reel_url in insta_tracks:
        final_path = custom_tracks_dir / filename
        if final_path.exists() and final_path.stat().st_size > 100000 and not force:
            print(f"[CustomTracks/Insta] Already exists: {final_path.name} ({final_path.stat().st_size} bytes)")
            prepared.append(str(final_path))
            continue

        print(f"\n[CustomTracks/Insta] Fetching viral audio from Instagram: {filename} ({reel_url})...")
        temp_raw = temp_dir / f"temp_{filename}_raw.mp4"
        if temp_raw.exists():
            try:
                temp_raw.unlink()
            except Exception:
                pass

        # Download reel
        try:
            ydl_opts = {
                "format": "ba/b/best",
                "outtmpl": str(temp_raw),
                "ffmpeg_location": ffmpeg_exe,
                "quiet": True,
                "no_warnings": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([reel_url])
        except Exception as e:
            print(f"[CustomTracks/Insta] yt-dlp notice for {reel_url}: {e}")

        # If downloaded, process with ffmpeg
        cands = list(temp_dir.glob(f"temp_{filename}_raw*"))
        if not cands or cands[0].stat().st_size < 10000:
            # Check if reference video exists in temp/style_ref/
            ref_fallback = temp_dir / "style_ref" / ("ref_1.mp4" if "ref1" in filename else "ref_2.mp4")
            if ref_fallback.exists():
                src_file = ref_fallback
            else:
                print(f"[CustomTracks/Insta] Warning: Could not download audio for {filename}")
                continue
        else:
            src_file = cands[0]

        # Convert to 48kHz stereo WAV with anti-content-ID filtering, looping if < 65s
        cmd = [
            ffmpeg_exe, "-y",
            "-stream_loop", "-1",
            "-i", str(src_file),
            "-t", "68",
            "-af", filter_str,
            "-ar", "48000",
            "-ac", "2",
            "-c:a", "pcm_s16le",
            str(final_path),
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0 and final_path.exists():
            print(f"[CustomTracks/Insta] Successfully created {final_path.name} ({final_path.stat().st_size} bytes)!")
            prepared.append(str(final_path))
        else:
            print(f"[CustomTracks/Insta] FFmpeg error: {res.stderr}")

        try:
            if src_file != ref_fallback and src_file.exists():
                src_file.unlink()
        except Exception:
            pass

    return prepared


def download_and_prepare_custom_tracks(force: bool = False) -> list[str]:
    """
    Downloads and prepares viral background music tracks into assets/audio/custom_tracks/
    from both YouTube and Instagram.
    """
    custom_tracks_dir = Config.BASE_DIR / "assets" / "audio" / "custom_tracks"
    custom_tracks_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = Config.TEMP_DIR
    temp_dir.mkdir(parents=True, exist_ok=True)

    tracks_to_fetch = [
        ("track_1_4_raws.wav", "ytsearch1:4 raws instrumental slowed edit", "scsearch1:4 raws instrumental slowed"),
        ("track_2_if_we_being_real.wav", "ytsearch1:yeat if we being real instrumental slowed", "scsearch1:yeat if we being real instrumental slowed"),
        ("track_3_jmsn_love_me.wav", "ytsearch1:JMSN Love Me instrumental slowed edit", "scsearch1:JMSN Love Me instrumental slowed"),
        ("track_4_sao_paulo.wav", "ytsearch1:The Weeknd Sao Paulo instrumental slowed edit", "scsearch1:The Weeknd Sao Paulo instrumental slowed"),
        ("track_5_love_me_not.wav", "ytsearch1:Ravyn Lenae Love Me Not instrumental slowed edit", "scsearch1:Ravyn Lenae Love Me Not instrumental slowed"),
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

        cookies_file = temp_dir / "cookies.txt"
        ydl_opts = {
            "format": "ba/b",
            "outtmpl": temp_out,
            "ffmpeg_location": ffmpeg_exe,
            "js_runtimes": {"node": {}},
            "remote_components": ["ejs:github"],
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
            "retries": 5,
        }
        if cookies_file.exists():
            ydl_opts["cookiefile"] = str(cookies_file)

        # Try YouTube search first, fall back to SoundCloud search if bot blocked
        download_success = False
        queries_to_try = [yt_query, sc_query]
        # Additional YouTube fallback query for JMSN
        if "jmsn" in filename:
            queries_to_try.insert(1, "ytsearch1:JMSN Love Me best part edit audio")
            queries_to_try.append("scsearch1:JMSN Love Me")

        for query in queries_to_try:
            try:
                print(f"[CustomTracks] Trying query: '{query}'...")
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([query])
                cands = glob.glob(str(temp_dir / f"temp_{filename}_raw.*"))
                if cands and os.path.getsize(cands[0]) > 50000:
                    download_success = True
                    break
            except Exception as e:
                print(f"[CustomTracks] Query '{query}' notice: {e}")

        cands = glob.glob(str(temp_dir / f"temp_{filename}_raw.*"))
        if not cands or os.path.getsize(cands[0]) <= 50000:
            print(f"[CustomTracks] Warning: Failed to download source audio for {filename}")
            continue

        raw_downloaded = cands[0]
        print(f"[CustomTracks] Processing with FFmpeg (skip 15s, duration 65s, anti-content-ID filter): {raw_downloaded}")

        # Skip first 15s, duration 65s, anti-content-ID filter
        filter_str = "asetrate=48000*0.96,aresample=48000,atempo=1.04,lowpass=f=4200,highpass=f=60"
        cmd = [
            ffmpeg_exe,
            "-y",
            "-ss", "15",
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

    # Also ensure Instagram viral tracks are fetched and prepared
    insta_prepared = download_and_prepare_insta_tracks(force=force)
    prepared_paths.extend(insta_prepared)

    return prepared_paths

    return prepared_paths


if __name__ == "__main__":
    download_and_prepare_custom_tracks()
