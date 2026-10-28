"""
Configuration and environment management for Clipwa.
"""

import os
import shutil
import sys
from pathlib import Path
from typing import Optional


class Config:
    """Clipwa central configuration."""

    # Project directories
    PACKAGE_DIR = Path(__file__).resolve().parent
    BASE_DIR = PACKAGE_DIR.parent
    BIN_DIR = BASE_DIR / "bin"
    MODELS_DIR = BASE_DIR / "models"
    TEMP_DIR = BASE_DIR / "temp"
    OUTPUT_DIR = BASE_DIR / "output"

    # API Keys & Endpoints
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    DEFAULT_LLM_PROVIDER: str = os.getenv("DEFAULT_LLM_PROVIDER", "gemini")  # gemini, groq, ollama, heuristic
    DEFAULT_GEMINI_MODEL: str = os.getenv("DEFAULT_GEMINI_MODEL", "gemini-2.5-flash")

    # YouTube API
    YOUTUBE_CLIENT_SECRETS_FILE: str = os.getenv("YOUTUBE_CLIENT_SECRETS_FILE", str(BASE_DIR / "client_secrets.json"))
    YOUTUBE_TOKEN_FILE: str = os.getenv("YOUTUBE_TOKEN_FILE", str(BASE_DIR / "token.json"))

    # Whisper Settings
    WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "tiny")  # tiny, base, small, medium, large-v3
    WHISPER_DEVICE: str = os.getenv("WHISPER_DEVICE", "cpu")  # cpu or cuda
    WHISPER_COMPUTE_TYPE: str = os.getenv("WHISPER_COMPUTE_TYPE", "int8")  # int8, float16, float32

    # Video Settings
    TARGET_WIDTH: int = 1080
    TARGET_HEIGHT: int = 1920
    MIN_CLIP_DURATION: float = 25.0
    MAX_CLIP_DURATION: float = 60.0
    DEFAULT_MAX_CLIPS: int = 3

    # FFmpeg executable
    FFMPEG_PATH: str = ""

    @classmethod
    def setup_environment(cls) -> None:
        """Ensure directories exist and locate/register FFmpeg binary."""
        cls.TEMP_DIR.mkdir(parents=True, exist_ok=True)
        cls.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        cls.MODELS_DIR.mkdir(parents=True, exist_ok=True)
        cls.BIN_DIR.mkdir(parents=True, exist_ok=True)

        # Set matplotlib cache directory to temp to prevent permission warnings
        os.environ["MPLCONFIGDIR"] = str(cls.TEMP_DIR)

        # Find FFmpeg executable
        ffmpeg_bin = cls._find_ffmpeg()
        cls.FFMPEG_PATH = ffmpeg_bin

        # Make sure directory containing FFmpeg is in system PATH
        if ffmpeg_bin:
            ffmpeg_dir = str(Path(ffmpeg_bin).parent)
            current_path = os.environ.get("PATH", "")
            if ffmpeg_dir not in current_path:
                os.environ["PATH"] = f"{ffmpeg_dir}{os.pathsep}{current_path}"

    @classmethod
    def _find_ffmpeg(cls) -> str:
        """Find the best available FFmpeg binary."""
        # 1. Check clipwa/bin directory
        local_bin_exe = cls.BIN_DIR / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
        if local_bin_exe.exists() and os.access(str(local_bin_exe), os.X_OK | os.R_OK):
            return str(local_bin_exe)

        # 2. Check imageio_ffmpeg
        try:
            import imageio_ffmpeg
            imgio_exe = imageio_ffmpeg.get_ffmpeg_exe()
            if imgio_exe and os.path.exists(imgio_exe):
                return imgio_exe
        except Exception:
            pass

        # 3. Check system PATH
        which_ffmpeg = shutil.which("ffmpeg")
        if which_ffmpeg:
            return which_ffmpeg

        # Fallback to simple command name
        return "ffmpeg"


# Run setup on import
Config.setup_environment()
