"""
Dynamic word-by-word animated subtitle generator using pysubs2 (Alex Hormozi / OpusClip style).
Upgraded for 1080x1920 canvas with massive bold fonts (Impact/Arial Black 92px),
thick 7px black outline, 4px drop shadow, and punchy pop-in scale animation.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pysubs2
from pysubs2 import Alignment, Color, SSAEvent, SSAFile, SSAStyle

from .config import Config


class SubtitleGenerator:
    """Generates massive, eye-catching ASS subtitle files with word-level highlight animations."""

    THEMES = {
        "hormozi_yellow": {
            "active_color": "&H0000FFFF&",    # Bright Neon Yellow (BGR in ASS: B=00, G=FF, R=FF)
            "inactive_color": "&H00FFFFFF&",  # Crisp White
            "outline_color": "&H00000000&",   # Pure Black
            "font_size": 92,
        },
        "neon_cyan": {
            "active_color": "&H00FFFF00&",    # Neon Cyan (BGR in ASS)
            "inactive_color": "&H00FFFFFF&",
            "outline_color": "&H00000000&",
            "font_size": 92,
        },
        "emerald_green": {
            "active_color": "&H0033FF33&",    # Neon Lime/Green (BGR in ASS)
            "inactive_color": "&H00FFFFFF&",
            "outline_color": "&H00000000&",
            "font_size": 92,
        },
        "hot_pink": {
            "active_color": "&H00D214FF&",    # Vivid Pink
            "inactive_color": "&H00FFFFFF&",
            "outline_color": "&H00000000&",
            "font_size": 92,
        },
        "clean_white": {
            "active_color": "&H00FFFFFF&",
            "inactive_color": "&H00A0A0A0&",
            "outline_color": "&H00000000&",
            "font_size": 92,
        },
    }

    def __init__(
        self,
        theme: str = "hormozi_yellow",
        font_name: str = "Impact",
        all_caps: bool = True,
        words_per_line: int = 3,
        position: str = "lower_third",
    ):
        self.theme_name = theme if theme in self.THEMES else "hormozi_yellow"
        self.theme = self.THEMES[self.theme_name]
        self.font_name = font_name or "Impact"
        self.all_caps = all_caps
        self.words_per_line = max(1, min(3, words_per_line))  # Tight 2-3 words max per card
        self.position = position

    def generate_ass(
        self,
        words: List[Dict[str, Any]],
        output_path: str,
        clip_start: float = 0.0,
        clip_end: Optional[float] = None,
    ) -> str:
        """
        Generate ASS subtitle file for a specific time window [clip_start, clip_end].
        Explicitly configured for 1080x1920 PlayRes with massive size-92 font,
        thick 7.0 outline, 4.0 shadow, and punchy pop-in animation tag.
        """
        subs = SSAFile()
        subs.info["ScriptType"] = "v4.00+"
        # Explicit PlayRes (1080x1920 standard 9:16 Shorts) so font sizes scale accurately
        target_w = Config.TARGET_WIDTH
        target_h = Config.TARGET_HEIGHT
        subs.info["PlayResX"] = str(target_w)
        subs.info["PlayResY"] = str(target_h)

        # Base style: Massive Hormozi styling
        style = SSAStyle()
        style.fontname = self.font_name
        style.fontsize = float(self.theme["font_size"])  # 92.0
        style.bold = True
        style.italic = False
        style.primarycolor = Color(255, 255, 255)
        style.outlinecolor = Color(0, 0, 0)
        style.backcolor = Color(0, 0, 0, 160)
        style.outline = 7.0   # Deep, crisp black border
        style.shadow = 4.0    # Solid drop shadow for maximum readability
        style.spacing = 1.0

        if self.position == "center":
            style.alignment = Alignment.MIDDLE_CENTER
            style.marginv = 0
        else:
            # Scaled margin for 1080x1920 canvas (480px from bottom sits right in the lower third, above YouTube UI overlay)
            style.alignment = Alignment.BOTTOM_CENTER
            style.marginv = 480 if target_h >= 1920 else int(target_h * 0.16)

        subs.styles["Default"] = style

        # Filter words within clip window
        clip_words = []
        for w in words:
            w_start = w.get("start", 0.0)
            w_end = w.get("end", 0.0)
            if clip_end is not None and w_start >= clip_end:
                continue
            if w_end <= clip_start:
                continue
            # Shift relative to clip start
            rel_start = max(0.0, w_start - clip_start)
            rel_end = (w_end - clip_start) if clip_end is None else min(clip_end - clip_start, w_end - clip_start)
            if rel_end > rel_start:
                text = w.get("word", "").strip()
                if self.all_caps:
                    text = text.upper()
                clip_words.append({
                    "word": text,
                    "start_ms": int(rel_start * 1000),
                    "end_ms": int(rel_end * 1000),
                })

        if not clip_words:
            subs.save(output_path)
            return output_path

        # Group words into tight cards of 2 to 3 words
        chunks: List[List[Dict[str, Any]]] = []
        for i in range(0, len(clip_words), self.words_per_line):
            chunks.append(clip_words[i : i + self.words_per_line])

        active_col = self.theme["active_color"]
        inactive_col = self.theme["inactive_color"]

        # Pop-in scale animation tag: punches in from 115% to 100% in 120ms
        pop_anim = r"{\fscx115\fscy115\t(0,120,\fscx100\fscy100)}"

        for chunk in chunks:
            # For each word in this chunk, create a sub-event where that word is highlighted
            for i, active_word in enumerate(chunk):
                w_start = active_word["start_ms"]
                w_end = active_word["end_ms"]

                if w_end <= w_start:
                    w_end = w_start + 150

                line_parts = []
                for j, other_word in enumerate(chunk):
                    w_text = other_word["word"]
                    if i == j:
                        # Spoken active word highlighted with slight extra emphasis
                        line_parts.append(f"{{\\c{active_col}\\fscx106\\fscy106}}{w_text}{{\\c{inactive_col}\\fscx100\\fscy100}}")
                    else:
                        line_parts.append(f"{{\\c{inactive_col}}}{w_text}")

                display_text = f"{pop_anim} " + " ".join(line_parts)

                event = SSAEvent(
                    start=w_start,
                    end=w_end,
                    text=display_text,
                    style="Default",
                )
                subs.events.append(event)

        subs.save(output_path)
        return output_path
