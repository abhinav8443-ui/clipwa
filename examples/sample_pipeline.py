"""
Example Python script demonstrating programmatic usage of Clipwa.
"""

import os
import sys
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from clipwa import ClipwaPipeline


def main():
    # Set Gemini API key (optional - falls back to heuristic if not set)
    # os.environ["GEMINI_API_KEY"] = "your-gemini-api-key"

    print("Initializing Clipwa Pipeline...")
    pipeline = ClipwaPipeline(
        llm_provider="gemini",     # "gemini", "groq", "ollama", or "heuristic"
        whisper_model="tiny",      # "tiny", "base", "small", "medium"
    )

    # You can supply any YouTube URL, Twitch clip, podcast feed, or local video path:
    # target_video = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    # Or a local test video file:
    target_video = str(Path(__file__).parent / "test_synthetic_video.mp4")

    if not os.path.exists(target_video):
        print("Run examples/test_synthetic_video.py first to generate a local sample video, or pass a YouTube URL!")
        return

    print(f"Processing: {target_video}...")
    result = pipeline.process_video(
        url_or_path=target_video,
        max_clips=2,
        min_clip_duration=25.0,
        max_clip_duration=60.0,
        layout_mode="auto",            # "auto", "face_track", "blurred_stack"
        caption_theme="hormozi_yellow",# "hormozi_yellow", "neon_cyan", etc.
        words_per_line=3,
        upload_to_youtube=False,       # Set True to upload automatically
        youtube_privacy="private",
    )

    print("\nProcessing completed!")
    print(f"Generated {len(result['clips'])} clips in {result['elapsed_seconds']}s:")
    for clip in result["clips"]:
        print(f" - [{clip['virality_score']}%] {clip['youtube_title']}")
        print(f"   Path: {clip['file_path']}")


if __name__ == "__main__":
    main()
