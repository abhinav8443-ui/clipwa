"""
Dynamic word-by-word animated subtitle generator using pysubs2 (Alex Hormozi / OpusClip style).
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pysubs2
from pysubs2 import Alignment, Color, SSAEvent, SSAFile, SSAStyle

from .config import Config


class SubtitleGenerator:
    """Generates engaging ASS subtitle files with word-level highlight animations."""

    THEMES = {
        "hormozi_yellow": {
            "active_color": "&H002BF7&",      # Bright Golden Yellow (BGR in ASS)
            "inactive_color": "&HFFFFFF&",    # Crisp White
            "outline_color": "&H000000&",     # Pure Black Outline
            "font_size": 22,
        },
        "neon_cyan": {
            "active_color": "&HFFFF00&",      # Neon Cyan
            "inactive_color": "&HFFFFFF&",
            "outline_color": "&H000000&",
            "font_size": 22,
        },
        "emerald_green": {
            "active_color": "&H33FF33&",      # Neon Lime/Green
            "inactive_color": "&HFFFFFF&",
            "outline_color": "&H000000&",
            "font_size": 22,
        },
        "hot_pink": {
            "active_color": "&HD214FF&",      # Vivid Pink
            "inactive_color": "&HFFFFFF&",
            "outline_color": "&H000000&",
            "font_size": 22,
        },
        "clean_white": {
            "active_color": "&HFFFFFF&",
            "inactive_color": "&HA0A0A0&",
            "outline_color": "&H000000&",
            "font_size": 22,
        },
    }

    def __init__(
        self,
        theme: str = "hormozi_yellow",
        font_name: str = "Arial Black",
        all_caps: bool = True,
        words_per_line: int = 3,
        position: str = "lower_third",  # "lower_third" or "center"
    ):
        self.theme_name = theme if theme in self.THEMES else "hormozi_yellow"
        self.theme = self.THEMES[self.theme_name]
        self.font_name = font_name
        self.all_caps = all_caps
        self.words_per_line = max(1, min(5, words_per_line))
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
        Timestamps are shifted so that clip_start becomes 0ms.
        """
        subs = SSAFile()
        subs.info["ScriptType"] = "v4.00+"
        subs.info["PlayResX"] = str(Config.TARGET_WIDTH)
        subs.info["PlayResY"] = str(Config.TARGET_HEIGHT)

        # Base style
        style = SSAStyle()
        style.fontname = self.font_name
        style.fontsize = self.theme["font_size"]
        style.bold = True
        style.italic = False
        style.primarycolor = Color(255, 255, 255)
        style.outlinecolor = Color(0, 0, 0)
        style.backcolor = Color(0, 0, 0, 128)
        style.outline = 3.5
        style.shadow = 1.5

        if self.position == "center":
            style.alignment = Alignment.MIDDLE_CENTER
            style.marginv = 0
        else:
            # Lower-third placed above YouTube Shorts bottom action bar
            style.alignment = Alignment.BOTTOM_CENTER
            style.marginv = 380

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
            # Return empty valid ASS file
            subs.save(output_path)
            return output_path

        # Group words into cards of `words_per_line`
        chunks: List[List[Dict[str, Any]]] = []
        for i in range(0, len(clip_words), self.words_per_line):
            chunks.append(clip_words[i : i + self.words_per_line])

        active_col = self.theme["active_color"]
        inactive_col = self.theme["inactive_color"]

        for chunk in chunks:
            chunk_start = chunk[0]["start_ms"]
            chunk_end = chunk[-1]["end_ms"]

            # For each word in this chunk, create a sub-event where that word is highlighted
            for i, active_word in enumerate(chunk):
                w_start = active_word["start_ms"]
                w_end = active_word["end_ms"]

                # Ensure non-zero duration
                if w_end <= w_start:
                    w_end = w_start + 150

                line_parts = []
                for j, other_word in enumerate(chunk):
                    w_text = other_word["word"]
                    if i == j:
                        # Highlighted active word with gentle scale pop
                        line_parts.append(f"{{\\c{active_col}\\fscx108\\fscy108}}{w_text}{{\\c{inactive_col}\\fscx100\\fscy100}}")
                    else:
                        line_parts.append(f"{{\\c{inactive_col}}}{w_text}")

                display_text = " ".join(line_parts)

                event = SSAEvent(
                    start=w_start,
                    end=w_end,
                    text=display_text,
                    style="Default",
                )
                subs.events.append(event)

        subs.save(output_path)
        return output_path
