"""
Self-test suite for Clipwa.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

from .config import Config
from .pipeline import ClipwaPipeline


def generate_synthetic_video(output_mp4: str, duration: int = 35) -> str:
    """Generate a 16:9 test video with visual graphics and synthesized beep/audio."""
    fps = 30
    width, height = 1280, 720
    temp_raw_video = str(Config.TEMP_DIR / "temp_raw_video.mp4")
    temp_raw_audio = str(Config.TEMP_DIR / "temp_raw_audio.wav")

    # 1. Create video frames
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(temp_raw_video, fourcc, fps, (width, height))

    total_frames = fps * duration
    for f in range(total_frames):
        t = f / fps
        frame = np.zeros((height, width, 3), dtype=np.uint8)

        # Dynamic background gradient
        color_val = int(127 + 127 * np.sin(t * 2))
        frame[:] = (color_val // 4, 30, color_val // 2)

        # Draw a synthetic "speaker" face circle in center
        face_x = int(width / 2 + 60 * np.sin(t * 1.5))
        face_y = int(height / 2)
        cv2.circle(frame, (face_x, face_y), 90, (230, 200, 175), -1)
        # Eyes
        cv2.circle(frame, (face_x - 30, face_y - 20), 12, (50, 40, 30), -1)
        cv2.circle(frame, (face_x + 30, face_y - 20), 12, (50, 40, 30), -1)
        # Mouth moving
        mouth_open = int(10 + 8 * np.sin(t * 10))
        cv2.ellipse(frame, (face_x, face_y + 40), (25, mouth_open), 0, 0, 180, (40, 30, 120), -1)

        # On-screen text
        cv2.putText(
            frame,
            f"CLIPWA TEST STREAM - {t:.1f}s",
            (50, 80),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (0, 255, 255),
            2,
        )

        out.write(frame)

    out.release()

    # 2. Generate a test audio track using FFmpeg lavfi sine tone
    cmd_audio = [
        Config.FFMPEG_PATH,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration}",
        "-c:a",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        temp_raw_audio,
    ]
    subprocess.run(cmd_audio, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

    # 3. Mux video and audio into final test MP4
    cmd_mux = [
        Config.FFMPEG_PATH,
        "-y",
        "-i",
        temp_raw_video,
        "-i",
        temp_raw_audio,
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        "-shortest",
        output_mp4,
    ]
    subprocess.run(cmd_mux, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

    if os.path.exists(temp_raw_video):
        try: os.remove(temp_raw_video)
        except Exception: pass
    if os.path.exists(temp_raw_audio):
        try: os.remove(temp_raw_audio)
        except Exception: pass

    return output_mp4


def run_synthetic_test():
    """Execute end-to-end synthetic verification."""
    print("=" * 60)
    print("  CLIPWA END-TO-END SYNTHETIC VERIFICATION TEST")
    print("=" * 60)

    test_video_path = str(Config.TEMP_DIR / "synthetic_test_video.mp4")
    print(f"\n[1/4] Generating synthetic 35-second test video at:\n      {test_video_path}...")
    generate_synthetic_video(test_video_path, duration=35)
    print("      Synthetic test video created successfully.")

    print("\n[2/4] Initializing Clipwa Pipeline...")
    pipeline = ClipwaPipeline(
        llm_provider="heuristic",  # Use heuristic to run 100% offline
        whisper_model="tiny",
    )

    print("\n[3/4] Processing synthetic video through Clipwa pipeline...")
    result = pipeline.process_video(
        url_or_path=test_video_path,
        max_clips=1,
        min_clip_duration=25.0,
        max_clip_duration=35.0,
        layout_mode="auto",
        caption_theme="hormozi_yellow",
        upload_to_youtube=True,
        dry_run_upload=True,
    )

    print("\n[4/4] Verifying pipeline outputs...")
    clips = result.get("clips", [])
    if not clips:
        raise RuntimeError("Test Failed: No clips produced by pipeline!")

    first_clip = clips[0]
    clip_file = first_clip["file_path"]
    if not os.path.exists(clip_file) or os.path.getsize(clip_file) == 0:
        raise RuntimeError(f"Test Failed: Clip file missing or empty: {clip_file}")

    # Verify video resolution with cv2
    cap = cv2.VideoCapture(clip_file)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()

    print(f"\n Output Short Details:")
    print(f"      - Path: {clip_file}")
    print(f"      - Dimensions: {w}x{h} (Target: 1080x1920)")
    print(f"      - Duration: {first_clip['duration']}s")
    print(f"      - Virality Score: {first_clip['virality_score']}%")
    print(f"      - Title: {first_clip['youtube_title']}")
    print(f"      - Dry-run YouTube Upload: {first_clip['upload_result']['status']}")

    assert (w, h) == (1080, 1920), f"Expected 1080x1920 vertical format, got {w}x{h}"
    assert first_clip["upload_result"]["status"] == "dry_run_success"

    print("\n" + "=" * 60)
    print("  ALL VERIFICATION CHECKS PASSED 100% SUCCESSFULLY!")
    print("=" * 60)
    return True
