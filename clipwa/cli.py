"""
Rich Command-Line Interface for Clipwa.
"""

import os
# Silence C++ logging from TensorFlow Lite, MediaPipe, and Abseil
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
os.environ["GLOG_minloglevel"] = "3"
os.environ["ABSL_MIN_LOG_LEVEL"] = "3"

import json
import sys
from pathlib import Path
from typing import Optional

import click
from rich import print as rprint
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

from .ai_brain import ViralMomentDetector
from .config import Config
from .downloader import MediaDownloader
from .pipeline import ClipwaPipeline
from .subtitle_generator import SubtitleGenerator
from .transcriber import LocalTranscriber
from .uploader import YouTubeShortsUploader
from .insta_uploader import InstagramReelsUploader
from .video_editor import VideoEditor

console = Console()

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass


def print_banner():
    banner_text = (
        "[bold cyan]CLIPWA[/bold cyan] - [bold white]100% Free Automated Viral Clipping & YouTube Shorts Pipeline[/bold white]\n"
        "[dim]Powered by yt-dlp + faster-whisper + Gemini 2.5 Flash + MediaPipe + pysubs2 + FFmpeg[/dim]"
    )
    console.print(Panel(banner_text, border_style="cyan", expand=False))


def _format_yt_status(clip: dict) -> str:
    res = clip.get("upload_result") or {}
    yt_st = clip.get("youtube_status") or res.get("status") or ""
    slot_data = res.get("scheduled_slot")
    if isinstance(slot_data, dict):
        slot_label = slot_data.get("slot_label") or slot_data.get("slot_name") or res.get("scheduled_for")
    else:
        slot_label = str(slot_data) if slot_data else res.get("scheduled_for")

    if res.get("status") == "scheduled" or "Scheduled" in str(yt_st):
        return f"[green]Scheduled: {slot_label}[/green]" if slot_label else f"[green]{yt_st}[/green]"
    elif res.get("status") == "uploaded" or "Uploaded" in str(yt_st):
        pub_url = res.get("video_url") or res.get("url")
        return f"[green]Uploaded: {pub_url}[/green]" if pub_url else f"[green]{yt_st}[/green]"
    elif res.get("status") == "queued_daily_limit":
        return "[yellow]Queued (Daily Cap)[/yellow]"
    elif res.get("status") == "dry_run_success":
        return f"[cyan]Dry Run OK ({slot_label or 'Simulated'})[/cyan]"
    elif res.get("error"):
        return f"[red]Error: {res.get('error')}[/red]"
    elif yt_st and yt_st not in ("Skipped", "Pending"):
        return f"[cyan]{yt_st}[/cyan]"
    elif yt_st == "Pending":
        return "[yellow]Pending[/yellow]"
    return "[dim]Skipped[/dim]"


def _format_ig_status(clip: dict) -> str:
    res = clip.get("instagram_result") or {}
    ig_st = clip.get("instagram_status") or res.get("status") or ""
    slot_data = res.get("scheduled_slot")
    if isinstance(slot_data, dict):
        slot_label = slot_data.get("slot_label") or slot_data.get("slot_name") or res.get("scheduled_for")
    else:
        slot_label = str(slot_data) if slot_data else res.get("scheduled_for")

    if res.get("status") in ("scheduled", "shared", "uploaded") or any(k in str(ig_st) for k in ("Scheduled", "Shared", "Uploaded")):
        if slot_label and "Scheduled" in str(ig_st):
            return f"[magenta]Scheduled: {slot_label}[/magenta]"
        return f"[magenta]{ig_st}[/magenta]"
    elif res.get("status") == "queued_daily_limit":
        return "[yellow]Queued (Daily Cap)[/yellow]"
    elif res.get("status") == "dry_run_success":
        return "[cyan]Dry Run OK[/cyan]"
    elif res.get("error"):
        return f"[red]Error: {res.get('error')}[/red]"
    elif ig_st and ig_st not in ("Skipped", "Pending"):
        return f"[cyan]{ig_st}[/cyan]"
    elif ig_st == "Pending":
        return "[yellow]Pending[/yellow]"
    return "[dim]Skipped[/dim]"


def _format_local_file(clip: dict) -> str:
    is_del = clip.get("file_deleted") or clip.get("local_file_deleted")
    fpath = clip.get("file_path")
    if is_del or not fpath or not os.path.exists(fpath):
        return "[dim]Cleaned up[/dim]"
    return Path(fpath).name


@click.group()
def cli():
    """Clipwa - Automated viral video clipping & YouTube Shorts generator."""
    pass


@cli.command()
@click.argument("url_or_file")
@click.option("--max-clips", "-n", default=3, type=int, help="Number of viral shorts to create (1-5).")
@click.option("--min-duration", default=35.0, type=float, help="Minimum clip duration in seconds.")
@click.option("--max-duration", default=58.0, type=float, help="Maximum clip duration in seconds.")
@click.option(
    "--layout",
    type=click.Choice(["9:16", "blurred_stack", "auto", "face_track", "4:3"], case_sensitive=False),
    default="9:16",
    help="Video layout: 9:16 (YouTube Shorts Zero-Crop Stack), blurred_stack, face_track, or 4:3.",
)
@click.option(
    "--theme",
    type=click.Choice(["hormozi_yellow", "neon_cyan", "emerald_green", "hot_pink", "clean_white"], case_sensitive=False),
    default="hormozi_yellow",
    help="Alex Hormozi caption style theme.",
)
@click.option("--words-per-line", default=3, type=int, help="Words per subtitle card (1-5).")
@click.option("--provider", default="gemini", help="LLM Provider: gemini, groq, ollama, heuristic.")
@click.option("--whisper-model", default="tiny", help="Whisper model: tiny, base, small, medium.")
@click.option(
    "--platforms",
    "-P",
    type=click.Choice(["all", "youtube", "insta"], case_sensitive=False),
    default="all",
    help="Target upload platforms: all, youtube, or insta (default: all).",
)
@click.option(
    "--upload/--no-upload",
    default=True,
    help="Upload generated shorts to YouTube/Instagram (default: True).",
)
@click.option(
    "--privacy",
    "-p",
    type=click.Choice(["public", "unlisted", "private"], case_sensitive=False),
    default="public",
    help="YouTube Shorts privacy status (default: public).",
)
@click.option("--dry-run-upload", is_flag=True, default=False, help="Test upload flow without publishing.")
@click.option(
    "--schedule/--immediate",
    default=True,
    help="Schedule Shorts in research-backed peak engagement windows (default: True).",
)
@click.option(
    "--max-per-day",
    default=2,
    type=click.IntRange(1, 4),
    help="Maximum scheduled Shorts per day (default: 2).",
)
@click.option(
    "--engine",
    type=click.Choice(["openfilm", "ffmpeg"], case_sensitive=False),
    default="openfilm",
    help="Rendering engine: openfilm or ffmpeg (default: openfilm).",
)
def process(
    url_or_file,
    max_clips,
    min_duration,
    max_duration,
    layout,
    theme,
    words_per_line,
    provider,
    whisper_model,
    platforms,
    upload,
    privacy,
    dry_run_upload,
    schedule,
    max_per_day,
    engine,
):
    """Run full automated pipeline from URL/file to finished YouTube Shorts & Instagram Reels."""
    print_banner()

    console.print(f"[bold green]Target:[/bold green] {url_or_file}")
    console.print(f"[bold cyan]Settings:[/bold cyan] {max_clips} clips ({min_duration}-{max_duration}s) | Layout: {layout} | Theme: {theme} | Platforms: {platforms.upper()} | LLM: {provider}")

    pipeline = ClipwaPipeline(
        llm_provider=provider,
        whisper_model=whisper_model,
    )

    with Progress(
        SpinnerColumn(spinner_name="line"),
        TextColumn("[bold cyan]{task.fields[stage]}[/bold cyan]"),
        BarColumn(bar_width=None),
        TaskProgressColumn(),
        TextColumn("{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("", total=100, completed=0, stage="Starting")

        def callback(event):
            stage = event.get("stage", "")
            msg = event.get("message", "")
            pct = event.get("percent")
            speed = event.get("speed")
            eta = event.get("eta")

            speed_str = ""
            if speed and speed > 0:
                mb_s = speed / (1024 * 1024)
                speed_str = f" [{mb_s:.1f} MB/s]"
            if eta and eta > 0:
                speed_str += f" [ETA {int(eta)}s]"

            if stage in ("download_progress", "transcribe_progress", "upload_progress", "render_progress"):
                if pct is not None:
                    progress.update(task, completed=pct, description=f"{msg}{speed_str}")
            elif stage == "discovery":
                progress.update(task, stage="1/5 Discovery", description=msg)
            elif stage == "transcribe":
                progress.update(task, stage="1/5 Transcribe", completed=10, description=msg)
            elif stage == "ai_brain":
                progress.update(task, stage="2/5 AI Brain", completed=35, description=msg)
            elif stage == "clip_download":
                c_idx = event.get("clip_index", 1)
                t_clips = event.get("total_clips", 1)
                progress.update(task, stage=f"3/5 Download {c_idx}/{t_clips}", completed=15, description=msg)
            elif stage == "clip_transcribe":
                progress.update(task, stage="3/5 Word Align", completed=55, description=msg)
            elif stage == "render_clip":
                c_idx = event.get("clip_index", 1)
                t_clips = event.get("total_clips", 1)
                progress.update(task, stage=f"4/5 Render {c_idx}/{t_clips}", completed=75, description=msg)
            elif stage == "upload_short":
                progress.update(task, stage="5/5 YouTube", completed=90, description=msg)
            elif stage == "upload_insta":
                progress.update(task, stage="5/5 Instagram", completed=95, description=msg)
            elif stage == "finished":
                progress.update(task, stage="Finished", completed=100, description=msg)

        result = pipeline.process_video(
            url_or_path=url_or_file,
            max_clips=max_clips,
            min_clip_duration=min_duration,
            max_clip_duration=max_duration,
            layout_mode=layout,
            caption_theme=theme,
            words_per_line=words_per_line,
            platforms=platforms,
            upload_to_youtube=upload,
            youtube_privacy=privacy,
            scheduled=schedule,
            max_per_day=max_per_day,
            dry_run_upload=dry_run_upload,
            engine=engine,
            progress_callback=callback,
        )

    console.print("\n[bold green]Pipeline Execution Completed Successfully![/bold green]\n")

    # Display results table
    table = Table(title="Generated Shorts & Reels", border_style="cyan")
    table.add_column("#", justify="center", style="bold cyan")
    table.add_column("Upgraded Viral Title", style="bold white")
    table.add_column("Duration", justify="center")
    table.add_column("Virality", justify="center", style="bold yellow")
    table.add_column("YouTube Status", style="green")
    table.add_column("Instagram Status", style="magenta")
    table.add_column("Local File", style="dim")

    for clip in result.get("clips", []):
        dur_val = clip.get("duration", 0)
        dur_str = f"{dur_val:.1f}s" if isinstance(dur_val, (int, float)) else str(dur_val)
        table.add_row(
            str(clip.get("clip_id", 1)),
            str(clip.get("youtube_title", "Viral Short")),
            dur_str,
            f"{clip.get('virality_score', 85)}%",
            _format_yt_status(clip),
            _format_ig_status(clip),
            _format_local_file(clip),
        )

    console.print(table)
    console.print(f"\n[bold]Output Directory:[/bold] {Config.OUTPUT_DIR}")


@cli.command()
@click.argument("media_file")
@click.option("--model", default="tiny", help="Whisper model size: tiny, base, small, medium.")
@click.option("--output", "-o", default=None, help="Output JSON transcript file path.")
def transcribe(media_file, model, output):
    """Transcribe audio or video locally with word timestamps."""
    print_banner()
    downloader = MediaDownloader()
    transcriber = LocalTranscriber(model_size=model)

    console.print(f"[cyan]Extracting audio from:[/cyan] {media_file}")
    temp_audio = str(Config.TEMP_DIR / f"{Path(media_file).stem}_16k.wav")
    downloader.extract_audio(media_file, temp_audio)

    console.print(f"[yellow]Transcribing locally with faster-whisper ({model})...[/yellow]")
    data = transcriber.transcribe(temp_audio)

    out_file = output or str(Path(media_file).with_suffix(".transcript.json"))
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    console.print(f"[green]Saved transcript to:[/green] {out_file}")
    console.print(f"[bold]Detected Language:[/bold] {data.get('language')} ({data.get('language_probability', 1.0):.1%})")
    console.print(f"[bold]Total Words:[/bold] {len(data.get('words', []))}")


@cli.command()
@click.argument("transcript_json")
@click.option("--max-clips", "-n", default=3, type=int, help="Number of viral moments to detect.")
@click.option("--provider", default="gemini", help="LLM Provider: gemini, groq, ollama, heuristic.")
def detect(transcript_json, max_clips, provider):
    """Detect top viral moments from transcript JSON."""
    print_banner()
    with open(transcript_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    detector = ViralMomentDetector(provider=provider)
    console.print(f"[cyan]Analyzing transcript with {provider}...[/cyan]")
    moments = detector.detect_moments(data, max_clips=max_clips)

    table = Table(title="Detected Viral Moments", border_style="magenta")
    table.add_column("ID", justify="center")
    table.add_column("Title", style="bold white")
    table.add_column("Start-End", justify="center")
    table.add_column("Virality", justify="center", style="bold yellow")
    table.add_column("Hook", style="italic")

    for m in moments:
        table.add_row(
            str(m["clip_id"]),
            m["youtube_title"],
            f"{m['start_time']}s - {m['end_time']}s",
            f"{m['virality_score']}%",
            m["hook_text"],
        )
    console.print(table)


@cli.command()
@click.argument("video_file")
@click.option("--start", "-s", required=True, type=float, help="Start time in seconds.")
@click.option("--end", "-e", required=True, type=float, help="End time in seconds.")
@click.option("--layout", type=click.Choice(["auto", "face_track", "blurred_stack"]), default="auto")
@click.option("--subtitles", "-sub", default=None, help="Path to ASS subtitle file.")
@click.option("--output", "-o", default=None, help="Output MP4 filename.")
def cut(video_file, start, end, layout, subtitles, output):
    """Cut and reframe a video into 9:16 vertical."""
    print_banner()
    editor = VideoEditor()
    out_name = output or f"cut_{int(start)}_{int(end)}_short.mp4"

    console.print(f"[cyan]Rendering 9:16 vertical Short ({start}s -> {end}s)...[/cyan]")
    final_path = editor.render_clip(
        video_path=video_file,
        start_time=start,
        end_time=end,
        output_filename=out_name,
        subtitle_ass_path=subtitles,
        layout_mode=layout,
    )
    console.print(f"[green]Clip rendered successfully:[/green] {final_path}")


@cli.command()
@click.argument("video_file")
@click.option("--title", "-t", required=True, help="YouTube Shorts Title.")
@click.option("--description", "-d", default="Check out this viral moment! #Shorts", help="Description.")
@click.option(
    "--privacy",
    "-p",
    type=click.Choice(["public", "unlisted", "private"], case_sensitive=False),
    default="public",
    help="YouTube Shorts privacy status (default: public).",
)
@click.option("--dry-run", is_flag=True, default=False, help="Simulate upload without publishing.")
def upload(video_file, title, description, privacy, dry_run):
    """Upload a video directly to YouTube Shorts."""
    print_banner()
    uploader = YouTubeShortsUploader()
    console.print(f"[cyan]Uploading {video_file} to YouTube Shorts...[/cyan]")
    res = uploader.upload_short(
        video_path=video_file,
        title=title,
        description=description,
        privacy_status=privacy,
        dry_run=dry_run,
    )
    console.print(f"[green]Upload Status:[/green] {res.get('status')}")
    if res.get("url"):
        console.print(f"[bold cyan]Short URL:[/bold cyan] {res.get('url')}")


@cli.command(name="studio")
@click.argument("clip_folder", required=False, default=None)
def studio_cmd(clip_folder):
    """Launch OpenFilm Studio in browser for interactive preview and timeline editing."""
    print_banner()
    from .openfilm_engine import OpenFilmEngine
    engine = OpenFilmEngine()
    target_dir = Path(clip_folder) if clip_folder else None
    engine.open_studio(target_dir)


@cli.command()
@click.option("--port", default=8000, type=int, help="Port to host the Web UI.")
@click.option("--host", default="127.0.0.1", help="Host interface.")
def web(port, host):
    """Launch the interactive Clipwa Web UI."""
    print_banner()
    from .web.app import run_server
    run_server(host=host, port=port)


@cli.command()
def test():
    """Run built-in end-to-end self-test using a synthetic video."""
    print_banner()
    from .tester import run_synthetic_test
    run_synthetic_test()


@cli.command()
@click.option("--timeout", default=300, help="Max seconds to wait for login.")
def login(timeout):
    """Sign in to your YouTube account once via persistent browser."""
    print_banner()
    uploader = YouTubeShortsUploader()
    success = uploader.login_studio(timeout_sec=timeout)
    if success:
        console.print("[bold green]✅ YouTube Studio login saved successfully![/bold green]")
    else:
        console.print("[bold red]❌ Login was not completed or timed out.[/bold red]")


@cli.command(name="switch-channel")
@click.option("--timeout", default=300, help="Max seconds to wait for channel selection.")
def switch_channel_cmd(timeout):
    """Switch active YouTube channel via channel switcher."""
    print_banner()
    uploader = YouTubeShortsUploader()
    res = uploader.switch_channel(timeout_sec=timeout)
    if res.get("status") == "success":
        console.print(f"[bold green]✅ Active channel updated to: {res.get('channel_name', 'Active Channel')} ({res.get('channel_id', 'ID')})[/bold green]")
    else:
        console.print("[bold red]❌ Channel switch was not completed or timed out.[/bold red]")


@cli.command(name="login-insta")
@click.option("--timeout", default=300, help="Max seconds to wait for Instagram login.")
def login_insta_cmd(timeout):
    """Sign in to your Instagram account once via persistent browser."""
    print_banner()
    uploader = InstagramReelsUploader()
    console.print(f"[cyan]Opening persistent browser for Instagram login (up to {timeout}s)...[/cyan]")
    console.print("[dim]Please complete login or 2FA in the opened browser window.[/dim]")
    success = uploader.login_insta(timeout_sec=timeout)
    if success:
        console.print("[bold green]✅ Instagram login saved and verified permanently![/bold green]")
    else:
        console.print("[bold red]❌ Instagram login was not completed or timed out.[/bold red]")


@cli.command(name="upload-existing")
@click.option(
    "--platforms",
    "-P",
    type=click.Choice(["all", "youtube", "insta"], case_sensitive=False),
    default="all",
    help="Target upload platforms: all, youtube, or insta (default: all).",
)
@click.option(
    "--privacy",
    "-p",
    type=click.Choice(["public", "unlisted", "private"], case_sensitive=False),
    default="public",
    help="YouTube visibility status (default: public).",
)
@click.option("--summary", default=None, help="Path to pipeline_summary.json (defaults to output directory).")
@click.option("--dry-run", is_flag=True, default=False, help="Simulate upload without publishing.")
@click.option("--no-title-upgrade", is_flag=True, default=False, help="Skip AI Brain title upgrading.")
@click.option("--force", "-f", is_flag=True, default=False, help="Re-upload even if already uploaded.")
@click.option(
    "--schedule/--immediate",
    default=True,
    help="Schedule Shorts in research-backed peak engagement windows (default: True).",
)
@click.option(
    "--max-per-day",
    default=2,
    type=click.IntRange(1, 4),
    help="Maximum scheduled Shorts per day (default: 2).",
)
def upload_existing(platforms, privacy, summary, dry_run, no_title_upgrade, force, schedule, max_per_day):
    """Upload previously generated clips from output/ to YouTube Shorts and/or Instagram Reels."""
    print_banner()
    pipeline = ClipwaPipeline()

    mode_str = f"Scheduled Mode (Max {max_per_day}/day)" if schedule else f"Immediate Mode ({privacy.upper()})"
    console.print(f"[bold cyan]Uploading existing clips ({platforms.upper()}) from output directory [{mode_str}]...[/bold cyan]")

    def on_progress(stage, message, extra=None):
        if stage in ("upgrade_titles", "start_upload", "uploading_clip", "skip_clip", "finished"):
            console.print(f"[cyan]•[/cyan] {message}")
        elif stage in ("upload_short", "upload_insta", "upload_progress"):
            console.print(f"[yellow]•[/yellow] {message}")
        elif stage == "upload_error":
            console.print(f"[bold red]✗[/bold red] {message}")

    try:
        updated_clips = pipeline.upload_existing_clips(
            summary_path=summary,
            platforms=platforms,
            privacy_status=privacy,
            scheduled=schedule,
            max_per_day=max_per_day,
            upgrade_titles=not no_title_upgrade,
            force=force,
            dry_run=dry_run,
            progress_callback=on_progress,
        )

        table = Table(title="Shorts & Reels Dual Upload Status", show_header=True, header_style="bold magenta")
        table.add_column("#", style="dim", width=4)
        table.add_column("Upgraded Viral Title", style="bold white", width=34)
        table.add_column("Duration", justify="right", width=10)
        table.add_column("Virality", justify="center", width=10)
        table.add_column("YouTube Status", style="green", width=32)
        table.add_column("Instagram Status", style="magenta", width=32)
        table.add_column("Local File", style="dim", width=22)

        for clip in updated_clips:
            dur_val = clip.get("duration", 0)
            dur_str = f"{dur_val:.1f}s" if isinstance(dur_val, (int, float)) else str(dur_val)
            table.add_row(
                str(clip.get("clip_id", 1)),
                clip.get("youtube_title", ""),
                dur_str,
                f"{clip.get('virality_score', 85)}%",
                _format_yt_status(clip),
                _format_ig_status(clip),
                _format_local_file(clip),
            )

        console.print("\n")
        console.print(table)
        console.print(f"\n[bold green]✅ All existing clips processed across {platforms.upper()} successfully![/bold green]")
    except Exception as e:
        console.print(f"[bold red]Error uploading existing clips:[/bold red] {e}")


def main():
    cli()


if __name__ == "__main__":
    main()
