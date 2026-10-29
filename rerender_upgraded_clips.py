"""
Re-render the 3 viral clips with:
- Massive 92px Impact pop-in Hormozi subtitles
- Crisp 1080p HD sharpened video (CRF 18 / 8M bitrate)
- 0.0s Hook Sound Effect + background retention tune + compressed audio
"""

import json
import os
import sys
import time
from pathlib import Path

# Fix Windows console encoding for Unicode/emojis
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from clipwa.config import Config
from clipwa.downloader import MediaDownloader
from clipwa.transcriber import LocalTranscriber
from clipwa.subtitle_generator import SubtitleGenerator
from clipwa.video_editor import VideoEditor

def rerender_all():
    summary_path = Config.OUTPUT_DIR / "pipeline_summary.json"
    with open(summary_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    url = data.get("source", "https://youtu.be/BV7QRw8wtjs?si=6GowOCb8k5fcw6WL")
    clips = data.get("clips", [])
    
    print("\n" + "=" * 75)
    print(f" 🎬 RE-RENDERING {len(clips)} CLIPS WITH ULTRA-HD, HUGE SUBTITLES & HOOK AUDIO")
    print("=" * 75)

    downloader = MediaDownloader()
    transcriber = LocalTranscriber(model_size="base")
    sub_gen = SubtitleGenerator(theme="hormozi_yellow", font_name="Impact", words_per_line=3)
    editor = VideoEditor()

    for idx, clip in enumerate(clips, 1):
        st = clip["start_time"]
        et = clip["end_time"]
        dur = clip["duration"]
        title = clip["youtube_title"]
        out_name = clip["file_name"]

        print(f"\n[{idx}/{len(clips)}] Processing: {title}")
        print(f"      Time range: {st}s -> {et}s (Duration: {dur:.2f}s)")

        # 1. Download 1080p section
        raw_clip = str(Config.TEMP_DIR / f"raw_section_1080p_{idx}.mp4")
        if not os.path.exists(raw_clip):
            print("      [1/4] Downloading 1080p section...")
            actual_raw = downloader.download_clip_section(
                url_or_path=url,
                start_time=st,
                end_time=et,
                output_path=raw_clip,
            )
        else:
            print("      [1/4] Using cached 1080p raw section...")
            actual_raw = raw_clip

        # 2. Extract audio and get exact word timestamps
        print("      [2/4] Transcribing word-level timestamps...")
        clip_wav = str(Config.TEMP_DIR / f"clip_{idx}_16k.wav")
        downloader.extract_audio(actual_raw, clip_wav)
        words_data = transcriber.transcribe(clip_wav, word_timestamps=True)

        # 3. Generate massive pop-in ASS subtitles
        print("      [3/4] Generating massive size-92 Hormozi subtitles...")
        ass_path = str(Config.TEMP_DIR / f"clip_{idx}_huge.ass")
        sub_gen.generate_ass(
            words=words_data.get("words", []),
            output_path=ass_path,
            clip_start=0.0,
            clip_end=dur,
        )

        # 4. Render 9:16 vertical HD with audio mix & sharpening
        print("      [4/4] Rendering 1080x1920 HD with Hook SFX + BG Tune...")
        final_mp4 = editor.render_clip(
            video_path=actual_raw,
            start_time=0.0,
            end_time=dur,
            output_filename=out_name,
            subtitle_ass_path=ass_path,
            layout_mode="auto",
        )

        clip["file_size_bytes"] = os.path.getsize(final_mp4)
        clip["file_path"] = final_mp4
        clip["upload_result"] = None  # Reset for new channel upload
        print(f"      ✅ Rendered: {out_name} ({clip['file_size_bytes'] / (1024*1024):.1f} MB)")

    # Save updated metadata
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 75)
    print(" 🎉 ALL 3 CLIPS SUCCESSFULLY RE-RENDERED IN ULTRA-HD!")
    print("=" * 75 + "\n")

if __name__ == '__main__':
    rerender_all()
