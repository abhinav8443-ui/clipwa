"""
Configuration and environment management for Clipwa.
"""

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Optional, Union

# Silence C++ logging from TensorFlow Lite, MediaPipe, and Abseil before other modules import them
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
os.environ["GLOG_minloglevel"] = "3"
os.environ["ABSL_MIN_LOG_LEVEL"] = "3"


def atomic_write_json(file_path: Union[str, Path], data: Any) -> None:
    """Safely and atomically writes JSON data with default=str to avoid corrupting files on crash."""
    p = Path(file_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = p.with_suffix(f"{p.suffix}.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp_path, p)


def _load_env_file(path: Path) -> None:
    """Safely parse and populate os.environ from a .env file without external dependencies."""
    if not path.is_file():
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = val
    except Exception:
        pass


class Config:
    """Clipwa central configuration."""

    # Project directories
    PACKAGE_DIR = Path(__file__).resolve().parent
    BASE_DIR = PACKAGE_DIR.parent
    BIN_DIR = BASE_DIR / "bin"
    MODELS_DIR = BASE_DIR / "models"
    TEMP_DIR = BASE_DIR / "temp"
    OUTPUT_DIR = BASE_DIR / "output"

    # Load .env files: first check project root, then check current working directory
    _load_env_file(BASE_DIR / ".env")
    _load_env_file(Path.cwd() / ".env")

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

    # Video Settings - YouTube Shorts 9:16 Vertical Standard (1080x1920)
    TARGET_WIDTH: int = 1080
    TARGET_HEIGHT: int = 1920
    DEFAULT_ASPECT_RATIO: str = "9:16"
    MIN_CLIP_DURATION: float = 35.0
    MAX_CLIP_DURATION: float = 58.0
    DEFAULT_MAX_CLIPS: int = 3

    # FFmpeg & FFprobe executables
    FFMPEG_PATH: str = ""
    FFPROBE_PATH: str = ""

    @classmethod
    def setup_environment(cls) -> None:
        """Ensure directories exist and locate/register FFmpeg binary."""
        cls.TEMP_DIR.mkdir(parents=True, exist_ok=True)
        cls.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        cls.MODELS_DIR.mkdir(parents=True, exist_ok=True)
        cls.BIN_DIR.mkdir(parents=True, exist_ok=True)

        # Set matplotlib cache directory to temp to prevent permission warnings
        os.environ["MPLCONFIGDIR"] = str(cls.TEMP_DIR)

        # Find FFmpeg & FFprobe executables
        cls.FFMPEG_PATH = cls._find_ffmpeg()
        cls.FFPROBE_PATH = cls._find_ffprobe()

        # Always prepend local bin directory to system PATH
        bin_dir_str = str(cls.BIN_DIR)
        current_path = os.environ.get("PATH", "")
        if bin_dir_str not in current_path.split(os.pathsep):
            os.environ["PATH"] = f"{bin_dir_str}{os.pathsep}{current_path}"

    @classmethod
    def _find_ffmpeg(cls) -> str:
        """Find the best available FFmpeg binary."""
        # 1. Check clipwa/bin directory (full build)
        local_bin_exe = cls.BIN_DIR / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
        if local_bin_exe.exists() and os.access(str(local_bin_exe), os.X_OK | os.R_OK):
            return str(local_bin_exe)

        # 2. Check system PATH
        which_ffmpeg = shutil.which("ffmpeg")
        if which_ffmpeg:
            return which_ffmpeg

        # 3. Check imageio_ffmpeg fallback
        try:
            import imageio_ffmpeg
            imgio_exe = imageio_ffmpeg.get_ffmpeg_exe()
            if imgio_exe and os.path.exists(imgio_exe):
                return imgio_exe
        except Exception:
            pass

        return "ffmpeg"

    @classmethod
    def _find_ffprobe(cls) -> str:
        """Find the best available FFprobe binary."""
        local_bin_exe = cls.BIN_DIR / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
        if local_bin_exe.exists() and os.access(str(local_bin_exe), os.X_OK | os.R_OK):
            return str(local_bin_exe)

        which_ffprobe = shutil.which("ffprobe")
        if which_ffprobe:
            return which_ffprobe

        return "ffprobe"


# Run setup on import
Config.setup_environment()
