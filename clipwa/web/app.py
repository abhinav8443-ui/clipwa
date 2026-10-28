"""
Interactive Web UI server for Clipwa (Zero-dependency, built-in HTTP server).
"""

import http.server
import json
import mimetypes
import os
import socketserver
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List

from ..config import Config
from ..pipeline import ClipwaPipeline
from ..uploader import YouTubeShortsUploader

CURRENT_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = CURRENT_DIR / "templates"

# Global pipeline state
PIPELINE_STATE: Dict[str, Any] = {
    "status": "idle",
    "stage": "",
    "message": "Ready to process videos.",
    "progress_percent": 0,
    "current_clip": 0,
    "total_clips": 0,
    "logs": [],
    "last_result": None,
}


def run_pipeline_thread(params: Dict[str, Any]):
    global PIPELINE_STATE
    PIPELINE_STATE["status"] = "running"
    PIPELINE_STATE["logs"] = []
    PIPELINE_STATE["progress_percent"] = 5
    PIPELINE_STATE["message"] = "Starting Clipwa Pipeline..."
    PIPELINE_STATE["last_result"] = None

    def callback(event):
        stage = event.get("stage", "")
        msg = event.get("message", "")
        PIPELINE_STATE["stage"] = stage
        PIPELINE_STATE["message"] = msg
        timestamp = time.strftime("%H:%M:%S")
        PIPELINE_STATE["logs"].append(f"[{timestamp}] [{stage.upper()}] {msg}")

        # Update progress percent estimation
        if stage == "download":
            PIPELINE_STATE["progress_percent"] = 15
        elif stage == "transcribe":
            PIPELINE_STATE["progress_percent"] = 35
        elif stage == "ai_brain":
            PIPELINE_STATE["progress_percent"] = 55
        elif stage == "render_clip":
            c_idx = event.get("clip_index", 1)
            t_clips = event.get("total_clips", 1)
            PIPELINE_STATE["current_clip"] = c_idx
            PIPELINE_STATE["total_clips"] = t_clips
            pct = 60 + int((c_idx / max(1, t_clips)) * 30)
            PIPELINE_STATE["progress_percent"] = min(90, pct)
        elif stage == "upload_short":
            PIPELINE_STATE["progress_percent"] = 95
        elif stage == "finished":
            PIPELINE_STATE["progress_percent"] = 100

    try:
        pipeline = ClipwaPipeline(
            llm_provider=params.get("provider", "gemini"),
            whisper_model=params.get("whisper_model", "tiny"),
        )
        res = pipeline.process_video(
            url_or_path=params.get("url", ""),
            max_clips=int(params.get("max_clips", 3)),
            min_clip_duration=float(params.get("min_duration", 25.0)),
            max_clip_duration=float(params.get("max_duration", 60.0)),
            layout_mode=params.get("layout", "auto"),
            caption_theme=params.get("theme", "hormozi_yellow"),
            words_per_line=int(params.get("words_per_line", 3)),
            upload_to_youtube=bool(params.get("upload", False)),
            youtube_privacy=params.get("privacy", "private"),
            dry_run_upload=bool(params.get("dry_run_upload", False)),
            progress_callback=callback,
        )
        PIPELINE_STATE["status"] = "completed"
        PIPELINE_STATE["message"] = f"Finished! {len(res.get('clips', []))} clips created."
        PIPELINE_STATE["last_result"] = res
        PIPELINE_STATE["progress_percent"] = 100
    except Exception as e:
        PIPELINE_STATE["status"] = "error"
        PIPELINE_STATE["message"] = f"Error: {str(e)}"
        PIPELINE_STATE["logs"].append(f"[ERROR] {str(e)}")


class ClipwaHTTPHandler(http.server.SimpleHTTPRequestHandler):
    """Custom HTTP handler serving dashboard and REST endpoints."""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ["/", "/index.html"]:
            self.serve_html()
            return

        if path == "/api/status":
            self.send_json(PIPELINE_STATE)
            return

        if path == "/api/clips":
            clips = []
            if PIPELINE_STATE.get("last_result"):
                clips = PIPELINE_STATE["last_result"].get("clips", [])
            self.send_json({"clips": clips})
            return

        if path.startswith("/video/"):
            filename = os.path.basename(path)
            file_path = Config.OUTPUT_DIR / filename
            if file_path.exists():
                self.serve_video_file(str(file_path))
                return
            else:
                self.send_error(404, "Video clip not found")
                return

        super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        try:
            params = json.loads(body)
        except Exception:
            params = {}

        if path == "/api/process":
            if PIPELINE_STATE["status"] == "running":
                self.send_json({"error": "A pipeline task is already actively running!"}, status=400)
                return

            url = params.get("url", "").strip()
            if not url:
                self.send_json({"error": "Video URL or local file path is required."}, status=400)
                return

            thread = threading.Thread(target=run_pipeline_thread, args=(params,), daemon=True)
            thread.start()
            self.send_json({"status": "started", "message": "Pipeline initiated in background."})
            return

        if path == "/api/upload":
            clip_name = params.get("clip_name", "")
            title = params.get("title", "Viral Clip #Shorts")
            description = params.get("description", "#Shorts")
            privacy = params.get("privacy", "private")
            dry_run = bool(params.get("dry_run", False))

            clip_path = Config.OUTPUT_DIR / clip_name
            if not clip_path.exists():
                self.send_json({"error": f"Clip file not found: {clip_name}"}, status=404)
                return

            try:
                uploader = YouTubeShortsUploader()
                res = uploader.upload_short(
                    video_path=str(clip_path),
                    title=title,
                    description=description,
                    privacy_status=privacy,
                    dry_run=dry_run,
                )
                self.send_json(res)
            except Exception as e:
                self.send_json({"error": str(e)}, status=500)
            return

        self.send_error(404, "Endpoint not found")

    def serve_html(self):
        index_file = TEMPLATES_DIR / "index.html"
        if not index_file.exists():
            self.send_error(404, "index.html template missing")
            return
        with open(index_file, "rb") as f:
            content = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def serve_video_file(self, file_path: str):
        file_size = os.path.getsize(file_path)
        mime_type, _ = mimetypes.guess_type(file_path)
        mime_type = mime_type or "video/mp4"

        self.send_response(200)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(file_size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

        with open(file_path, "rb") as f:
            while chunk := f.read(64 * 1024):
                self.wfile.write(chunk)

    def send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Quiet standard HTTP logs to avoid cluttering console
        pass


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def run_server(host: str = "127.0.0.1", port: int = 8000):
    server = ThreadedHTTPServer((host, port), ClipwaHTTPHandler)
    print(f"\n[Clipwa Web UI] Server running at http://{host}:{port}/")
    print("[Clipwa Web UI] Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Clipwa Web UI server...")
        server.shutdown()
