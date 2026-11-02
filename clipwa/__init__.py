"""
Clipwa - 100% Free & Open-Source Automated Viral Clipping & YouTube Shorts Pipeline.
"""

__version__ = "1.0.0"
__author__ = "Clipwa Team"

from .config import Config
from .downloader import MediaDownloader
from .transcriber import LocalTranscriber
from .ai_brain import ViralMomentDetector
from .video_editor import VideoEditor
from .subtitle_generator import SubtitleGenerator
from .uploader import YouTubeShortsUploader
from .insta_uploader import InstagramReelsUploader
from .pipeline import ClipwaPipeline

__all__ = [
    "Config",
    "MediaDownloader",
    "LocalTranscriber",
    "ViralMomentDetector",
    "VideoEditor",
    "SubtitleGenerator",
    "YouTubeShortsUploader",
    "InstagramReelsUploader",
    "ClipwaPipeline",
]
