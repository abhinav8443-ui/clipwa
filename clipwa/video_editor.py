"""
Video editing, smart 9:16 reframing, face tracking, and subtitle burning using FFmpeg, OpenCV, and MediaPipe.
"""

import math
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from .config import Config


class VideoEditor:
    """Handles smart cropping, face-tracking reframing, and final clip rendering."""

    def __init__(self, temp_dir: Optional[Path] = None, output_dir: Optional[Path] = None):
        self.temp_dir = temp_dir or Config.TEMP_DIR
        self.output_dir = output_dir or Config.OUTPUT_DIR
        self.ffmpeg_path = Config.FFMPEG_PATH
        self.models_dir = Config.MODELS_DIR
        self._mp_detector = None
        self._cv_cascade = None

    def _init_face_detectors(self) -> None:
        """Initialize MediaPipe and OpenCV face detectors."""
        # 1. MediaPipe Tasks FaceDetector
        mp_model_path = str(self.models_dir / "blaze_face_short_range.tflite")
        if os.path.exists(mp_model_path) and self._mp_detector is None:
            try:
                import mediapipe as mp
                from mediapipe.tasks import python
                from mediapipe.tasks.python import vision

                base_options = python.BaseOptions(model_asset_path=mp_model_path)
                options = vision.FaceDetectorOptions(base_options=base_options, min_detection_confidence=0.5)
                self._mp_detector = vision.FaceDetector.create_from_options(options)
            except Exception as e:
                print(f"[VideoEditor] Note: MediaPipe initialization notice: {e}")

        # 2. OpenCV Haar Cascade Fallback
        cascade_path = str(self.models_dir / "haarcascade_frontalface_default.xml")
        if os.path.exists(cascade_path) and self._cv_cascade is None:
            try:
                self._cv_cascade = cv2.CascadeClassifier(cascade_path)
            except Exception:
                pass

    def detect_face_in_frame(self, frame_bgr: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
        """
        Detect the primary face bounding box (x, y, w, h) in a frame.
        Returns largest detected face or None.
        """
        self._init_face_detectors()
        h, w = frame_bgr.shape[:2]

        # Try MediaPipe first
        if self._mp_detector is not None:
            try:
                import mediapipe as mp
                frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)
                results = self._mp_detector.detect(mp_image)
                if results and results.detections:
                    # Pick largest face
                    best_box = None
                    best_area = 0
                    for det in results.detections:
                        bb = det.bounding_box
                        x = max(0, bb.origin_x)
                        y = max(0, bb.origin_y)
                        bw = min(w - x, bb.width)
                        bh = min(h - y, bb.height)
                        area = bw * bh
                        if area > best_area:
                            best_area = area
                            best_box = (x, y, bw, bh)
                    if best_box:
                        return best_box
            except Exception:
                pass

        # Fallback to OpenCV Haar Cascade
        if self._cv_cascade is not None:
            try:
                gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
                faces = self._cv_cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=4, minSize=(60, 60))
                if len(faces) > 0:
                    # Largest face
                    best = max(faces, key=lambda f: f[2] * f[3])
                    return (int(best[0]), int(best[1]), int(best[2]), int(best[3]))
            except Exception:
                pass

        return None

    def analyze_framing(
        self,
        video_path: str,
        start_time: float,
        end_time: float,
        samples: int = 25,
    ) -> Dict[str, Any]:
        """
        Sample video segment to determine optimal 9:16 framing (face-tracked crop vs blurred stack).
        """
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1920)
        orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1080)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

        # 9:16 target crop width inside original frame
        crop_w = int(orig_h * (9 / 16))
        # Ensure even width
        crop_w = crop_w - (crop_w % 2)

        start_frame = int(start_time * fps)
        end_frame = min(total_frames, int(end_time * fps))
        frame_span = max(1, end_frame - start_frame)
        step = max(1, frame_span // samples)

        face_centers_x: List[float] = []
        detected_count = 0

        for f_idx in range(start_frame, end_frame, step):
            cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
            ret, frame = cap.read()
            if not ret or frame is None:
                continue

            face = self.detect_face_in_frame(frame)
            if face:
                fx, fy, fw, fh = face
                center_x = fx + (fw / 2.0)
                face_centers_x.append(center_x)
                detected_count += 1

        cap.release()

        detection_rate = detected_count / max(1, len(range(start_frame, end_frame, step)))

        # If faces are detected in >= 35% of samples, use face-tracking crop
        if detection_rate >= 0.35 and face_centers_x:
            # Use median face center to be robust against outliers
            median_center_x = float(np.median(face_centers_x))
            crop_x = int(median_center_x - (crop_w / 2.0))
            # Clamp to frame boundaries
            crop_x = max(0, min(orig_w - crop_w, crop_x))
            crop_x = crop_x - (crop_x % 2)

            return {
                "layout": "face_track",
                "detection_rate": round(detection_rate, 2),
                "crop_w": crop_w,
                "crop_h": orig_h,
                "crop_x": crop_x,
                "crop_y": 0,
                "orig_w": orig_w,
                "orig_h": orig_h,
            }

        # Fallback layout: Blurred zoomed background + centered 16:9 foreground
        return {
            "layout": "blurred_stack",
            "detection_rate": round(detection_rate, 2),
            "orig_w": orig_w,
            "orig_h": orig_h,
        }

    def render_clip(
        self,
        video_path: str,
        start_time: float,
        end_time: float,
        output_filename: str,
        subtitle_ass_path: Optional[str] = None,
        layout_mode: str = "auto",  # "auto", "face_track", "blurred_stack"
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> str:
        """
        Cut, reframe into 9:16 vertical (1080x1920), burn subtitles, and save final MP4.
        """
        output_path = str(self.output_dir / output_filename)
        duration = end_time - start_time

        # Framing determination
        if layout_mode == "auto":
            framing = self.analyze_framing(video_path, start_time, end_time)
            chosen_layout = framing["layout"]
        else:
            framing = self.analyze_framing(video_path, start_time, end_time)
            chosen_layout = layout_mode

        filter_parts: List[str] = []

        if chosen_layout == "face_track" and "crop_w" in framing:
            cw = framing["crop_w"]
            ch = framing["crop_h"]
            cx = framing["crop_x"]
            cy = framing["crop_y"]
            # Crop 9:16 region and scale to 1080x1920
            base_filter = f"[0:v]crop={cw}:{ch}:{cx}:{cy},scale={Config.TARGET_WIDTH}:{Config.TARGET_HEIGHT}:flags=lanczos[scaled]"
            current_v = "[scaled]"
        else:
            # Blurred background stack
            # Background: zoom, crop to 1080x1920, and blur
            # Foreground: scale source video to width 1080 (maintaining aspect ratio), center overlay
            base_filter = (
                f"[0:v]scale={Config.TARGET_WIDTH}:{Config.TARGET_HEIGHT}:force_original_aspect_ratio=increase,"
                f"crop={Config.TARGET_WIDTH}:{Config.TARGET_HEIGHT},boxblur=25:5[bg]; "
                f"[0:v]scale={Config.TARGET_WIDTH}:-2[fg]; "
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2[stacked]"
            )
            current_v = "[stacked]"

        filter_parts.append(base_filter)

        # Add subtitle burning if ASS file is provided
        working_dir = str(self.temp_dir)
        if subtitle_ass_path and os.path.exists(subtitle_ass_path):
            ass_rel = os.path.relpath(subtitle_ass_path, working_dir).replace("\\", "/")
            sub_filter = f"{current_v}ass=filename='{ass_rel}'[outv]"
            filter_parts.append(sub_filter)
            final_map_v = "[outv]"
        else:
            final_map_v = current_v

        filter_complex = "; ".join(filter_parts)

        cmd = [
            self.ffmpeg_path,
            "-y",
            "-ss",
            f"{start_time:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            video_path,
            "-filter_complex",
            filter_complex,
            "-map",
            final_map_v,
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            output_path,
        ]

        if progress_callback:
            progress_callback({
                "stage": "rendering",
                "layout": chosen_layout,
                "duration": round(duration, 2),
                "output": output_filename,
            })

        p = subprocess.run(
            cmd,
            cwd=working_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        if p.returncode != 0:
            raise RuntimeError(f"FFmpeg render failed with exit code {p.returncode}: {p.stderr[-1000:]}")

        return output_path
