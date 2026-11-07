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
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

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
        styles_dir: Optional[Path] = None,
    ):
        self.temp_dir = Path(temp_dir or Config.TEMP_DIR)
        self.output_dir = Path(output_dir or Config.OUTPUT_DIR)
        self.bin_dir = Path(bin_dir or (Config.BASE_DIR / "bin"))
        self.styles_dir = Path(styles_dir or (Config.BASE_DIR / "assets" / "styles"))
        self.styles_dir.mkdir(parents=True, exist_ok=True)
        self.projects_dir = self.temp_dir / "openfilm_projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)

        # Inject local ffmpeg/ffprobe into PATH so openfilm CLI detects them
        self._inject_bin_path()

        # Locate openfilm executable
        self.openfilm_cmd = self._find_openfilm_cli()

    def get_style(self, style: Union[str, Dict[str, Any], None] = "pro_cinematic_reel") -> Dict[str, Any]:
        """Load and resolve an OpenFilm Style Blueprint by name or dictionary."""
        from .style_cloner import PRO_CINEMATIC_REEL_BLUEPRINT, FORSETH_AI_BLUEPRINT
        if isinstance(style, dict):
            merged = dict(PRO_CINEMATIC_REEL_BLUEPRINT)
            merged.update(style)
            return merged

        style_name = str(style or "pro_cinematic_reel").strip().lower()
        candidates = [
            self.styles_dir / f"{style_name}.json",
            self.styles_dir / "pro_cinematic_reel.json",
            self.styles_dir / "forseth_ai.json",
        ]
        for c in candidates:
            if c.exists():
                try:
                    data = json.loads(c.read_text(encoding="utf-8"))
                    merged = dict(PRO_CINEMATIC_REEL_BLUEPRINT)
                    merged.update(data)
                    return merged
                except Exception as e:
                    logger.warning(f"Error reading style blueprint {c}: {e}")

        return dict(PRO_CINEMATIC_REEL_BLUEPRINT)

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
        style: Union[str, Dict[str, Any], None] = "pro_cinematic_reel",
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

        style_data = self.get_style(style)

        # 1. Copy or link media assets into assets/
        src_video = Path(video_path).resolve()
        target_video = assets_dir / "clip.mp4"
        if src_video.resolve() != target_video.resolve():
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
                if Path(hook_sfx_path).resolve() != target_sfx.resolve():
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
                    if Path(bg_tune_path).resolve() != target_bg.resolve():
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

        # 4. Generate overlay.html (clean bespoke cinematic typography, no HUD clutter)
        overlay_html_path = project_dir / "overlay.html"
        self._write_overlay_html(
            output_path=overlay_html_path,
            duration=duration,
            phrases=phrases,
            font_size_px=font_size_px,
            style_data=style_data,
            hook_title=clean_hook,
        )

        # 5. Generate film.html (Multi-Track OpenFilm Stage)
        film_html_path = project_dir / "film.html"
        self._write_film_html(
            output_path=film_html_path,
            duration=duration,
            clip_actual_dur=clip_actual_dur,
            has_hook_sfx=has_hook_sfx,
            sfx_actual_dur=sfx_actual_dur,
            has_bg_music=has_bg_music,
            bg_actual_dur=bg_actual_dur,
            style_data=style_data,
        )

        logger.info(f"OpenFilm project generated at: {project_dir} (style: {style_data.get('style_name', 'forseth_ai')})")
        return project_dir

    def _write_film_html(
        self,
        output_path: Path,
        duration: float,
        clip_actual_dur: float,
        has_hook_sfx: bool,
        sfx_actual_dur: float,
        has_bg_music: bool,
        bg_actual_dur: float,
        style_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Write film.html cutting together tracks:
        - Track 1: Cinematic Color Grading & Editorial Kinetic Typography (overlay.html)
        - Track 2: Primary Foreground Video with Voice Audio (assets/clip.mp4)
        - Track 3: Ambient Blurred Background Video (muted)
        - Track 4: Audio Stems (Hook SFX & Rotated Viral Music)
        """
        sd = style_data or {}
        cg = sd.get("color_grade", {})
        fm = sd.get("framing_and_motion", {})

        fg_filter = cg.get("css_filter", "contrast(1.15) saturate(1.06) brightness(0.93)")
        bg_blur = cg.get("bg_blur_px", 50)
        bg_bright = cg.get("bg_brightness", 0.18)
        bg_sat = cg.get("bg_saturate", 0.90)
        bg_scale = cg.get("bg_scale", 1.10)
        fg_radius = fm.get("fg_border_radius_px", 18)
        fg_shadow = fm.get("fg_box_shadow", "0 25px 60px rgba(0, 0, 0, 0.85)")
        fg_top = fm.get("fg_video_top_px", 460)
        fg_height = fm.get("fg_video_height_px", 1000)

        # Subtle handheld float & slow cinematic push-in on central video
        fg_animation = "animation: cinematicPushFloat 8.5s cubic-bezier(0.25, 1, 0.5, 1) infinite;" if fm.get("camera_zoom_enabled", True) else ""

        # Clamp video duration strictly to actual file duration - 0.02s
        if clip_actual_dur > 0:
            v_end = min(duration, clip_actual_dur - 0.02)
        else:
            v_end = duration
        v_dur_str = f"{v_end:.3f}"

        audio_tracks: List[str] = []
        if has_hook_sfx:
            audio_tracks.append(
                '  <section>\n    <audio id="hook_sfx" src="assets/hook_sfx.wav" volume="0.45"></audio>\n  </section>'
            )
        if has_bg_music:
            if bg_actual_dur > 0:
                bg_end = min(duration, bg_actual_dur - 0.05)
            else:
                bg_end = duration
            audio_tracks.append(
                f'  <section>\n    <audio id="bg_music" src="assets/bg_music.wav#t=0,{bg_end:.3f}" volume="0.12"></audio>\n  </section>'
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
    @keyframes cinematicPushFloat {{
      0% {{
        scale: 1.0;
        translate: 0px 0px;
      }}
      25% {{
        scale: 1.025;
        translate: 1.8px -1.2px;
      }}
      50% {{
        scale: 1.055;
        translate: -1.2px 1.5px;
      }}
      75% {{
        scale: 1.075;
        translate: 1.5px 0.8px;
      }}
      95% {{
        scale: 1.09;
        translate: -0.8px -1.0px;
      }}
      100% {{
        scale: 1.0;
        translate: 0px 0px;
      }}
    }}
    .bg-video {{
      object-fit: cover;
      filter: blur({bg_blur}px) brightness({bg_bright}) saturate({bg_sat});
      scale: {bg_scale};
    }}
    .fg-video {{
      object-fit: contain;
      border-radius: {fg_radius}px;
      box-shadow: {fg_shadow};
      filter: {fg_filter};
      {fg_animation}
    }}
  </style>
</head>
<body>
  <!-- Track 1: Cinematic Color Grading & Editorial Kinetic Typography -->
  <section>
    <iframe id="overlay" src="overlay.html" at="0" style="left: 0; top: 0; width: 1080px; height: 1920px"></iframe>
  </section>

  <!-- Track 2: Primary Foreground Video with Voice Audio (volume=1.0) -->
  <section>
    <video id="fg_video" src="assets/clip.mp4#t=0,{v_dur_str}" volume="1.0" class="fg-video"
           style="left: 0; top: {fg_top}px; width: 1080px; height: {fg_height}px"></video>
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
        phrases: List[Dict[str, Any]],
        font_size_px: int,
        style_data: Optional[Dict[str, Any]] = None,
        hook_title: str = "",
    ) -> None:
        """
        Write overlay.html containing deterministic frame(t) logic for:
        1. 100% Clean Bespoke Human-Editor Cinema Aesthetic (NO top banners, NO progress bars).
        2. Editorial kinetic typography with buttery 110ms motion-blur entrance.
        3. 120ms optical exposure pulse / flash on sentence cuts.
        4. Pure negative space lighting: full-frame cinematic vignette and film grain.
        """
        sd = style_data or {}
        cs = sd.get("caption_style", {})
        cg = sd.get("color_grade", {})
        fm = sd.get("framing_and_motion", {})

        font_family = cs.get("font_family", "Montserrat, 'Plus Jakarta Sans', 'Inter', -apple-system, sans-serif")
        font_sz = font_size_px or cs.get("font_size_px", 66)
        color_default = cs.get("color_default", "#F8F9FA")
        color_active = cs.get("color_active", "#FFD60A")
        color_power = cs.get("color_power", "#FFFFFF")
        active_scale = cs.get("active_word_scale", 1.06)
        letter_spacing = cs.get("letter_spacing_em", "-0.025em")
        top_px = cs.get("top_px", 1300)
        safe_width = cs.get("safe_width_px", 840)
        safe_left = cs.get("safe_left_px", 120)

        vignette_radial = cg.get("vignette_radial", "radial-gradient(ellipse at center, rgba(0,0,0,0) 50%, rgba(0,0,0,0.55) 100%)")
        bottom_gradient = cg.get("bottom_contrast_gradient", "linear-gradient(180deg, rgba(0,0,0,0.15) 0%, rgba(0,0,0,0) 35%, rgba(0,0,0,0.65) 82%, rgba(0,0,0,0.88) 100%)")
        grain_opacity = cg.get("cinematic_film_grain_opacity", 0.035)

        phrases_json = json.dumps(phrases, ensure_ascii=False)
        power_words_json = json.dumps(list(POWER_WORDS))

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
      font-family: {font_family};
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
      background: {vignette_radial};
      pointer-events: none;
      z-index: 1;
    }}

    /* Full-Height Atmospheric Gradient for Seamless Negative Space Lighting */
    #bottom-contrast {{
      position: absolute;
      top: 0;
      left: 0;
      width: 1080px;
      height: 1920px;
      background: {bottom_gradient};
      pointer-events: none;
      z-index: 2;
    }}

    /* Subtle Cinematic Film Grain Overlay */
    #film-grain {{
      position: absolute;
      top: 0;
      left: 0;
      width: 1080px;
      height: 1920px;
      pointer-events: none;
      z-index: 3;
      opacity: {grain_opacity};
      background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noiseFilter'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.8' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noiseFilter)'/%3E%3C/svg%3E");
    }}

    /* Subtle 120ms Optical Exposure Flash on Sentence Cuts */
    #optical-pulse {{
      position: absolute;
      top: 0;
      left: 0;
      width: 1080px;
      height: 1920px;
      pointer-events: none;
      background: rgba(255, 255, 255, 0.08);
      opacity: 0;
      mix-blend-mode: overlay;
      z-index: 4;
    }}

    /* Editorial Lower-Third Typography Sweet Spot (strictly within central {safe_width}px safe width) */
    #subtitles-container {{
      position: absolute;
      top: {top_px}px;
      left: {safe_left}px;
      width: {safe_width}px;
      min-height: 180px;
      display: flex;
      flex-wrap: wrap;
      justify-content: center;
      align-items: center;
      gap: 14px 18px;
      text-align: center;
      z-index: 20;
      opacity: 0;
      will-change: transform, opacity, filter;
    }}

    .word {{
      display: inline-block;
      font-size: {font_sz}px;
      font-weight: 800;
      text-transform: uppercase;
      line-height: 1.15;
      letter-spacing: {letter_spacing};
      color: {color_default};
      text-shadow: 0 4px 18px rgba(0, 0, 0, 0.9), 0 1px 4px rgba(0, 0, 0, 0.95);
      transform-origin: center center;
      transition: color 0.06s ease, transform 0.08s ease, text-shadow 0.08s ease;
    }}

    /* Active Spoken Impact Word: Warm Gold with Soft Glow */
    .word.active {{
      color: {color_active} !important;
      transform: scale({active_scale});
      text-shadow: 0 4px 20px rgba(0, 0, 0, 0.9), 0 0 22px rgba(255, 214, 10, 0.65) !important;
    }}

    /* Power Words: Clean Crisp Icy Glow */
    .word.active.power {{
      color: {color_power} !important;
      transform: scale(1.08);
      text-shadow: 0 4px 20px rgba(0, 0, 0, 0.95), 0 0 22px rgba(255, 255, 255, 0.75) !important;
    }}
  </style>
</head>
<body>
  <div id="vignette"></div>
  <div id="bottom-contrast"></div>
  <div id="film-grain"></div>
  <div id="optical-pulse"></div>

  <!-- Editorial Kinetic Typography Container -->
  <div id="subtitles-container"></div>

  <script>
    const DURATION = {duration};
    const phrases = {phrases_json};
    const powerWords = new Set({power_words_json});

    const subContainer = document.getElementById('subtitles-container');
    const pulseEl = document.getElementById('optical-pulse');

    let cachedPhraseIdx = -1;

    function cleanWordText(raw) {{
      return raw.replace(/[^a-zA-Z0-9]/g, '').toLowerCase();
    }}

    // OpenFilm Protocol 0.1 frame(t) Implementation
    window.film = {{
      duration: DURATION,
      width: 1080,
      height: 1920,
      frame(t) {{
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
          subContainer.style.opacity = '0';
          cachedPhraseIdx = -1;
          if (pulseEl) pulseEl.style.opacity = '0';
          return;
        }}

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

        // Buttery 110ms motion-blur word reveal entrance
        const phraseAge = t - currentPhrase.start;
        if (phraseAge >= 0 && phraseAge < 0.11) {{
          const p = Math.max(0, phraseAge / 0.11);
          const ty = (1 - p) * 10;
          const blurPx = (1 - p) * 4;
          subContainer.style.opacity = p.toFixed(3);
          subContainer.style.transform = `translate3d(0, ${{ty.toFixed(1)}}px, 0)`;
          subContainer.style.filter = `blur(${{blurPx.toFixed(1)}}px)`;

          // Subtle optical exposure pulse (110ms)
          if (pulseEl) {{
            pulseEl.style.opacity = ((1 - p) * 0.85).toFixed(3);
          }}
        }} else {{
          subContainer.style.opacity = '1';
          subContainer.style.transform = 'translate3d(0, 0, 0)';
          subContainer.style.filter = 'none';
          if (pulseEl) pulseEl.style.opacity = '0';
        }}

        // Active spoken word highlight
        currentPhrase.words.forEach((w, wIdx) => {{
          const el = document.getElementById(`w_${{wIdx}}`);
          if (!el) return;
          const isSpoken = (t >= w.start && t <= w.end);
          if (isSpoken) {{
            el.classList.add('active');
            const cl = cleanWordText(w.word);
            if (powerWords.has(cl)) {{
              el.classList.add('power');
            }} else {{
              el.classList.remove('power');
            }}
          }} else {{
            el.classList.remove('active', 'power');
          }}
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
