"""
High-quality media downloader with fast two-stage downloading (captions first, partial video later).
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import pysubs2
import yt_dlp
from yt_dlp.utils import download_range_func

from .config import Config


class SilentYDLLogger:
    """Silences stray external downloader warnings and ffmpeg exit messages from yt-dlp."""

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        # Silence HTTP 429 and common subtitle notices
        if "HTTP Error 429" in msg or "subtitles" in msg.lower() or "too many requests" in msg.lower():
            return

    def error(self, msg: str) -> None:
        # Ignore external ffmpeg control-c/exit code notices and HTTP 429 subtitle errors
        if (
            "3436169992" in msg
            or "ffmpeg exited with code" in msg
            or "HTTP Error 429" in msg
            or "too many requests" in msg.lower()
        ):
            return


class MediaDownloader:
    """Downloads source media, extracts subtitles/audio, and fetches targeted clip sections."""

    def __init__(self, temp_dir: Optional[Path] = None):
        self.temp_dir = temp_dir or Config.TEMP_DIR
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # Resolve exact ffmpeg path (prefer local bin/ffmpeg.exe)
        bin_dir = Path(Config.BIN_DIR)
        bin_dir.mkdir(parents=True, exist_ok=True)
        ffmpeg_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
        local_ffmpeg = bin_dir / ffmpeg_name

        if not local_ffmpeg.exists():
            try:
                import imageio_ffmpeg
                img_exe = imageio_ffmpeg.get_ffmpeg_exe()
                if img_exe and os.path.exists(img_exe):
                    shutil.copyfile(img_exe, str(local_ffmpeg))
            except Exception:
                pass

        if local_ffmpeg.exists():
            self.ffmpeg_path = str(local_ffmpeg.resolve())
            self.ffmpeg_dir = str(bin_dir.resolve())
        elif Config.FFMPEG_PATH and os.path.exists(Config.FFMPEG_PATH):
            self.ffmpeg_path = str(Path(Config.FFMPEG_PATH).resolve())
            self.ffmpeg_dir = str(Path(self.ffmpeg_path).parent.resolve())
        else:
            which_ff = shutil.which("ffmpeg")
            if which_ff:
                self.ffmpeg_path = str(Path(which_ff).resolve())
                self.ffmpeg_dir = str(Path(which_ff).parent.resolve())
            else:
                self.ffmpeg_path = "ffmpeg"
                self.ffmpeg_dir = str(bin_dir.resolve())

        self.ffprobe_path = Config.FFPROBE_PATH

        # Prepend ffmpeg directory to PATH for all subprocesses and yt-dlp internal checks
        current_path = os.environ.get("PATH", "")
        path_parts = current_path.split(os.pathsep)
        if self.ffmpeg_dir not in path_parts:
            os.environ["PATH"] = f"{self.ffmpeg_dir}{os.pathsep}{current_path}"

    def _ensure_cookies_file(self) -> Optional[str]:
        """
        Export authenticated cookies from Playwright's persistent profile if available.
        Ensures yt-dlp has signed-in authorization to bypass bot detection.
        """
        cookies_path = self.temp_dir / "cookies.txt"
        profile_dir = Path(Config.BASE_DIR) / "youtube_auth_profile"
        if not profile_dir.exists():
            return str(cookies_path) if cookies_path.exists() else None

        # If cookies.txt is fresh (< 6 hours old) and non-empty, use it
        try:
            if cookies_path.exists() and (time.time() - cookies_path.stat().st_mtime < 21600) and cookies_path.stat().st_size > 500:
                return str(cookies_path)
        except Exception:
            pass

        try:
            from playwright.sync_api import sync_playwright
            # Unlock profile lockfiles if any
            for lf in ["SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"]:
                f = profile_dir / lf
                if f.exists():
                    try:
                        f.unlink()
                    except Exception:
                        pass
            with sync_playwright() as p:
                browser = p.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir.resolve()),
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                cookies = browser.cookies()
                browser.close()

            if cookies:
                with open(cookies_path, "w", encoding="utf-8") as f:
                    f.write("# Netscape HTTP Cookie File\n")
                    now = int(time.time())
                    for c in cookies:
                        domain = c.get("domain", "")
                        flag = "TRUE" if domain.startswith(".") else "FALSE"
                        path = c.get("path", "/")
                        secure = "TRUE" if c.get("secure") else "FALSE"
                        expires = int(c.get("expires", 0))
                        if expires <= 0:
                            expires = now + 86400 * 30
                        name = c.get("name", "")
                        value = c.get("value", "")
                        f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")
                return str(cookies_path)
        except Exception:
            pass

        return str(cookies_path) if cookies_path.exists() else None

    def _get_base_ydl_opts(self) -> Dict[str, Any]:
        """Base yt-dlp options configured with Node.js challenge solver and persistent cookies."""
        cookiefile = self._ensure_cookies_file()
        opts: Dict[str, Any] = {
            "ffmpeg_location": self.ffmpeg_path,
            "js_runtimes": {"node": {}},
            "remote_components": ["ejs:github"],
            "socket_timeout": 30,
            "retries": 20,
            "fragment_retries": 20,
        }
        if cookiefile and os.path.exists(cookiefile):
            opts["cookiefile"] = cookiefile
        return opts

    def _has_valid_video(self, file_path: str) -> bool:
        """Verify that a media file exists, has size > 1000, and contains a readable video stream with width > 0 and height > 0."""
        if not file_path or not os.path.exists(file_path):
            return False
        try:
            if os.path.getsize(file_path) < 1000:
                return False
        except Exception:
            return False

        # 1. Check with OpenCV VideoCapture
        try:
            cap = cv2.VideoCapture(str(file_path))
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            cap.release()
            if w > 0 and h > 0 and fps > 0:
                return True
        except Exception:
            pass

        # 2. Check with FFmpeg/FFprobe
        try:
            cmd = [self.ffmpeg_path, "-i", str(file_path)]
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
            if "Video:" in p.stderr:
                return True
        except Exception:
            pass

        return False

    @staticmethod
    def sanitize_filename(name: str) -> str:
        """Sanitize string for safe filesystem usage."""
        cleaned = re.sub(r'[\\/*?:"<>|]', "", name)
        cleaned = re.sub(r"\s+", "_", cleaned).strip("._")
        return cleaned[:80] or "source_video"

    def get_info(self, url_or_path: str) -> Dict[str, Any]:
        """Extract metadata from URL or local file."""
        if os.path.exists(url_or_path):
            cap = cv2.VideoCapture(url_or_path)
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1920)
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1080)
            duration = total_frames / fps if fps > 0 else 0
            cap.release()

            filename = Path(url_or_path).stem
            return {
                "id": filename,
                "title": filename.replace("_", " "),
                "duration": round(duration, 2),
                "width": width,
                "height": height,
                "fps": fps,
                "uploader": "Local File",
                "is_local": True,
                "filepath": os.path.abspath(url_or_path),
            }

        ydl_opts = {
            **self._get_base_ydl_opts(),
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "logger": SilentYDLLogger(),
            "skip_unavailable_fragments": True,
            "abort_on_error": False,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url_or_path, download=False)
            return {
                "id": info.get("id", "video"),
                "title": info.get("title", "Untitled Video"),
                "duration": round(info.get("duration", 0) or 0, 2),
                "width": info.get("width", 1920),
                "height": info.get("height", 1080),
                "fps": info.get("fps", 30),
                "uploader": info.get("uploader", "Unknown"),
                "thumbnail": info.get("thumbnail"),
                "description": info.get("description", ""),
                "is_local": False,
                "filepath": None,
            }

    def fetch_fast_transcript_or_audio(
        self,
        url_or_path: str,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], Dict[str, Any]]:
        """
        Stage 1: Fast discovery pass.
        1. If local file: extracts audio and returns (None, audio_path, info).
        2. If remote URL: attempts to download existing subtitles / auto-captions in < 3s.
        3. If subtitles exist: parses directly and returns (transcript_data, None, info).
        4. If no subtitles: downloads audio-only at low bitrate and returns (None, audio_path, info).
        """
        info = self.get_info(url_or_path)
        base_name = self.sanitize_filename(info["title"])

        if info["is_local"]:
            video_path = info["filepath"]
            audio_path = str(self.temp_dir / f"{base_name}_16k.wav")
            self.extract_audio(video_path, audio_path)
            return None, audio_path, info

        # Remote URL: Try downloading subtitles/auto-captions first
        if progress_callback:
            progress_callback({"stage": "discovery", "message": "Checking for YouTube captions/subtitles..."})

        sub_prefix = str(self.temp_dir / f"sub_{info['id']}")
        # Remove any stale subtitle files
        for old_f in glob.glob(f"{sub_prefix}*"):
            try:
                os.remove(old_f)
            except Exception:
                pass

        sub_opts = {
            **self._get_base_ydl_opts(),
            "skip_download": True,
            "writeautomaticsub": True,
            "writesubtitles": True,
            "subtitleslangs": ["en", "en-orig", "en-US", "en-GB"],
            "subtitlesformat": "json3/vtt/srt",
            "outtmpl": f"{sub_prefix}.%(ext)s",
            "logger": SilentYDLLogger(),
            "socket_timeout": 15,
            "retries": 1,
            "fragment_retries": 1,
            "extractor_retries": 1,
            "skip_unavailable_fragments": True,
            "ignoreerrors": True,
            "abort_on_error": False,
            "quiet": True,
            "no_warnings": True,
        }

        try:
            with yt_dlp.YoutubeDL(sub_opts) as ydl:
                ydl.download([url_or_path])
        except Exception:
            pass

        # Check if subtitle file was downloaded
        found_subs = glob.glob(f"{sub_prefix}*.json3") or glob.glob(f"{sub_prefix}*.vtt") or glob.glob(f"{sub_prefix}*.srt")
        if found_subs:
            best_sub = found_subs[0]
            # Prioritize json3 for exact word offsets
            for s in found_subs:
                if s.endswith(".json3"):
                    best_sub = s
                    break

            try:
                transcript_data = self._parse_subtitle_file(best_sub)
                if transcript_data and transcript_data.get("segments"):
                    if progress_callback:
                        progress_callback({
                            "stage": "discovery",
                            "message": f"Successfully parsed {len(transcript_data['segments'])} caption segments from YouTube!",
                        })
                    return transcript_data, None, info
            except Exception as e:
                print(f"[MediaDownloader] Error parsing subtitles ({e}), falling back to audio download...")

        # Fallback: Download audio-only at low bitrate
        if progress_callback:
            progress_callback({"stage": "download_audio", "message": "Downloading audio for Whisper..."})

        audio_output_template = str(self.temp_dir / f"discovery_audio_{info['id']}.%(ext)s")

        def hook(d: Dict[str, Any]) -> None:
            if progress_callback and d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
                downloaded = d.get("downloaded_bytes") or 0
                pct = (downloaded / total) * 100
                progress_callback({
                    "stage": "download_progress",
                    "percent": round(pct, 1),
                    "speed": d.get("speed", 0),
                    "eta": d.get("eta", 0),
                    "message": f"Downloading audio: {pct:.1f}%",
                })

        audio_opts = {
            **self._get_base_ydl_opts(),
            "format": "ba[abr<=64]/ba[ext=m4a]/ba/b",
            "outtmpl": audio_output_template,
            "concurrent_fragment_downloads": 8,
            "logger": SilentYDLLogger(),
            "external_downloader": {"default": "native"},
            "skip_unavailable_fragments": True,
            "abort_on_error": False,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [hook] if progress_callback else [],
        }

        with yt_dlp.YoutubeDL(audio_opts) as ydl:
            download_info = ydl.extract_info(url_or_path, download=True)
            downloaded_audio = ydl.prepare_filename(download_info)

        wav_path = str(self.temp_dir / f"discovery_speech_{info['id']}_16k.wav")
        self.extract_audio(downloaded_audio, wav_path)
        return None, wav_path, info

    def download_clip_section(
        self,
        url_or_path: str,
        start_time: float,
        end_time: float,
        output_path: str,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> str:
        """
        Stage 2: Download ONLY the specific 30-60s viral segment using yt-dlp partial ranges.
        Never invokes FFmpeg over the network. FFmpeg is only used on local disk files.
        If local file, uses FFmpeg to cut the section directly.
        """
        dur = max(1.0, end_time - start_time)

        # If local file, use FFmpeg fast stream copy or re-encode
        if os.path.exists(url_or_path):
            cmd = [
                self.ffmpeg_path,
                "-y",
                "-ss",
                f"{start_time:.3f}",
                "-t",
                f"{dur:.3f}",
                "-i",
                url_or_path,
                "-c",
                "copy",
                output_path,
            ]
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            if self._has_valid_video(output_path):
                return output_path
            # If stream copy failed or produced no video, re-encode cleanly
            cmd = [
                self.ffmpeg_path,
                "-y",
                "-ss",
                f"{start_time:.3f}",
                "-i",
                url_or_path,
                "-t",
                f"{dur:.3f}",
                "-c:v",
                "libx264",
                "-preset",
                "superfast",
                "-c:a",
                "aac",
                output_path,
            ]
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            return output_path

        def hook(d: Dict[str, Any]) -> None:
            if progress_callback and d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
                downloaded = d.get("downloaded_bytes") or 0
                pct = (downloaded / total) * 100
                progress_callback({
                    "stage": "download_progress",
                    "percent": round(pct, 1),
                    "speed": d.get("speed", 0),
                    "eta": d.get("eta", 0),
                    "message": f"Downloading clip section: {pct:.1f}%",
                })

        # Primary Method: 100% Native Python yt-dlp Fragment/Byte-Range Download
        # Format selector strictly requires video (never allow audio-only formats!)
        primary_format = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080][ext=mp4]/b[vcodec!=none]"

        buffer_start = max(0.0, start_time - 2.0)
        buffer_end = end_time + 2.0
        ranges = [(buffer_start, buffer_end)]
        offset = max(0.0, start_time - buffer_start)

        temp_base = str(self.temp_dir / f"raw_download_{int(start_time)}_{int(end_time)}")
        for old_f in glob.glob(f"{temp_base}*"):
            try:
                os.remove(old_f)
            except Exception:
                pass

        primary_success = False
        downloaded_raw = None

        try:
            ydl_opts = {
                **self._get_base_ydl_opts(),
                "format": primary_format,
                "outtmpl": f"{temp_base}.%(ext)s",
                "merge_output_format": "mp4",
                "download_ranges": download_range_func(None, ranges),
                "force_keyframes_at_cuts": False,
                "concurrent_fragment_downloads": 8,
                "logger": SilentYDLLogger(),
                "external_downloader": {"default": "native"},
                "skip_unavailable_fragments": True,
                "abort_on_error": False,
                "overwrites": True,
                "quiet": True,
                "no_warnings": True,
                "progress_hooks": [hook] if progress_callback else [],
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url_or_path])

            cands = glob.glob(f"{temp_base}.*")
            if cands:
                for cand in cands:
                    if cand.endswith((".mp4", ".mkv", ".webm", ".ts")):
                        if self._has_valid_video(cand):
                            downloaded_raw = cand
                            primary_success = True
                            break
        except Exception as e:
            if progress_callback:
                progress_callback({
                    "stage": "download_notice",
                    "message": f"Section range download notice ({e}). Switching to progressive fallback...",
                })
            print(f"[MediaDownloader] Range download encounter: {e}. Switching to progressive stream fallback...")
            primary_success = False

        if primary_success and downloaded_raw and self._has_valid_video(downloaded_raw):
            try:
                raw_dur = self._get_media_duration(downloaded_raw)
                if raw_dur > dur + 0.5:
                    seek_time = min(offset, max(0.0, raw_dur - dur))
                else:
                    seek_time = 0.0

                trim_cmd = [
                    self.ffmpeg_path,
                    "-y",
                    "-ss",
                    f"{seek_time:.3f}",
                    "-i",
                    downloaded_raw,
                    "-t",
                    f"{dur:.3f}",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "superfast",
                    "-c:a",
                    "aac",
                    "-movflags",
                    "+faststart",
                    output_path,
                ]
                subprocess.run(trim_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
                if self._has_valid_video(output_path):
                    try:
                        os.remove(downloaded_raw)
                    except Exception:
                        pass
                    return output_path
                else:
                    print("[MediaDownloader] Primary trimmed output has no valid video stream. Switching to progressive fallback...")
            except Exception as trim_err:
                print(f"[MediaDownloader] Primary FFmpeg trim error: {trim_err}. Switching to progressive fallback...")
            finally:
                if downloaded_raw and os.path.exists(downloaded_raw):
                    try:
                        os.remove(downloaded_raw)
                    except Exception:
                        pass

        # Fallback Method: Progressive MP4 download (format='18/b[ext=mp4]/b[vcodec!=none]') + FFmpeg slice
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass

        fallback_base = str(self.temp_dir / f"fallback_stream_{int(start_time)}_{int(end_time)}")
        for old_f in glob.glob(f"{fallback_base}*"):
            try:
                os.remove(old_f)
            except Exception:
                pass

        fallback_opts = {
            **self._get_base_ydl_opts(),
            "format": "18/b[ext=mp4]/b[vcodec!=none]/bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080][ext=mp4]/b[vcodec!=none]",
            "outtmpl": f"{fallback_base}.%(ext)s",
            "merge_output_format": "mp4",
            "abort_on_error": False,
            "external_downloader": {"default": "native"},
            "logger": SilentYDLLogger(),
            "skip_unavailable_fragments": True,
            "overwrites": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [hook] if progress_callback else [],
        }

        try:
            with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                ydl.download([url_or_path])
        except Exception as fb_err:
            print(f"[MediaDownloader] Progressive stream fallback notice ({fb_err}). Retrying with best available video format...")
            fallback_opts["format"] = "bestvideo[height<=1080]+bestaudio/bestvideo+bestaudio"
            with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                ydl.download([url_or_path])

        fb_cands = glob.glob(f"{fallback_base}.*")
        downloaded_fb = None
        for cand in fb_cands:
            if cand.endswith((".mp4", ".mkv", ".webm", ".ts")):
                if self._has_valid_video(cand):
                    downloaded_fb = cand
                    break

        if not downloaded_fb or not os.path.exists(downloaded_fb):
            raise RuntimeError(f"Failed to download valid video stream for {url_or_path}")

        # Local trim with FFmpeg on the local file
        trim_cmd = [
            self.ffmpeg_path,
            "-y",
            "-ss",
            f"{start_time:.3f}",
            "-i",
            downloaded_fb,
            "-t",
            f"{dur:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "superfast",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            output_path,
        ]
        subprocess.run(trim_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        try:
            os.remove(downloaded_fb)
        except Exception:
            pass

        if not self._has_valid_video(output_path):
            raise RuntimeError(f"FFmpeg trim completed but output file '{output_path}' contains no valid video stream.")

        return output_path

    def _get_media_duration(self, filepath: str) -> float:
        """Probe media duration in seconds using ffprobe."""
        try:
            cmd = [
                self.ffprobe_path or "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                filepath,
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            return float(res.stdout.strip())
        except Exception:
            return 0.0

    def _parse_subtitle_file(self, sub_path: str) -> Dict[str, Any]:
        """Parse YouTube json3 or VTT/SRT subtitles into standard transcript data."""
        if sub_path.endswith(".json3"):
            with open(sub_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            events = data.get("events", [])
            segments = []
            all_words = []
            full_text_parts = []
            formatted_lines = []

            for ev in events:
                if "segs" not in ev:
                    continue
                t_start = ev.get("tStartMs", 0) / 1000.0
                t_dur = ev.get("dDurationMs", 0) / 1000.0
                t_end = t_start + t_dur
                text = "".join(s.get("utf8", "") for s in ev["segs"]).strip()
                if not text or text == "\n":
                    continue

                seg_words = []
                for s in ev["segs"]:
                    w_text = s.get("utf8", "").strip()
                    if not w_text:
                        continue
                    offset = s.get("tOffsetMs", 0) / 1000.0
                    w_start = t_start + offset
                    w_end = min(t_end, w_start + 0.35)
                    w_dict = {
                        "word": w_text,
                        "start": round(w_start, 3),
                        "end": round(w_end, 3),
                        "probability": 1.0,
                    }
                    seg_words.append(w_dict)
                    all_words.append(w_dict)

                segments.append({
                    "id": len(segments),
                    "start": round(t_start, 3),
                    "end": round(t_end, 3),
                    "text": text,
                    "words": seg_words,
                })
                full_text_parts.append(text)

                mins, secs = divmod(t_start, 60)
                hours, mins = divmod(mins, 60)
                fmt_t = f"{int(hours):02d}:{int(mins):02d}:{secs:04.1f}" if hours > 0 else f"{int(mins):02d}:{secs:04.1f}"
                formatted_lines.append(f"[{fmt_t}] {text}")

            return {
                "language": "en",
                "language_probability": 1.0,
                "duration": round(segments[-1]["end"] if segments else 0, 2),
                "full_text": " ".join(full_text_parts),
                "formatted_transcript": "\n".join(formatted_lines),
                "segments": segments,
                "words": all_words,
            }

        # VTT / SRT fallback via pysubs2
        subs = pysubs2.load(sub_path)
        segments = []
        all_words = []
        full_text_parts = []
        formatted_lines = []

        for i, ev in enumerate(subs.events):
            t_start = ev.start / 1000.0
            t_end = ev.end / 1000.0
            text = ev.text.replace("\\N", " ").strip()
            if not text:
                continue

            words = text.split()
            seg_words = []
            dur_per_word = (t_end - t_start) / max(1, len(words))
            for j, w in enumerate(words):
                w_start = t_start + j * dur_per_word
                w_end = w_start + dur_per_word
                w_dict = {
                    "word": w,
                    "start": round(w_start, 3),
                    "end": round(w_end, 3),
                    "probability": 1.0,
                }
                seg_words.append(w_dict)
                all_words.append(w_dict)

            segments.append({
                "id": i,
                "start": round(t_start, 3),
                "end": round(t_end, 3),
                "text": text,
                "words": seg_words,
            })
            full_text_parts.append(text)

            mins, secs = divmod(t_start, 60)
            formatted_lines.append(f"[{int(mins):02d}:{secs:04.1f}] {text}")

        return {
            "language": "en",
            "language_probability": 1.0,
            "duration": round(segments[-1]["end"] if segments else 0, 2),
            "full_text": " ".join(full_text_parts),
            "formatted_transcript": "\n".join(formatted_lines),
            "segments": segments,
            "words": all_words,
        }

    def download(
        self,
        url_or_path: str,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Tuple[str, str, Dict[str, Any]]:
        """
        Backwards-compatible full download method (used by test suite and local files).
        """
        info = self.get_info(url_or_path)
        base_name = self.sanitize_filename(info["title"])

        if info["is_local"]:
            video_path = info["filepath"]
            audio_path = str(self.temp_dir / f"{base_name}_16k.wav")
            self.extract_audio(video_path, audio_path)
            return video_path, audio_path, info

        video_output_template = str(self.temp_dir / f"{base_name}.%(ext)s")

        def hook(d: Dict[str, Any]) -> None:
            if progress_callback and d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
                downloaded = d.get("downloaded_bytes") or 0
                pct = (downloaded / total) * 100
                progress_callback({
                    "stage": "download_progress",
                    "percent": round(pct, 1),
                    "speed": d.get("speed", 0),
                    "eta": d.get("eta", 0),
                })

        ydl_opts = {
            **self._get_base_ydl_opts(),
            "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]/best",
            "outtmpl": video_output_template,
            "merge_output_format": "mp4",
            "abort_on_error": False,
            "logger": SilentYDLLogger(),
            "concurrent_fragment_downloads": 8,
            "skip_unavailable_fragments": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [hook] if progress_callback else [],
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            download_info = ydl.extract_info(url_or_path, download=True)
            video_path = ydl.prepare_filename(download_info)
            if not video_path.endswith(".mp4"):
                video_path = os.path.splitext(video_path)[0] + ".mp4"

        audio_path = str(self.temp_dir / f"{base_name}_16k.wav")
        self.extract_audio(video_path, audio_path)
        return video_path, audio_path, info

    def extract_audio(self, video_path: str, output_audio_path: str) -> str:
        """Extract 16kHz mono audio optimized for Whisper speech-to-text."""
        if os.path.exists(output_audio_path) and os.path.getsize(output_audio_path) > 1000:
            return output_audio_path

        cmd = [
            self.ffmpeg_path,
            "-y",
            "-i",
            video_path,
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            output_audio_path,
        ]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"FFmpeg audio extraction failed: {p.stderr}")

        return output_audio_path
