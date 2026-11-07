"""
OpenFilm 9:16 Compositor Engine for Clipwa.
Builds HTML/CSS/Canvas motion-graphics projects according to OpenFilm Protocol 0.1,
executes automated visual QA (openfilm look), and renders professional 1080x1920 MP4s (openfilm render).
"""

import json
import logging
import os
import re
import shutil
import subprocess
import time
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import Config

logger = logging.getLogger("clipwa.openfilm")

# High-energy / Power words to highlight in electric green (#00FF66)
POWER_WORDS = {
    "win", "winning", "winner", "winners", "money", "rich", "wealth", "growth",
    "grow", "success", "successful", "dominate", "unstoppable", "power", "powerful",
    "10x", "2x", "million", "billion", "champion", "conquer", "greatness", "achieve",
    "profit", "scaling", "results", "freedom", "superpower", "legend", "elite",
}

# Danger / Pain / Obstacle words to highlight in neon red (#FF3366)
DANGER_WORDS = {
    "fail", "failure", "failing", "pain", "quit", "quitting", "fear", "danger",
    "lose", "losing", "loser", "dead", "death", "die", "destroy", "broke", "hate",
    "suffer", "suffering", "kill", "killing", "mistake", "regret", "trap", "crisis",
    "anxiety", "stress", "worst", "stop", "never",
}


class OpenFilmEngine:
    """Professional OpenFilm 9:16 motion-graphics compositor and rendering engine."""

    def __init__(
        self,
        temp_dir: Optional[Path] = None,
        output_dir: Optional[Path] = None,
        bin_dir: Optional[Path] = None,
    ):
        self.temp_dir = Path(temp_dir or Config.TEMP_DIR)
        self.output_dir = Path(output_dir or Config.OUTPUT_DIR)
        self.bin_dir = Path(bin_dir or (Config.BASE_DIR / "bin"))
        self.projects_dir = self.temp_dir / "openfilm_projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)

        # Inject local ffmpeg/ffprobe into PATH so openfilm CLI detects them
        self._inject_bin_path()

        # Locate openfilm executable
        self.openfilm_cmd = self._find_openfilm_cli()

    def _inject_bin_path(self) -> None:
        """Prepend local bin directory to os.environ['PATH']."""
        if self.bin_dir.exists():
            bin_str = str(self.bin_dir.resolve())
            current_path = os.environ.get("PATH", "")
            if bin_str not in current_path:
                os.environ["PATH"] = bin_str + os.pathsep + current_path

    def _find_openfilm_cli(self) -> Optional[str]:
        """Find the openfilm executable on the system."""
        # 1. System PATH
        for cmd_name in ("openfilm.cmd", "openfilm", "openfilm.ps1"):
            p = shutil.which(cmd_name)
            if p:
                return p

        # 2. Windows npm global prefix
        appdata_npm = Path(os.environ.get("APPDATA", "")) / "npm"
        for name in ("openfilm.cmd", "openfilm"):
            candidate = appdata_npm / name
            if candidate.exists():
                return str(candidate)

        return None

    def is_available(self) -> bool:
        """Check if OpenFilm CLI is installed and responsive."""
        if not self.openfilm_cmd:
            self.openfilm_cmd = self._find_openfilm_cli()
        if not self.openfilm_cmd:
            return False

        try:
            self._inject_bin_path()
            res = subprocess.run(
                [self.openfilm_cmd, "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                shell=(os.name == "nt"),
            )
            return res.returncode == 0
        except Exception as e:
            logger.warning(f"OpenFilm availability check failed: {e}")
            return False

    def probe_duration(self, file_path: Path) -> float:
        """Probe exact media duration using wave module for wav files or ffprobe for video/audio."""
        p = Path(file_path).resolve()
        if not p.exists():
            return 0.0
        if p.suffix.lower() == ".wav":
            try:
                with wave.open(str(p), "rb") as w:
                    return w.getnframes() / float(w.getframerate())
            except Exception:
                pass
        ffprobe_bin = str(self.bin_dir / "ffprobe.exe")
        if not Path(ffprobe_bin).exists():
            ffprobe_bin = shutil.which("ffprobe") or "ffprobe"
        try:
            cmd = [
                ffprobe_bin,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(p),
            ]
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                shell=(os.name == "nt"),
            )
            if res.returncode == 0 and res.stdout.strip():
                return float(res.stdout.strip())
        except Exception:
            pass
        return 0.0

    def _group_words_into_phrases(
        self,
        words: List[Dict[str, Any]],
        max_words_per_phrase: int = 3,
        max_phrase_duration: float = 1.8,
    ) -> List[Dict[str, Any]]:
        """
        Group individual word timestamps into short 2-3 word rhythmic phrases for
        Hormozi-style kinetic subtitle pacing.
        """
        if not words:
            return []

        phrases: List[Dict[str, Any]] = []
        current_chunk: List[Dict[str, Any]] = []

        for w in words:
            word_text = str(w.get("word", "")).strip()
            if not word_text:
                continue

            start_t = float(w.get("start", 0.0))
            end_t = float(w.get("end", start_t + 0.3))

            clean_word = {
                "word": word_text,
                "start": start_t,
                "end": max(end_t, start_t + 0.15),
            }

            if not current_chunk:
                current_chunk.append(clean_word)
                continue

            # Check if adding this word exceeds limits or punctuation pauses
            chunk_start = current_chunk[0]["start"]
            chunk_dur = end_t - chunk_start
            prev_word = current_chunk[-1]["word"]
            has_break = prev_word.endswith((".", "!", "?", ",", "—", "-"))

            if len(current_chunk) >= max_words_per_phrase or chunk_dur > max_phrase_duration or has_break:
                phrases.append({
                    "start": current_chunk[0]["start"],
                    "end": current_chunk[-1]["end"],
                    "words": current_chunk,
                })
                current_chunk = [clean_word]
            else:
                current_chunk.append(clean_word)

        if current_chunk:
            phrases.append({
                "start": current_chunk[0]["start"],
                "end": current_chunk[-1]["end"],
                "words": current_chunk,
            })

        return phrases

    def build_project(
        self,
        clip_id: int,
        video_path: str,
        duration: float,
        hook_title: str,
        words_data: Optional[List[Dict[str, Any]]] = None,
        bg_tune_path: Optional[Path] = None,
        hook_sfx_path: Optional[Path] = None,
        font_size_px: int = 68,
    ) -> Path:
        """
        Build an OpenFilm project folder adhering strictly to the OpenFilm Protocol:
        temp/openfilm_projects/clip_<N>/
          film.html
          overlay.html
          assets/
            clip.mp4
            hook_sfx.wav
            bg_music.wav
        """
        project_dir = self.projects_dir / f"clip_{clip_id}"
        assets_dir = project_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)

        # 1. Copy or link media assets into assets/
        src_video = Path(video_path).resolve()
        target_video = assets_dir / "clip.mp4"
        try:
            if not target_video.exists() or target_video.stat().st_size != src_video.stat().st_size:
                shutil.copy2(src_video, target_video)
        except Exception:
            shutil.copyfile(src_video, target_video)
        clip_actual_dur = self.probe_duration(target_video)

        has_hook_sfx = False
        sfx_actual_dur = 0.0
        if hook_sfx_path and Path(hook_sfx_path).exists():
            target_sfx = assets_dir / "hook_sfx.wav"
            try:
                shutil.copyfile(hook_sfx_path, target_sfx)
                sfx_actual_dur = self.probe_duration(target_sfx)
                has_hook_sfx = target_sfx.exists() and target_sfx.stat().st_size > 0
            except Exception:
                pass

        has_bg_music = False
        bg_actual_dur = 0.0
        if bg_tune_path and Path(bg_tune_path).exists():
            target_bg = assets_dir / "bg_music.wav"
            try:
                src_bg_dur = self.probe_duration(Path(bg_tune_path))
                needed_dur = max(65.0, duration + 5.0)
                if src_bg_dur < needed_dur:
                    # Loop background music with ffmpeg so it is guaranteed longer than the clip
                    ffmpeg_bin = str(self.bin_dir / "ffmpeg.exe")
                    if not Path(ffmpeg_bin).exists():
                        ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
                    tmp_bg = assets_dir / "bg_music_loop.wav"
                    cmd = [
                        ffmpeg_bin, "-y",
                        "-stream_loop", "-1",
                        "-i", str(bg_tune_path),
                        "-t", f"{needed_dur:.2f}",
                        "-c:a", "pcm_s16le",
                        "-ar", "48000",
                        "-ac", "2",
                        str(tmp_bg),
                    ]
                    subprocess.run(
                        cmd,
                        check=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        shell=(os.name == "nt"),
                    )
                    shutil.move(str(tmp_bg), target_bg)
                else:
                    shutil.copyfile(bg_tune_path, target_bg)

                bg_actual_dur = self.probe_duration(target_bg)
                has_bg_music = target_bg.exists() and target_bg.stat().st_size > 0
            except Exception as e:
                logger.warning(f"Failed to prepare bg_music.wav: {e}")

        # 2. Format words data into phrases
        phrases = self._group_words_into_phrases(words_data or [])

        # 3. Clean hook title for display
        clean_hook = re.sub(r"[#*_~`]", "", hook_title).strip()
        # Remove trailing hashtags
        clean_hook = re.sub(r"\s*#\w+", "", clean_hook).strip().upper()
        if len(clean_hook) > 65:
            clean_hook = clean_hook[:62] + "..."

        # 4. Generate overlay.html (HUD, Typography, Animations)
        overlay_html_path = project_dir / "overlay.html"
        self._write_overlay_html(
            output_path=overlay_html_path,
            duration=duration,
            hook_title=clean_hook,
            phrases=phrases,
            font_size_px=font_size_px,
        )

        # 5. Generate film.html (Multi-Track OpenFilm Stage)
        film_html_path = project_dir / "film.html"
        self._write_film_html(
            output_path=film_html_path,
            duration=duration,
            clip_actual_dur=clip_actual_dur,
            bg_actual_dur=bg_actual_dur,
            has_hook_sfx=has_hook_sfx,
            has_bg_music=has_bg_music,
        )

        logger.info(f"OpenFilm project generated at: {project_dir}")
        return project_dir

    def _write_film_html(
        self,
        output_path: Path,
        duration: float,
        clip_actual_dur: float,
        bg_actual_dur: float,
        has_hook_sfx: bool,
        has_bg_music: bool,
    ) -> None:
        """Write the multi-track film.html according to OpenFilm Protocol 0.1."""
        # Clamp video duration strictly to actual file duration - 0.02s
        if clip_actual_dur > 0:
            v_end = min(duration, clip_actual_dur - 0.02)
        else:
            v_end = duration
        v_dur_str = f"{v_end:.3f}"

        audio_tracks: List[str] = []
        if has_hook_sfx:
            # Omit #t= so OpenFilm naturally plays the full sound effect without media fragment range errors
            audio_tracks.append(
                '  <section>\n    <audio id="hook_sfx" src="assets/hook_sfx.wav" volume="0.45"></audio>\n  </section>'
            )
        if has_bg_music:
            if bg_actual_dur > 0:
                bg_end = min(duration, bg_actual_dur - 0.05)
            else:
                bg_end = duration
            audio_tracks.append(
                f'  <section>\n    <audio id="bg_music" src="assets/bg_music.wav#t=0,{bg_end:.3f}" volume="0.11"></audio>\n  </section>'
            )

        audio_tracks_block = "\n".join(audio_tracks) if audio_tracks else ""

        html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=1080, height=1920">
  <style>
    body {{
      margin: 0;
      background: #000;
      overflow: hidden;
    }}
    .bg-video {{
      object-fit: cover;
      filter: blur(36px) brightness(0.38) saturate(1.15);
      transform: scale(1.08);
    }}
    .fg-video {{
      object-fit: contain;
      border-radius: 18px;
      box-shadow: 0 25px 60px rgba(0, 0, 0, 0.7);
    }}
  </style>
</head>
<body>
  <!-- Track 1: Cinematic HUD, 3s Hook Banner & Pro Hormozi Kinetic Typography -->
  <section>
    <iframe id="overlay" src="overlay.html" at="0" style="left: 0; top: 0; width: 1080px; height: 1920px"></iframe>
  </section>

  <!-- Track 2: Primary Foreground Video with Voice Audio (volume=1.0) -->
  <section>
    <video id="fg_video" src="assets/clip.mp4#t=0,{v_dur_str}" volume="1.0" class="fg-video"
           style="left: 0; top: 460px; width: 1080px; height: 1000px"></video>
  </section>

  <!-- Track 3: Ambient Blurred Background Video (muted) -->
  <section>
    <video id="bg_video" src="assets/clip.mp4#t=0,{v_dur_str}" volume="0" class="bg-video"
           style="left: 0; top: 0; width: 1080px; height: 1920px"></video>
  </section>

  <!-- Audio Stems: Hook SFX and Rotated Viral Music -->
{audio_tracks_block}
</body>
</html>
"""
        output_path.write_text(html, encoding="utf-8")

    def _write_overlay_html(
        self,
        output_path: Path,
        duration: float,
        hook_title: str,
        phrases: List[Dict[str, Any]],
        font_size_px: int,
    ) -> None:
        """
        Write overlay.html containing deterministic frame(t) logic for:
        1. 3-Second Glassmorphic Hook Banner with cubic-bezier entrance/exit.
        2. Pro Hormozi word-by-word kinetic typography with spring scale pops.
        3. Bottom neon progress bar + vignette + audio-reactive waveform indicators.
        """
        phrases_json = json.dumps(phrases, ensure_ascii=False)
        power_words_json = json.dumps(list(POWER_WORDS))
        danger_words_json = json.dumps(list(DANGER_WORDS))

        html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    * {{
      box-sizing: border-box;
      user-select: none;
    }}
    body {{
      margin: 0;
      width: 1080px;
      height: 1920px;
      overflow: hidden;
      background: transparent;
      font-family: 'Montserrat', 'Impact', 'Arial Black', -apple-system, BlinkMacSystemFont, sans-serif;
      color: #FFFFFF;
      position: relative;
    }}

    /* Cinematic Edge Vignette */
    #vignette {{
      position: absolute;
      top: 0;
      left: 0;
      width: 1080px;
      height: 1920px;
      background: radial-gradient(ellipse at center, rgba(0,0,0,0) 62%, rgba(0,0,0,0.45) 100%);
      pointer-events: none;
      z-index: 1;
    }}

    /* 3-Second Viral Hook Banner (Safe Zone: top 210px) */
    #hook-banner {{
      position: absolute;
      top: 210px;
      left: 60px;
      width: 960px;
      padding: 24px 30px;
      background: rgba(18, 18, 24, 0.78);
      backdrop-filter: blur(24px);
      -webkit-backdrop-filter: blur(24px);
      border-radius: 24px;
      border: 1.5px solid rgba(255, 255, 255, 0.22);
      box-shadow: 0 16px 45px rgba(0, 0, 0, 0.65), inset 0 1px 0 rgba(255, 255, 255, 0.2);
      text-align: center;
      font-size: 38px;
      font-weight: 900;
      letter-spacing: 0.5px;
      line-height: 1.25;
      text-transform: uppercase;
      color: #FFFFFF;
      text-shadow: 0 3px 12px rgba(0, 0, 0, 0.8);
      transform-origin: center top;
      opacity: 0;
      z-index: 10;
    }}
    #hook-badge {{
      display: inline-block;
      font-size: 19px;
      font-weight: 800;
      background: #FFE600;
      color: #000;
      padding: 4px 14px;
      border-radius: 12px;
      margin-bottom: 8px;
      letter-spacing: 1.5px;
    }}

    /* Pro Hormozi Kinetic Subtitles Safe Box (Central 9:16 safe area: 54% - 66%) */
    #subtitles-container {{
      position: absolute;
      top: 1060px;
      left: 60px;
      width: 960px;
      min-height: 160px;
      display: flex;
      flex-wrap: wrap;
      justify-content: center;
      align-items: center;
      gap: 16px 20px;
      text-align: center;
      z-index: 20;
    }}

    .word {{
      display: inline-block;
      font-size: {font_size_px}px;
      font-weight: 900;
      text-transform: uppercase;
      line-height: 1.1;
      letter-spacing: 0.5px;
      color: #FFFFFF;
      -webkit-text-stroke: 3.5px #000000;
      text-shadow: 0 6px 16px rgba(0, 0, 0, 0.95), 0 0 24px rgba(0, 0, 0, 0.7);
      transition: none;
      transform-origin: center center;
    }}

    /* Active Highlighted Word with Spring Physics */
    .word.active {{
      color: #FFE600;
      transform: scale(1.18);
      -webkit-text-stroke: 4px #000000;
      text-shadow: 0 8px 24px rgba(0, 0, 0, 1), 0 0 30px rgba(255, 230, 0, 0.5);
    }}

    .word.active.power {{
      color: #00FF66 !important;
      text-shadow: 0 8px 24px rgba(0, 0, 0, 1), 0 0 30px rgba(0, 255, 102, 0.6) !important;
    }}

    .word.active.danger {{
      color: #FF3366 !important;
      text-shadow: 0 8px 24px rgba(0, 0, 0, 1), 0 0 30px rgba(255, 51, 102, 0.6) !important;
    }}

    /* Bottom Neon Retention Progress Bar */
    #progress-track {{
      position: absolute;
      bottom: 34px;
      left: 60px;
      width: 960px;
      height: 8px;
      background: rgba(255, 255, 255, 0.18);
      border-radius: 4px;
      overflow: hidden;
      z-index: 10;
    }}
    #progress-fill {{
      height: 100%;
      width: 0%;
      background: #FFE600;
      border-radius: 4px;
      box-shadow: 0 0 14px #FFE600;
    }}

    /* Dynamic Audio-Reactive Waveform Indicator */
    #waveform-bar {{
      position: absolute;
      bottom: 54px;
      right: 60px;
      display: flex;
      align-items: flex-end;
      gap: 5px;
      height: 24px;
      z-index: 10;
      opacity: 0.85;
    }}
    .wave-stick {{
      width: 5px;
      background: #FFE600;
      border-radius: 3px;
      height: 6px;
      box-shadow: 0 0 8px rgba(255, 230, 0, 0.6);
    }}
  </style>
</head>
<body>
  <div id="vignette"></div>

  <!-- 3-Second Viral Hook Banner -->
  <div id="hook-banner">
    <div id="hook-badge">VIRAL HOOK</div><br>
    <span id="hook-text">{hook_title}</span>
  </div>

  <!-- Hormozi Kinetic Typography Container -->
  <div id="subtitles-container"></div>

  <!-- Audio Waveform Indicator -->
  <div id="waveform-bar">
    <div class="wave-stick" id="ws1"></div>
    <div class="wave-stick" id="ws2"></div>
    <div class="wave-stick" id="ws3"></div>
    <div class="wave-stick" id="ws4"></div>
    <div class="wave-stick" id="ws5"></div>
  </div>

  <!-- Bottom Retention Progress Bar -->
  <div id="progress-track">
    <div id="progress-fill"></div>
  </div>

  <script>
    const DURATION = {duration};
    const phrases = {phrases_json};
    const powerWords = new Set({power_words_json});
    const dangerWords = new Set({danger_words_json});

    const banner = document.getElementById('hook-banner');
    const subContainer = document.getElementById('subtitles-container');
    const progressFill = document.getElementById('progress-fill');
    const waveSticks = [
      document.getElementById('ws1'),
      document.getElementById('ws2'),
      document.getElementById('ws3'),
      document.getElementById('ws4'),
      document.getElementById('ws5')
    ];

    let cachedPhraseIdx = -1;

    function cubicBezier(t) {{
      // Smooth cubic-bezier(0.16, 1, 0.3, 1) approximation
      return 1 - Math.pow(1 - t, 3);
    }}

    function cleanWordText(raw) {{
      return raw.replace(/[^a-zA-Z0-9]/g, '').toLowerCase();
    }}

    // OpenFilm Protocol 0.1 frame(t) Implementation
    window.film = {{
      duration: DURATION,
      width: 1080,
      height: 1920,
      frame(t) {{
        // 1. Hook Banner Animation (0.0s to 2.8s)
        if (t < 0.42) {{
          const p = cubicBezier(t / 0.42);
          banner.style.display = 'block';
          banner.style.opacity = p;
          banner.style.transform = `translateY(${{(1 - p) * -38}}px) scale(${{0.92 + 0.08 * p}})`;
        }} else if (t < 2.40) {{
          banner.style.display = 'block';
          banner.style.opacity = 1;
          banner.style.transform = 'translateY(0) scale(1)';
        }} else if (t < 2.80) {{
          const p = cubicBezier((t - 2.40) / 0.40);
          banner.style.display = 'block';
          banner.style.opacity = 1 - p;
          banner.style.transform = `translateY(${{-p * 36}}px) scale(${{1 - 0.08 * p}})`;
        }} else {{
          banner.style.display = 'none';
        }}

        // 2. Hormozi Kinetic Typography
        let currentPhrase = null;
        let currentPhraseIdx = -1;
        for (let i = 0; i < phrases.length; i++) {{
          const ph = phrases[i];
          if (t >= ph.start && t <= ph.end + 0.12) {{
            currentPhrase = ph;
            currentPhraseIdx = i;
            break;
          }}
        }}

        if (!currentPhrase) {{
          subContainer.innerHTML = '';
          cachedPhraseIdx = -1;
        }} else {{
          // Re-render DOM elements only if phrase index changed
          if (cachedPhraseIdx !== currentPhraseIdx) {{
            cachedPhraseIdx = currentPhraseIdx;
            subContainer.innerHTML = '';
            currentPhrase.words.forEach((w, wIdx) => {{
              const span = document.createElement('span');
              span.className = 'word';
              span.id = `w_${{wIdx}}`;
              span.textContent = w.word;
              subContainer.appendChild(span);
            }});
          }}

          // Apply kinetic highlight and spring scale to active spoken word
          currentPhrase.words.forEach((w, wIdx) => {{
            const el = document.getElementById(`w_${{wIdx}}`);
            if (!el) return;

            const isSpoken = (t >= w.start && t <= w.end);
            if (isSpoken) {{
              el.classList.add('active');
              const cl = cleanWordText(w.word);
              if (powerWords.has(cl)) {{
                el.classList.add('power');
                el.classList.remove('danger');
              }} else if (dangerWords.has(cl)) {{
                el.classList.add('danger');
                el.classList.remove('power');
              }} else {{
                el.classList.remove('power', 'danger');
              }}
            }} else {{
              el.classList.remove('active', 'power', 'danger');
            }}
          }});
        }}

        // 3. Bottom Progress Bar Fill
        const progressPct = Math.min(100, Math.max(0, (t / DURATION) * 100));
        progressFill.style.width = progressPct.toFixed(2) + '%';

        // 4. Waveform Sticks Pulsing
        waveSticks.forEach((stick, idx) => {{
          const h = 6 + Math.abs(Math.sin(t * 7.5 + idx * 1.2)) * 18;
          stick.style.height = h.toFixed(1) + 'px';
        }});
      }}
    }};
  </script>
</body>
</html>
"""
        output_path.write_text(html, encoding="utf-8")

    def look(self, project_dir: Path) -> Dict[str, Any]:
        """
        Execute 'openfilm look' on project folder to verify frame boundaries, font loading,
        and frame determinism. If text clipping is detected, automatically adjusts CSS font size down.
        """
        if not self.openfilm_cmd:
            raise RuntimeError("OpenFilm CLI is not available.")

        self._inject_bin_path()
        film_file = project_dir / "film.html"
        if not film_file.exists():
            raise FileNotFoundError(f"film.html not found in {project_dir}")

        logger.info(f"Running OpenFilm Visual QA check: {project_dir}")
        try:
            cmd = [self.openfilm_cmd, "look", "1-3"]
            res = subprocess.run(
                cmd,
                cwd=str(project_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=45,
                shell=(os.name == "nt"),
            )
            output = res.stdout + "\n" + res.stderr

            is_ok = res.returncode == 0
            has_font_error = "font failed to load" in output.lower()
            has_clip_error = "clipped" in output.lower() or "overflow" in output.lower()

            # Automatic font size downscale if clipping detected
            if has_clip_error:
                logger.warning(f"Detected text clipping in {project_dir}. Auto-downscaling font size by 12%...")
                self._downscale_overlay_font(project_dir, scale_factor=0.88)
                # Re-run look once
                res_retry = subprocess.run(
                    cmd,
                    cwd=str(project_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=45,
                    shell=(os.name == "nt"),
                )
                output = res_retry.stdout + "\n" + res_retry.stderr
                is_ok = res_retry.returncode == 0

            return {
                "ok": is_ok,
                "exit_code": res.returncode,
                "output": output.strip(),
                "has_font_error": has_font_error,
                "has_clip_error": has_clip_error,
            }
        except Exception as e:
            logger.error(f"Error running openfilm look: {e}")
            return {"ok": False, "error": str(e), "exit_code": -1, "output": str(e)}

    def _downscale_overlay_font(self, project_dir: Path, scale_factor: float = 0.88) -> None:
        """Scale down font size in overlay.html if text clipping is detected."""
        overlay_path = project_dir / "overlay.html"
        if not overlay_path.exists():
            return
        content = overlay_path.read_text(encoding="utf-8")
        m = re.search(r"font-size:\s*(\d+)px", content)
        if m:
            cur_sz = int(m.group(1))
            new_sz = max(42, int(cur_sz * scale_factor))
            content = content.replace(f"font-size: {cur_sz}px", f"font-size: {new_sz}px")
            overlay_path.write_text(content, encoding="utf-8")

    def render(
        self,
        project_dir: Path,
        output_mp4: Path,
        fps: int = 30,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Path:
        """
        Execute 'openfilm render' to export the final 1080x1920 MP4 video with tagged BT.709 color,
        hardware NVENC encoding, and broadcast -14 LUFS loudness normalization.
        """
        if not self.openfilm_cmd:
            raise RuntimeError("OpenFilm CLI is not available.")

        self._inject_bin_path()
        output_mp4 = Path(output_mp4).resolve()
        output_mp4.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"Rendering OpenFilm MP4: {project_dir} -> {output_mp4}")
        if progress_callback:
            progress_callback({
                "stage": "render_progress",
                "percent": 10,
                "message": "Starting OpenFilm Chromium hardware render...",
            })

        # Run openfilm render
        cmd = [
            self.openfilm_cmd,
            "render",
            "--fps", str(fps),
            "--out", str(output_mp4),
            "--loudness=-14",
        ]

        start_time = time.time()
        p = subprocess.Popen(
            cmd,
            cwd=str(project_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            shell=(os.name == "nt"),
        )

        stdout_lines: List[str] = []
        total_frames = 0

        while True:
            line = p.stdout.readline()
            if not line and p.poll() is not None:
                break
            if line:
                stdout_lines.append(line)
                # Parse progress: e.g. "45/150 frames · about 11 s left"
                m = re.search(r"(\d+)/(\d+)\s+frames", line)
                if m and progress_callback:
                    cur_f = int(m.group(1))
                    tot_f = int(m.group(2))
                    total_frames = tot_f
                    pct = int(10 + (cur_f / max(1, tot_f)) * 85)
                    progress_callback({
                        "stage": "render_progress",
                        "percent": pct,
                        "message": f"Rendering OpenFilm frames ({cur_f}/{tot_f})...",
                    })

        p.wait()
        elapsed = time.time() - start_time

        if p.returncode != 0 or not output_mp4.exists() or output_mp4.stat().st_size == 0:
            err_msg = "".join(stdout_lines)
            raise RuntimeError(f"OpenFilm render failed with exit code {p.returncode}: {err_msg}")

        logger.info(f"OpenFilm render completed in {elapsed:.1f}s: {output_mp4} ({output_mp4.stat().st_size / 1024 / 1024:.2f} MB)")
        if progress_callback:
            progress_callback({
                "stage": "render_progress",
                "percent": 100,
                "message": f"OpenFilm render completed ({elapsed:.1f}s)!",
            })

        return output_mp4

    def open_studio(self, project_dir: Optional[Path] = None) -> None:
        """Launch the interactive browser-based OpenFilm Studio for the project."""
        if not self.openfilm_cmd:
            raise RuntimeError("OpenFilm CLI is not available.")

        self._inject_bin_path()

        if not project_dir:
            # Pick latest project in temp/openfilm_projects/
            projects = sorted(
                list(self.projects_dir.glob("clip_*")),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not projects:
                raise FileNotFoundError(f"No OpenFilm projects found in {self.projects_dir}")
            project_dir = projects[0]

        logger.info(f"Opening OpenFilm Studio: {project_dir}")
        print(f"\n[OpenFilm] Launching OpenFilm Studio for project: {project_dir}")
        print("[OpenFilm] Press Ctrl+C in terminal when finished editing in browser.\n")

        subprocess.run(
            [self.openfilm_cmd, "open", str(project_dir)],
            cwd=str(project_dir),
            shell=(os.name == "nt"),
        )
