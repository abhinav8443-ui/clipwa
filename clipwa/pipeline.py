"""
End-to-end processing pipeline orchestrating two-stage downloading, AI moment detection,
targeted partial clip downloads, exact word alignment, 9:16 reframing, and YouTube upload.
"""

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .ai_brain import ViralMomentDetector
from .config import Config, atomic_write_json
from .downloader import MediaDownloader
from .subtitle_generator import SubtitleGenerator
from .transcriber import LocalTranscriber, snap_to_complete_thought
from .uploader import YouTubeShortsUploader
from .insta_uploader import InstagramReelsUploader
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
        self.insta_uploader = InstagramReelsUploader(
            user_data_dir=Config.BASE_DIR / "youtube_auth_profile",
            output_dir=self.output_dir,
            temp_dir=self.temp_dir,
        )

        # Ensure custom viral background tracks are ready
        try:
            from .setup_custom_tracks import download_and_prepare_custom_tracks
            custom_tracks_dir = Config.BASE_DIR / "assets" / "audio" / "custom_tracks"
            if not custom_tracks_dir.exists() or len(list(custom_tracks_dir.glob("*.wav"))) < 5:
                download_and_prepare_custom_tracks()
        except Exception:
            pass

    def process_video(
        self,
        url_or_path: str,
        max_clips: int = 3,
        min_clip_duration: float = 35.0,
        max_clip_duration: float = 58.0,
        layout_mode: str = "auto",
        caption_theme: str = "hormozi_yellow",
        words_per_line: int = 3,
        upload_to_youtube: bool = False,
        youtube_privacy: str = "private",
        scheduled: bool = True,
        max_per_day: int = 2,
        dry_run_upload: bool = False,
        platforms: str = "all",
        engine: str = "openfilm",
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

        # Clear out any leftover files inside temp directory so old temporary files never build up
        if self.temp_dir.exists():
            for tf in self.temp_dir.iterdir():
                if tf.name == ".gitkeep":
                    continue
                try:
                    if tf.is_file() or tf.is_symlink():
                        tf.unlink(missing_ok=True)
                    elif tf.is_dir():
                        shutil.rmtree(tf, ignore_errors=True)
                except Exception:
                    pass

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
            b = snap_to_complete_thought(
                words=transcript_data.get("words", []),
                segments=transcript_data.get("segments", []),
                proposed_start=0.0,
                proposed_end=total_dur,
                min_dur=min_clip_duration,
                max_dur=max_clip_duration,
            )
            moments = [{
                "clip_id": 1,
                "start_time": b.start_time,
                "end_time": b.end_time,
                "duration": b.duration,
                "start_text": b.start_text,
                "end_text": b.end_text,
                "full_text": b.full_text,
                "virality_score": 85,
                "hook_text": media_info.get("title", "Highlight")[:40],
                "reason": "Intro highlight moment",
                "youtube_title": f"{media_info.get('title', 'Viral Clip')[:50]} #Shorts",
                "youtube_description": f"Highlight from {media_info.get('title', 'video')} #Shorts",
                "tags": ["shorts", "viral", "clip"],
            }]
            print(f'[BoundaryLock] Clip 1 locked: Starts with "{b.start_text}..." -> Ends with "...{b.end_text}" (Duration: {b.duration:.1f}s)')

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
                raw_clip_path = str(self.temp_dir / f"clip_video_{clip_num}_{int(clip_st)}_{int(clip_et)}.mp4")
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
                clip_audio = str(self.temp_dir / f"clip_align_audio_{clip_num}_{int(clip_st)}_{int(clip_et)}.wav")
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

            # Ensure viral title is strictly clean, valid, and devoid of markdown hashes
            clean_title = self.ai_brain.clean_and_validate_title(
                moment.get("youtube_title", ""),
                fallback_dialogue=moment.get("hook_text", "")
            )
            moment["youtube_title"] = clean_title

            # Render 9:16 vertical Shorts video with zero-crop face protection + burned subtitles
            out_mp4_name = f"{base_filename}_short.mp4"
            notify(
                "render_clip",
                f"Rendering 9:16 Shorts Clip {clip_num}/{len(moments)}: {moment.get('youtube_title', '')} (Impact: {moment['virality_score']}%)",
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
                clip_index=clip_num,
                engine=engine,
                words_data=clip_words_data.get("words", []),
                hook_title=moment.get("youtube_title", "") or moment.get("hook_text", ""),
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
                "instagram_result": None,
                "youtube_status": "Pending",
                "instagram_status": "Pending",
                "status": "Rendered",
                "file_deleted": False,
            }
            processed_clips.append(clip_record)

        # Batch Upload to selected platforms (YouTube Shorts and/or Instagram Reels)
        if upload_to_youtube and processed_clips:
            yt_needed = platforms in ("all", "youtube")
            ig_needed = platforms in ("all", "insta")

            # 1. YouTube Batch Upload
            if yt_needed:
                notify("upload_short", f"Uploading {len(processed_clips)} clips to YouTube Shorts ({youtube_privacy})...")
                try:
                    def upload_cb(d: Dict[str, Any]) -> None:
                        stg = d.get("stage", "upload_progress")
                        msg = d.get("message", "Uploading to YouTube...")
                        notify(stg, msg, d)

                    self.uploader.upload_multiple(
                        clips=processed_clips,
                        privacy_status=youtube_privacy,
                        scheduled=scheduled,
                        max_per_day=max_per_day,
                        dry_run=dry_run_upload,
                        auto_delete=(not ig_needed),  # Delete after YouTube only if Instagram is not needed
                        progress_callback=upload_cb,
                    )
                except Exception as e:
                    notify("upload_error", f"YouTube upload notice: {e}")

            # 2. Instagram Reels Batch Upload (Sequential - after YouTube context has closed cleanly)
            if ig_needed:
                notify("upload_insta", f"Uploading {len(processed_clips)} clips to Instagram Reels...")
                try:
                    def insta_cb(d: Dict[str, Any]) -> None:
                        stg = d.get("stage", "upload_progress")
                        msg = d.get("message", "Uploading to Instagram...")
                        notify(stg, msg, d)

                    self.insta_uploader.upload_multiple_reels(
                        clips=processed_clips,
                        scheduled=scheduled,
                        max_per_day=max_per_day,
                        dry_run=dry_run_upload,
                        auto_delete=False,
                        progress_callback=insta_cb,
                    )
                except Exception as e:
                    notify("upload_error", f"Instagram upload notice: {e}")

            # 3. Clean up local files ONLY after all requested platforms have completed
            for clip_rec in processed_clips:
                self.cleanup_uploaded_clip(clip_rec, platforms=platforms)

        results["clips"] = processed_clips
        results["status"] = "completed"
        results["elapsed_seconds"] = round(time.time() - start_clock, 2)

        # Save metadata summary JSON
        summary_path = str(self.output_dir / "pipeline_summary.json")
        try:
            atomic_write_json(summary_path, results)
            results["summary_file"] = summary_path
        except Exception:
            pass

        notify("finished", f"Pipeline complete! {len(processed_clips)} Shorts generated in {results['elapsed_seconds']}s.")
        return results

    def cleanup_uploaded_clip(self, clip_record: Dict[str, Any], platforms: str = "all") -> bool:
        """
        Deletes local .mp4 and associated temporary artifacts for a clip that was successfully uploaded or scheduled.
        When platforms='all', deletes ONLY AFTER BOTH YouTube and Instagram have completed!
        Updates clip record statuses to 'Uploaded & Deleted Locally' or 'Scheduled & Deleted Locally'.
        """
        youtube_needed = platforms in ("all", "youtube")
        insta_needed = platforms in ("all", "insta")

        yt_res = clip_record.get("upload_result") or {}
        yt_st = yt_res.get("status")
        yt_status = str(clip_record.get("youtube_status") or "")
        yt_done = (yt_st in ("uploaded", "scheduled")) or ("Scheduled" in yt_status) or ("Uploaded" in yt_status)

        ig_res = clip_record.get("instagram_result") or {}
        ig_st = ig_res.get("status")
        ig_status = str(clip_record.get("instagram_status") or "")
        ig_done = (ig_st in ("uploaded", "scheduled", "shared")) or ("Scheduled" in ig_status) or ("Shared" in ig_status) or ("Uploaded" in ig_status)

        # Ready to delete only if all requested platforms have succeeded
        ready_to_delete = True
        if youtube_needed and not yt_done:
            ready_to_delete = False
        if insta_needed and not ig_done:
            ready_to_delete = False

        if not ready_to_delete:
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
                f"raw_section_{clip_id}.*",
                f"raw_section_*_{clip_id}.*",
                f"clip_audio_{clip_id}.*",
            ]
            for pat in patterns:
                for tf in self.temp_dir.glob(pat):
                    try:
                        tf.unlink(missing_ok=True)
                    except Exception:
                        pass

        # 3. Update record status
        clip_record["file_deleted"] = True

        if yt_done:
            yt_final = "Scheduled & Deleted Locally" if (yt_st == "scheduled" or "Scheduled" in yt_status) else "Uploaded & Deleted Locally"
            clip_record["youtube_status"] = yt_final
            yt_res["file_deleted"] = True
            yt_res["local_file_cleaned"] = True
            clip_record["upload_result"] = yt_res

        if ig_done:
            ig_final = "Scheduled & Deleted Locally" if (ig_st == "scheduled" or "Scheduled" in ig_status) else "Shared & Deleted Locally"
            clip_record["instagram_status"] = ig_final
            ig_res["file_deleted"] = True
            ig_res["local_file_cleaned"] = True
            clip_record["instagram_result"] = ig_res

        clip_record["status"] = clip_record.get("youtube_status") or clip_record.get("instagram_status") or "Completed & Deleted Locally"

        # Sync with pipeline_summary.json
        summary_path = self.output_dir / "pipeline_summary.json"
        if summary_path.exists():
            try:
                with open(summary_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                clips = data.get("clips", [])
                for c in clips:
                    if (c.get("clip_id") == clip_record.get("clip_id")) or (
                        c.get("file_name") and c.get("file_name") == clip_record.get("file_name")
                    ):
                        c["youtube_status"] = clip_record.get("youtube_status")
                        c["instagram_status"] = clip_record.get("instagram_status")
                        c["file_deleted"] = True
                        c["status"] = clip_record["status"]
                        c["upload_result"] = yt_res
                        c["instagram_result"] = ig_res
                        break
                atomic_write_json(summary_path, data)
            except Exception:
                pass

        return deleted

    def upload_existing_clips(
        self,
        summary_path: Optional[str] = None,
        platforms: str = "all",
        privacy_status: str = "public",
        scheduled: bool = True,
        max_per_day: int = 2,
        upgrade_titles: bool = True,
        force: bool = False,
        dry_run: bool = False,
        progress_callback: Optional[Callable[[str, str, Optional[Dict[str, Any]]], None]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Uploads clips that were already generated in the output directory.
        Supports dual-platform uploading to YouTube Shorts and Instagram Reels.
        Strictly reads pipeline_summary.json and only uploads clips whose .mp4 file still exists.
        Never deletes local .mp4 until BOTH platforms have succeeded (when platforms='all').
        """
        def notify(stage: str, message: str, extra: Optional[Dict[str, Any]] = None, **kwargs) -> None:
            if progress_callback:
                progress_callback(stage, message, extra or kwargs)

        path = Path(summary_path) if summary_path else (self.output_dir / "pipeline_summary.json")
        if not path.exists():
            raise FileNotFoundError(f"Summary file not found at '{path}'. Run 'clipwa process <URL>' first.")

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        clips = data.get("clips", [])
        if not clips:
            raise ValueError(f"No clips found in summary file '{path}'.")

        youtube_needed = platforms in ("all", "youtube")
        insta_needed = platforms in ("all", "insta")

        clips_to_upload: List[Dict[str, Any]] = []
        for c in clips:
            is_deleted = c.get("file_deleted", False)

            # Check local file existence on disk
            f_path = c.get("file_path") or str(self.output_dir / c.get("file_name", ""))
            if not os.path.exists(f_path):
                alt_path = str(self.output_dir / c.get("file_name", ""))
                if os.path.exists(alt_path):
                    c["file_path"] = alt_path
                    f_path = alt_path
                else:
                    notify("skip_clip", f"Clip file not found on disk, skipping: {f_path}")
                    continue

            # Determine YouTube readiness
            yt_res = c.get("upload_result") or {}
            yt_status = str(c.get("youtube_status") or c.get("status") or "")
            yt_done = (
                not force
                and (
                    yt_res.get("status") in ("uploaded", "scheduled")
                    or "Deleted Locally" in yt_status
                    or yt_status in ("Scheduled", "Uploaded")
                )
            )

            # Determine Instagram readiness
            ig_res = c.get("instagram_result") or {}
            ig_status = str(c.get("instagram_status") or "")
            ig_done = (
                not force
                and (
                    ig_res.get("status") in ("uploaded", "scheduled", "shared")
                    or "Deleted Locally" in ig_status
                    or ig_status in ("Scheduled", "Shared", "Uploaded")
                )
            )

            # Skip if all requested platforms are already completed
            needs_yt = youtube_needed and not yt_done
            needs_ig = insta_needed and not ig_done

            if not force and not needs_yt and not needs_ig:
                notify("skip_clip", f"Clip already completed for requested platforms: {c.get('youtube_title')}")
                continue

            clips_to_upload.append(c)

        if not clips_to_upload:
            notify("finished", "No pending clips to upload in pipeline_summary.json!")
            return clips

        notify("start_upload", f"Found {len(clips_to_upload)} pending Short(s) to upload ({platforms.upper()})...")

        if upgrade_titles:
            for clip in clips_to_upload:
                cur_title = clip.get("youtube_title", "")
                raw_text = clip.get("youtube_description", "") or clip.get("hook_text", "")
                clip["youtube_title"] = self.ai_brain.clean_and_validate_title(cur_title, fallback_dialogue=raw_text)

        def upload_cb(d: Dict[str, Any]) -> None:
            stg = d.get("stage", "upload_progress")
            msg = d.get("message", "Uploading...")
            notify(stg, msg, d)

        # 1. YouTube Batch Upload (if requested)
        if youtube_needed:
            yt_batch = [
                c for c in clips_to_upload
                if force or not (
                    (c.get("upload_result") or {}).get("status") in ("uploaded", "scheduled")
                    or "Deleted Locally" in str(c.get("youtube_status", ""))
                )
            ]
            if yt_batch:
                notify("upload_short", f"Uploading {len(yt_batch)} clip(s) to YouTube Shorts...")
                try:
                    self.uploader.upload_multiple(
                        clips=yt_batch,
                        privacy_status=privacy_status,
                        scheduled=scheduled,
                        max_per_day=max_per_day,
                        force=force,
                        dry_run=dry_run,
                        auto_delete=(not insta_needed),  # only auto-delete if Instagram is not needed
                        progress_callback=upload_cb,
                    )
                except Exception as e:
                    notify("upload_error", f"YouTube upload notice: {e}")

        # 2. Instagram Reels Batch Upload (Sequential - runs after YouTube context has cleanly closed)
        if insta_needed:
            ig_batch = [
                c for c in clips_to_upload
                if force or not (
                    (c.get("instagram_result") or {}).get("status") in ("uploaded", "scheduled", "shared")
                    or "Deleted Locally" in str(c.get("instagram_status", ""))
                )
            ]
            if ig_batch:
                notify("upload_insta", f"Uploading {len(ig_batch)} clip(s) to Instagram Reels...")
                try:
                    self.insta_uploader.upload_multiple_reels(
                        clips=ig_batch,
                        scheduled=scheduled,
                        max_per_day=max_per_day,
                        dry_run=dry_run,
                        auto_delete=False,
                        progress_callback=upload_cb,
                    )
                except Exception as e:
                    notify("upload_error", f"Instagram upload notice: {e}")

        # 3. Enforce cleanup ONLY for clips that succeeded on all requested platforms
        for c in clips_to_upload:
            self.cleanup_uploaded_clip(c, platforms=platforms)

        # 4. Save and atomic-write summary file
        atomic_write_json(path, data)

        notify("finished", f"Finished processing {len(clips_to_upload)} clip(s) across {platforms.upper()}!")
        return clips
