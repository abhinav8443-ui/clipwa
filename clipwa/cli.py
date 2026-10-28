"""
Rich Command-Line Interface for Clipwa.
"""

import json
import os
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
from .video_editor import VideoEditor

console = Console()

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def print_banner():
    banner_text = (
        "[bold cyan]CLIPWA[/bold cyan] - [bold white]100% Free Automated Viral Clipping & YouTube Shorts Pipeline[/bold white]\n"
        "[dim]Powered by yt-dlp + faster-whisper + Gemini 2.5 Flash + MediaPipe + pysubs2 + FFmpeg[/dim]"
    )
    console.print(Panel(banner_text, border_style="cyan", expand=False))


@click.group()
def cli():
    """Clipwa - Automated viral video clipping & YouTube Shorts generator."""
    pass


@cli.command()
@click.argument("url_or_file")
@click.option("--max-clips", "-n", default=3, type=int, help="Number of viral shorts to create (1-5).")
@click.option("--min-duration", default=25.0, type=float, help="Minimum clip duration in seconds.")
@click.option("--max-duration", default=60.0, type=float, help="Maximum clip duration in seconds.")
@click.option(
    "--layout",
    type=click.Choice(["auto", "face_track", "blurred_stack"], case_sensitive=False),
    default="auto",
    help="9:16 Reframing layout.",
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
@click.option("--upload", is_flag=True, default=False, help="Upload generated shorts to YouTube.")
@click.option(
    "--privacy",
    type=click.Choice(["public", "unlisted", "private"], case_sensitive=False),
    default="private",
    help="YouTube Shorts privacy status.",
)
@click.option("--dry-run-upload", is_flag=True, default=False, help="Test YouTube upload flow without publishing.")
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
    upload,
    privacy,
    dry_run_upload,
):
    """Run full automated pipeline from URL/file to finished YouTube Shorts."""
    print_banner()

    console.print(f"[bold green]Target:[/bold green] {url_or_file}")
    console.print(f"[bold cyan]Settings:[/bold cyan] {max_clips} clips ({min_duration}-{max_duration}s) | Layout: {layout} | Theme: {theme} | LLM: {provider}")

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
                progress.update(task, stage="1/4 Discovery", description=msg)
            elif stage == "transcribe":
                progress.update(task, stage="1/4 Transcribe", completed=10, description=msg)
            elif stage == "ai_brain":
                progress.update(task, stage="2/4 AI Brain", completed=40, description=msg)
            elif stage == "clip_download":
                c_idx = event.get("clip_index", 1)
                t_clips = event.get("total_clips", 1)
                progress.update(task, stage=f"3/4 Download {c_idx}/{t_clips}", completed=15, description=msg)
            elif stage == "clip_transcribe":
                progress.update(task, stage="3/4 Word Align", completed=60, description=msg)
            elif stage == "render_clip":
                c_idx = event.get("clip_index", 1)
                t_clips = event.get("total_clips", 1)
                progress.update(task, stage=f"4/4 Render {c_idx}/{t_clips}", completed=80, description=msg)
            elif stage == "upload_short":
                progress.update(task, stage="5/5 YouTube", completed=95, description=msg)
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
            upload_to_youtube=upload,
            youtube_privacy=privacy,
            dry_run_upload=dry_run_upload,
            progress_callback=callback,
        )

    console.print("\n[bold green]Pipeline Execution Completed Successfully![/bold green]\n")

    # Display results table
    table = Table(title="Generated YouTube Shorts", border_style="cyan")
    table.add_column("#", justify="center", style="bold cyan")
    table.add_column("Title", style="bold white")
    table.add_column("Time Window", justify="center")
    table.add_column("Virality", justify="center", style="bold yellow")
    table.add_column("File Path", style="dim")
    table.add_column("YouTube Status", style="green")

    for clip in result.get("clips", []):
        up_status = "Skipped"
        if clip.get("upload_result"):
            res = clip["upload_result"]
            if res.get("status") == "uploaded":
                up_status = f"[green]Published: {res.get('url')}[/green]"
            elif res.get("status") == "dry_run_success":
                up_status = "[cyan]Dry Run OK[/cyan]"
            else:
                up_status = f"[red]Error: {res.get('error')}[/red]"

        table.add_row(
            str(clip["clip_id"]),
            clip["youtube_title"],
            f"{clip['start_time']}s - {clip['end_time']}s ({clip['duration']}s)",
            f"{clip['virality_score']}%",
            str(clip["file_path"]),
            up_status,
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
@click.option("--privacy", type=click.Choice(["public", "unlisted", "private"]), default="private")
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


def main():
    cli()


if __name__ == "__main__":
    main()
