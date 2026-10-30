"""
End-to-end processing pipeline orchestrating two-stage downloading, AI moment detection,
targeted partial clip downloads, exact word alignment, 9:16 reframing, and YouTube upload.
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
    """Master orchestrator for the high-speed automated viral clipping & Shorts pipeline."""

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
            device=whisper_device,
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
        layout_mode: str = "auto",
        caption_theme: str = "hormozi_yellow",
        words_per_line: int = 3,
        upload_to_youtube: bool = False,
        youtube_privacy: str = "private",
        dry_run_upload: bool = False,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Execute optimized two-stage pipeline:
        - If URL: Captions/low-audio first -> AI picks moments -> download ONLY those 30-60s sections -> word align -> render.
        - If local file: full local pipeline with backwards compatibility.
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

        is_local = os.path.exists(url_or_path)

        # ---------------------------------------------------------
        # STAGE 1: FAST DISCOVERY & TRANSCRIPTION
        # ---------------------------------------------------------
        if is_local:
            notify("download", "Reading local media file...")
            video_path, audio_path, media_info = self.downloader.download(url_or_path)
            results["metadata"] = media_info

            notify("transcribe", f"Transcribing local audio with faster-whisper ({self.transcriber.model_size})...")
            transcript_data = self.transcriber.transcribe(
                audio_path,
                word_timestamps=True,
                progress_callback=lambda d: notify("transcribe_progress", "Transcribing speech...", d),
            )
        else:
            notify("discovery", "Checking for YouTube captions/subtitles...")
            transcript_data, audio_path, media_info = self.downloader.fetch_fast_transcript_or_audio(
                url_or_path,
                progress_callback=lambda d: notify(d.get("stage", "discovery"), d.get("message", ""), d),
            )
            results["metadata"] = media_info

            if transcript_data and transcript_data.get("segments"):
                notify(
                    "discovery",
                    f"Direct YouTube captions loaded ({len(transcript_data['segments'])} segments). Skipping full Whisper audio transcribe!",
                )
            else:
                notify("transcribe", f"Running fast coarse transcription on low-bitrate audio ({self.transcriber.device})...")
                transcript_data = self.transcriber.transcribe(
                    audio_path,
                    word_timestamps=False,
                    beam_size=1,
                    progress_callback=lambda d: notify("transcribe_progress", "Coarse transcribing speech...", d),
                )

        results["transcript_words_count"] = len(transcript_data.get("words", []))

        # ---------------------------------------------------------
        # STAGE 2: AI BRAIN MOMENT DETECTION
        # ---------------------------------------------------------
        notify("ai_brain", f"AI Brain analyzing transcript for top {max_clips} viral moments...")
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

        # ---------------------------------------------------------
        # STAGE 3 & 4: TARGETED DOWNLOAD, WORD ALIGNMENT, & 9:16 RENDERING
        # ---------------------------------------------------------
        sub_gen = SubtitleGenerator(theme=caption_theme, words_per_line=words_per_line)
        processed_clips: List[Dict[str, Any]] = []

        for idx, moment in enumerate(moments):
            clip_num = idx + 1
            clip_st = moment["start_time"]
            clip_et = moment["end_time"]
            clip_dur = moment["duration"]
            base_filename = f"clip_{clip_num}_{int(clip_st)}_{int(clip_et)}"

            if is_local:
                # Local file: Use original video directly
                target_video = video_path
                render_st = clip_st
                render_et = clip_et

                # Subtitles from overall transcript
                ass_path = str(self.temp_dir / f"{base_filename}.ass")
                sub_gen.generate_ass(
                    words=transcript_data.get("words", []),
                    output_path=ass_path,
                    clip_start=clip_st,
                    clip_end=clip_et,
                )
            else:
                # Remote URL: Download ONLY this 30-60s clip
                notify(
                    "clip_download",
                    f"Downloading section for Clip {clip_num}/{len(moments)}: [{clip_st}s -> {clip_et}s] ({clip_dur}s)...",
                    clip_index=clip_num,
                    total_clips=len(moments),
                )
                raw_clip_path = str(self.temp_dir / f"raw_section_{clip_num}.mp4")
                actual_clip_path = self.downloader.download_clip_section(
                    url_or_path=url_or_path,
                    start_time=clip_st,
                    end_time=clip_et,
                    output_path=raw_clip_path,
                    progress_callback=lambda d: notify("download_progress", "Downloading clip section...", d),
                )
                target_video = actual_clip_path
                render_st = 0.0
                render_et = clip_dur

                # Extract audio for just this 30-60s clip and get exact word timestamps
                clip_audio = str(self.temp_dir / f"clip_audio_{clip_num}.wav")
                self.downloader.extract_audio(actual_clip_path, clip_audio)

                notify("clip_transcribe", f"Aligning word timestamps for Clip {clip_num}...")
                clip_words_data = self.transcriber.transcribe(clip_audio, word_timestamps=True)

                # Generate ASS Subtitles for the clip starting from 0.0s
                ass_path = str(self.temp_dir / f"{base_filename}.ass")
                sub_gen.generate_ass(
                    words=clip_words_data.get("words", []),
                    output_path=ass_path,
                    clip_start=0.0,
                    clip_end=clip_dur,
                )

            # Render clean 4:3 video with zero-crop face protection + burned subtitles
            out_mp4_name = f"{base_filename}_short.mp4"
            notify(
                "render_clip",
                f"Rendering 4:3 Clip {clip_num}/{len(moments)}: {moment.get('youtube_title', '')} (Virality: {moment['virality_score']}%)",
                clip_index=clip_num,
                total_clips=len(moments),
            )

            final_clip_path = self.editor.render_clip(
                video_path=target_video,
                start_time=render_st,
                end_time=render_et,
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
            processed_clips.append(clip_record)

        # Batch YouTube Upload in persistent browser session if requested
        if upload_to_youtube and processed_clips:
            notify("upload_short", f"Uploading {len(processed_clips)} clips to YouTube ({youtube_privacy})...")
            try:
                def upload_cb(d: Dict[str, Any]) -> None:
                    stg = d.get("stage", "upload_progress")
                    msg = d.get("message", "Uploading...")
                    notify(stg, msg, d)

                self.uploader.upload_multiple(
                    clips=processed_clips,
                    privacy_status=youtube_privacy,
                    dry_run=dry_run_upload,
                    progress_callback=upload_cb,
                )
            except Exception as e:
                notify("upload_error", f"YouTube upload failed: {e}")

            # Trigger immediate local file & temp cleanup for every successfully published clip
            for clip_rec in processed_clips:
                self.cleanup_uploaded_clip(clip_rec)

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

    def cleanup_uploaded_clip(self, clip_record: Dict[str, Any]) -> bool:
        """
        Deletes local .mp4 and associated temporary artifacts for a clip that was successfully uploaded.
        Updates the clip record status to 'Uploaded & Local File Cleaned'.
        """
        upload_res = clip_record.get("upload_result") or {}
        if upload_res.get("status") != "uploaded" or not upload_res.get("video_url"):
            return False

        file_path_str = clip_record.get("file_path") or ""
        file_name = clip_record.get("file_name") or ""
        deleted = False

        # 1. Delete local .mp4 from output directory
        target_files = []
        if file_path_str and os.path.exists(file_path_str):
            target_files.append(Path(file_path_str))
        if file_name and (self.output_dir / file_name).exists():
            target_files.append(self.output_dir / file_name)

        for p in set(target_files):
            try:
                p.unlink(missing_ok=True)
                deleted = True
                print(f"[ClipwaPipeline] 🗑️ Cleaned up uploaded local file: {p.name}")
            except Exception as e:
                print(f"[ClipwaPipeline] Warning deleting {p}: {e}")

        # 2. Delete temporary artifacts (.wav, .ass, raw section)
        if self.temp_dir.is_dir():
            stem = Path(file_name).stem if file_name else ""
            clip_id = clip_record.get("clip_id")
            patterns = [
                f"{stem}.*",
                f"clip_{clip_id}_*.*",
                f"raw_section_*_{clip_id}.mp4",
                f"clip_audio_{clip_id}.wav",
            ]
            for pat in patterns:
                for tf in self.temp_dir.glob(pat):
                    try:
                        tf.unlink(missing_ok=True)
                    except Exception:
                        pass

        # 3. Update record status
        clip_record["status"] = "Uploaded & Local File Cleaned"
        clip_record["local_file_deleted"] = True
        upload_res["local_file_cleaned"] = True
        return deleted

    def upload_existing_clips(
        self,
        summary_path: Optional[str] = None,
        privacy_status: str = "public",
        upgrade_titles: bool = True,
        force: bool = False,
        dry_run: bool = False,
        progress_callback: Optional[Callable[[str, str, Optional[Dict[str, Any]]], None]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Uploads clips that were already generated in the output directory.
        Upgrades their titles using AI Brain if requested.
        """
        def notify(stage: str, message: str, extra: Optional[Dict[str, Any]] = None, **kwargs) -> None:
            if progress_callback:
                progress_callback(stage, message, extra or kwargs)

        path = Path(summary_path) if summary_path else (self.output_dir / "pipeline_summary.json")
        data = {}
        clips = []

        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                clips = data.get("clips", [])
            except Exception:
                pass

        # Auto-discover clips if summary has no real video clips
        has_real_clips = any("4391" in c.get("file_name", "") or "6084" in c.get("file_name", "") or "2509" in c.get("file_name", "") for c in clips)
        if not clips or not has_real_clips:
            found_mp4s = sorted(list(self.output_dir.glob("*_short.mp4")))
            real_mp4s = [p for p in found_mp4s if "0_35" not in p.name and "synthetic" not in p.name]
            target_mp4s = real_mp4s if real_mp4s else found_mp4s
            if target_mp4s:
                clips = []
                for idx, p in enumerate(target_mp4s, 1):
                    raw_title = p.stem.replace("_short", "").replace("_", " ").title()
                    viral_title = self.ai_brain.generate_viral_title(raw_title)
                    clips.append({
                        "clip_id": idx,
                        "file_path": str(p),
                        "file_name": p.name,
                        "duration": 50.0,
                        "virality_score": 95,
                        "youtube_title": viral_title,
                        "youtube_description": f"{raw_title}\n\n#Shorts #Viral #Trending",
                        "tags": ["shorts", "viral", "trending"],
                        "upload_result": None,
                    })
                data["clips"] = clips

        if not clips:
            raise ValueError(f"No video clips found in output directory '{self.output_dir}'.")

        if upgrade_titles:
            notify("upgrade_titles", "Upgrading clip titles to high-CTR viral hooks using AI Brain...")
            for clip in clips:
                # Keep existing upgraded titles if already set and uploaded
                if (clip.get("upload_result") or {}).get("status") == "uploaded" and not force:
                    continue
                raw_text = clip.get("youtube_description", "") or clip.get("hook_text", "")
                clip["youtube_title"] = self.ai_brain.generate_viral_title(raw_text)

        notify("start_upload", f"Uploading existing Shorts to YouTube ({privacy_status})...")

        # Validate file paths
        valid_clips = []
        for idx, clip in enumerate(clips, 1):
            file_path = clip.get("file_path", "")
            if not os.path.exists(file_path):
                alt_path = str(self.output_dir / clip.get("file_name", ""))
                if os.path.exists(alt_path):
                    clip["file_path"] = alt_path
                    valid_clips.append(clip)
                else:
                    notify("skip_clip", f"Clip file not found: {file_path}")
                    clip["upload_result"] = {"error": f"File not found: {file_path}"}
            else:
                valid_clips.append(clip)

        # Filter out already uploaded clips unless force=True
        clips_to_upload = []
        for c in valid_clips:
            res = c.get("upload_result") or {}
            if not force and res.get("status") == "uploaded":
                notify("skip_clip", f"Clip already published: {c.get('youtube_title')} ({res.get('video_url')})")
            else:
                clips_to_upload.append(c)

        if clips_to_upload:
            def upload_cb(d: Dict[str, Any]) -> None:
                stg = d.get("stage", "upload_progress")
                msg = d.get("message", "Uploading...")
                notify(stg, msg, d)

            self.uploader.upload_multiple(
                clips=clips_to_upload,
                privacy_status=privacy_status,
                dry_run=dry_run,
                progress_callback=upload_cb,
            )

            # Auto-cleanup files for any newly uploaded clips
            for c in clips_to_upload:
                self.cleanup_uploaded_clip(c)
        else:
            notify("finished", "All clips are already uploaded to YouTube!")

        # Update summary file
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        notify("finished", f"Finished uploading {len(clips)} Shorts!")
        return clips
