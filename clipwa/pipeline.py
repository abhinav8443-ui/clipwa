"""
End-to-end processing pipeline orchestrating download, transcription, AI moment detection,
9:16 editing, dynamic captions, and YouTube Shorts uploading.
"""

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .ai_brain import ViralMomentDetector
from .config import Config
from .downloader import MediaDownloader
from .subtitle_generator import SubtitleGenerator
from .transcriber import LocalTranscriber
from .uploader import YouTubeShortsUploader
from .video_editor import VideoEditor


class ClipwaPipeline:
    """Master orchestrator for the automated viral clipping & Shorts publishing pipeline."""

    def __init__(
        self,
        llm_provider: Optional[str] = None,
        gemini_api_key: Optional[str] = None,
        whisper_model: Optional[str] = None,
        whisper_device: Optional[str] = None,
        temp_dir: Optional[Path] = None,
        output_dir: Optional[Path] = None,
    ):
        self.temp_dir = temp_dir or Config.TEMP_DIR
        self.output_dir = output_dir or Config.OUTPUT_DIR
        self.downloader = MediaDownloader(temp_dir=self.temp_dir)
        self.transcriber = LocalTranscriber(
            model_size=whisper_model or Config.WHISPER_MODEL,
            device=whisper_device or Config.WHISPER_DEVICE,
        )
        self.ai_brain = ViralMomentDetector(
            provider=llm_provider or Config.DEFAULT_LLM_PROVIDER,
            api_key=gemini_api_key or Config.GEMINI_API_KEY,
        )
        self.editor = VideoEditor(temp_dir=self.temp_dir, output_dir=self.output_dir)
        self.uploader = YouTubeShortsUploader()

    def process_video(
        self,
        url_or_path: str,
        max_clips: int = 3,
        min_clip_duration: float = 25.0,
        max_clip_duration: float = 60.0,
        layout_mode: str = "auto",  # auto, face_track, blurred_stack
        caption_theme: str = "hormozi_yellow",
        words_per_line: int = 3,
        upload_to_youtube: bool = False,
        youtube_privacy: str = "private",
        dry_run_upload: bool = False,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Execute full automated pipeline from source video URL/file to finished YouTube Shorts.
        """
        start_clock = time.time()
        results: Dict[str, Any] = {
            "source": url_or_path,
            "status": "processing",
            "metadata": {},
            "clips": [],
            "elapsed_seconds": 0,
        }

        def notify(stage: str, message: str, extra: Optional[Dict[str, Any]] = None, **kwargs):
            if progress_callback:
                data = dict(extra or {})
                data.update(kwargs)
                data["stage"] = stage
                data["message"] = message
                progress_callback(data)

        # Step 1: Download or locate media & extract audio
        notify("download", "Downloading / reading source media...")
        video_path, audio_path, media_info = self.downloader.download(
            url_or_path,
            progress_callback=lambda d: notify("download_progress", "Downloading media stream...", d),
        )
        results["metadata"] = media_info

        # Step 2: Transcribe audio locally with word-level timestamps
        notify("transcribe", f"Transcribing audio locally using faster-whisper ({self.transcriber.model_size})...")
        transcript_data = self.transcriber.transcribe(
            audio_path,
            progress_callback=lambda d: notify("transcribe_progress", "Transcribing speech...", d),
        )
        results["transcript_words_count"] = len(transcript_data.get("words", []))

        # Step 3: Analyze transcript with AI Brain to find viral moments
        notify("ai_brain", f"Analyzing transcript for top {max_clips} viral moments with {self.ai_brain.provider}...")
        moments = self.ai_brain.detect_moments(
            transcript_data=transcript_data,
            max_clips=max_clips,
            min_duration=min_clip_duration,
            max_duration=max_clip_duration,
        )

        if not moments:
            notify("warning", "No distinct viral moments detected, selecting fallback clip from start.")
            total_dur = min(max_clip_duration, media_info.get("duration", 30))
            moments = [{
                "clip_id": 1,
                "start_time": 0.0,
                "end_time": round(total_dur, 2),
                "duration": round(total_dur, 2),
                "virality_score": 85,
                "hook_text": media_info.get("title", "Highlight")[:40],
                "reason": "Intro highlight moment",
                "youtube_title": f"{media_info.get('title', 'Viral Clip')[:50]} #Shorts",
                "youtube_description": f"Highlight from {media_info.get('title', 'video')} #Shorts",
                "tags": ["shorts", "viral", "clip"],
            }]

        results["detected_moments"] = moments

        # Step 4: Process and edit each moment into a 9:16 Short
        sub_gen = SubtitleGenerator(
            theme=caption_theme,
            words_per_line=words_per_line,
        )

        processed_clips: List[Dict[str, Any]] = []

        for idx, moment in enumerate(moments):
            clip_num = idx + 1
            clip_st = moment["start_time"]
            clip_et = moment["end_time"]
            clip_dur = moment["duration"]

            notify(
                "render_clip",
                f"Generating Clip {clip_num}/{len(moments)}: [{clip_st}s -> {clip_et}s] (Virality: {moment['virality_score']}%)",
                clip_index=clip_num,
                total_clips=len(moments),
            )

            # Generate ASS Subtitles for this specific clip time window
            base_filename = f"clip_{clip_num}_{int(clip_st)}_{int(clip_et)}"
            ass_path = str(self.temp_dir / f"{base_filename}.ass")
            sub_gen.generate_ass(
                words=transcript_data.get("words", []),
                output_path=ass_path,
                clip_start=clip_st,
                clip_end=clip_et,
            )

            # Render 9:16 vertical video with auto face-tracking or blurred stack + burned subtitles
            out_mp4_name = f"{base_filename}_short.mp4"
            final_clip_path = self.editor.render_clip(
                video_path=video_path,
                start_time=clip_st,
                end_time=clip_et,
                output_filename=out_mp4_name,
                subtitle_ass_path=ass_path,
                layout_mode=layout_mode,
                progress_callback=lambda d: notify("render_progress", f"Rendering clip {clip_num}...", d),
            )

            clip_record: Dict[str, Any] = {
                "clip_id": clip_num,
                "file_path": final_clip_path,
                "file_name": out_mp4_name,
                "file_size_bytes": os.path.getsize(final_clip_path),
                "start_time": clip_st,
                "end_time": clip_et,
                "duration": clip_dur,
                "virality_score": moment.get("virality_score", 90),
                "hook_text": moment.get("hook_text", ""),
                "reason": moment.get("reason", ""),
                "youtube_title": moment.get("youtube_title", f"Short #{clip_num} #Shorts"),
                "youtube_description": moment.get("youtube_description", "#Shorts"),
                "tags": moment.get("tags", ["shorts", "viral"]),
                "upload_result": None,
            }

            # Step 5: (Optional) Upload to YouTube Shorts
            if upload_to_youtube:
                notify("upload_short", f"Uploading Clip {clip_num} to YouTube Shorts ({youtube_privacy})...")
                try:
                    upload_res = self.uploader.upload_short(
                        video_path=final_clip_path,
                        title=clip_record["youtube_title"],
                        description=clip_record["youtube_description"],
                        tags=clip_record["tags"],
                        privacy_status=youtube_privacy,
                        dry_run=dry_run_upload,
                        progress_callback=lambda d: notify("upload_progress", "Uploading chunk...", d),
                    )
                    clip_record["upload_result"] = upload_res
                except Exception as e:
                    clip_record["upload_result"] = {"error": str(e)}
                    notify("upload_error", f"YouTube upload failed: {e}")

            processed_clips.append(clip_record)

        results["clips"] = processed_clips
        results["status"] = "completed"
        results["elapsed_seconds"] = round(time.time() - start_clock, 2)

        # Save metadata summary JSON
        summary_path = str(self.output_dir / "pipeline_summary.json")
        try:
            with open(summary_path, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
            results["summary_file"] = summary_path
        except Exception:
            pass

        notify("finished", f"Pipeline complete! {len(processed_clips)} Shorts generated in {results['elapsed_seconds']}s.")
        return results
