"""
High-quality media downloader and preprocessor using yt-dlp and FFmpeg.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import cv2
import yt_dlp

from .config import Config


class MediaDownloader:
    """Downloads source video and extracts audio for downstream processing."""

    def __init__(self, temp_dir: Optional[Path] = None):
        self.temp_dir = temp_dir or Config.TEMP_DIR
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.ffmpeg_path = Config.FFMPEG_PATH

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
                "duration": duration,
                "width": width,
                "height": height,
                "fps": fps,
                "uploader": "Local File",
                "is_local": True,
                "filepath": os.path.abspath(url_or_path),
            }

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "ffmpeg_location": self.ffmpeg_path,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url_or_path, download=False)
            return {
                "id": info.get("id", "video"),
                "title": info.get("title", "Untitled Video"),
                "duration": info.get("duration", 0),
                "width": info.get("width", 1920),
                "height": info.get("height", 1080),
                "fps": info.get("fps", 30),
                "uploader": info.get("uploader", "Unknown"),
                "thumbnail": info.get("thumbnail"),
                "description": info.get("description", ""),
                "is_local": False,
                "filepath": None,
            }

    def download(
        self,
        url_or_path: str,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Tuple[str, str, Dict[str, Any]]:
        """
        Download media and extract 16kHz mono audio.
        Returns: (video_path, audio_path, metadata)
        """
        info = self.get_info(url_or_path)
        base_name = self.sanitize_filename(info["title"])

        if info["is_local"]:
            video_path = info["filepath"]
            audio_path = str(self.temp_dir / f"{base_name}_16k.wav")
            self.extract_audio(video_path, audio_path)
            return video_path, audio_path, info

        # Download from URL
        video_output_template = str(self.temp_dir / f"{base_name}.%(ext)s")

        def hook(d: Dict[str, Any]) -> None:
            if progress_callback and d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
                downloaded = d.get("downloaded_bytes") or 0
                pct = (downloaded / total) * 100
                progress_callback({
                    "stage": "downloading",
                    "percent": round(pct, 1),
                    "speed": d.get("speed", 0),
                    "eta": d.get("eta", 0),
                })

        ydl_opts = {
            "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]/best",
            "outtmpl": video_output_template,
            "merge_output_format": "mp4",
            "ffmpeg_location": self.ffmpeg_path,
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
