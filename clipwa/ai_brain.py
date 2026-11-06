"""
AI Brain for value-driven, motivational viral moment detection and metadata generation.
Prioritizes high-impact resilience stories, 'never give up' mentality, overcoming adversity,
and actionable life lessons.
Supports Google GenAI (Gemini 2.5 Flash), Groq, Ollama, Zero-API-key Free LLMs (g4f & Pollinations),
and Smart Algorithmic Heuristic fallback with full-video chronological sampling and strict deduplication.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import urllib.request
import urllib.error

from .config import Config, atomic_write_json


def _sanitize_youtube_text(text: str) -> str:
    """
    Sanitizes text for YouTube titles, descriptions, and metadata.
    YouTube strictly forbids angle brackets ('<' and '>') and control characters.
    Also strips leading '>>' speaker markers often found in auto-captions.
    """
    if not text:
        return ""
    # 1. Strip leading speaker markers like '>>', '>>>', '>> '
    cleaned = re.sub(r"^\s*>>+\s*", "", text)
    # 2. Strip all '<' and '>' characters strictly forbidden by YouTube
    cleaned = cleaned.replace("<", "").replace(">", "")
    # 3. Strip control characters (retain normal newlines/whitespace)
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", cleaned)
    return cleaned.strip()


class ViralMomentDetector:
    """Identifies top retention, high-value, and inspirational clips from transcripts."""

    MOTIVATIONAL_KEYWORDS = {
        "never give up": 30,
        "keep going": 25,
        "quit": 22,
        "pain": 22,
        "discipline": 22,
        "mindset": 22,
        "failure": 20,
        "struggle": 20,
        "hard work": 20,
        "lesson": 18,
        "realized": 18,
        "purpose": 18,
        "overcome": 22,
        "sacrifice": 22,
        "believe": 18,
        "pushed": 18,
        "limits": 20,
        "suffer": 22,
        "suffering": 22,
        "darkest": 22,
        "proud": 18,
        "advice": 16,
        "focus": 16,
        "goggins": 22,
        "unbreakable": 25,
        "endurance": 20,
        "mental": 18,
        "impossible": 20,
        "strength": 18,
        "fear": 18,
        "dream": 15,
        "action": 16,
        "consistency": 18,
        "grind": 18,
        "habit": 16,
        "truth": 16,
        "worst": 15,
        "breakdown": 18,
        "breakthrough": 22,
        "champion": 18,
        "compete": 16,
    }

    VIRAL_KEYWORDS = set(MOTIVATIONAL_KEYWORDS.keys()).union({
        "secret", "insane", "crazy", "shocking", "unbelievable", "never", "always",
        "mistake", "warning", "truth", "money", "billion", "future", "genius",
        "hidden", "hack", "trick", "rule", "destroy", "win", "stop", "change",
        "danger", "best", "powerful", "huge", "revolutionary", "running", "treadmill"
    })

    def __init__(
        self,
        provider: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.provider = provider or Config.DEFAULT_LLM_PROVIDER
        self.api_key = api_key or Config.GEMINI_API_KEY
        self.model = model or Config.DEFAULT_GEMINI_MODEL

    def clean_and_validate_title(self, raw_title: Optional[str], fallback_dialogue: str = "") -> str:
        """
        Cleans and validates a YouTube Shorts title:
        1. Strips existing '#Shorts' or '#shorts' tag.
        2. Strips markdown symbols: '#', '*', '_', '>', '<', '`', '~', '|'.
        3. Strips excessive whitespace and surrounding quotes.
        4. Validation check: If cleaned title has fewer than 3 alphabetic characters (e.g. '####', '...', empty),
           automatically falls back to generating a clean, punchy motivational title from fallback_dialogue keywords.
        5. Appends ' #Shorts' and ensures length <= 70 characters.
        """
        text = str(raw_title or "").strip()
        # Remove any existing '#shorts' or '#Shorts'
        text = re.sub(r"#shorts\b", "", text, flags=re.IGNORECASE).strip()

        # Strip markdown symbols and formatting
        cleaned = re.sub(r"[#*_><`~|]+", " ", text)
        cleaned = re.sub(r"^\s*>>+\s*", "", cleaned)
        cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip().strip('"\'“”‘’')

        # Count alphabetic characters
        alpha_chars = re.findall(r"[a-zA-Z]", cleaned)
        if len(alpha_chars) < 4:
            # Fall back to generating from transcript dialogue keywords or hook
            fallback_title = self._smart_heuristic_title(fallback_dialogue)
            cleaned_fb = re.sub(r"#shorts\b", "", fallback_title, flags=re.IGNORECASE).strip()
            cleaned_fb = re.sub(r"[#*_><`~|]+", " ", cleaned_fb)
            cleaned_fb = re.sub(r"\s+", " ", cleaned_fb).strip().strip('"\'“”‘’')
            if len(re.findall(r"[a-zA-Z]", cleaned_fb)) >= 4:
                cleaned = cleaned_fb
            elif fallback_dialogue and len(re.findall(r"[a-zA-Z]", fallback_dialogue)) >= 4:
                first_phrase = re.split(r"[.!?\n]", fallback_dialogue)[0].strip()
                cleaned = re.sub(r"[#*_><`~|]+", " ", first_phrase)[:50].strip()
            else:
                cleaned = "The Mindset That Changes Everything 🔥"

        # Limit base title to 55 chars so with ' #Shorts' it stays under 65 chars
        cleaned = cleaned[:55].strip()
        return _sanitize_youtube_text(f"{cleaned} #Shorts")

    def detect_moments(
        self,
        transcript_data: Dict[str, Any],
        max_clips: int = 3,
        min_duration: float = 35.0,
        max_duration: float = 58.0,
    ) -> List[Dict[str, Any]]:
        """
        Analyze transcript and return list of top value-driven, motivational moment dictionaries.
        Guarantees non-overlapping, chronologically dispersed clips (at least 120s apart).
        """
        segments = transcript_data.get("segments", [])
        total_duration = float(transcript_data.get("duration", 0) or 0)
        if not total_duration and segments:
            total_duration = float(segments[-1].get("end", 0))

        if not segments:
            return []

        # If total video is already shorter than max_duration, return entire video as 1 clip
        if total_duration <= max_duration:
            text = transcript_data.get("full_text", "")
            title = self.generate_viral_title(text)
            return [{
                "clip_id": 1,
                "start_time": 0.0,
                "end_time": round(total_duration, 2),
                "duration": round(total_duration, 2),
                "virality_score": 95,
                "hook_text": title,
                "reason": "Complete concise video segment",
                "youtube_title": title,
                "youtube_description": f"{title}\n\n#Shorts #Motivation #Viral",
                "tags": ["shorts", "motivation", "nevergiveup", "viral"],
            }]

        # Prepare full-video representative chronological sample
        sampled_transcript = self._sample_transcript_across_video(
            segments=segments,
            total_duration=total_duration,
            target_char_limit=24000,
        )

        candidates: List[Dict[str, Any]] = []

        # 1. Attempt Gemini if API key is provided
        if self.provider == "gemini" and self.api_key:
            try:
                candidates = self._detect_gemini(sampled_transcript, max_clips * 3, min_duration, max_duration)
            except Exception as e:
                print(f"[ViralMomentDetector] Gemini API notice ({e}), checking alternative providers...")

        # 2. Attempt Groq if API key is provided
        if not candidates and Config.GROQ_API_KEY:
            try:
                candidates = self._detect_groq(sampled_transcript, max_clips * 3, min_duration, max_duration)
            except Exception as e:
                print(f"[ViralMomentDetector] Groq API notice ({e}), checking alternative providers...")

        # 3. Check local Ollama
        if not candidates:
            try:
                candidates = self._detect_ollama(sampled_transcript, max_clips * 3, min_duration, max_duration)
            except Exception:
                pass

        # 4. Zero-Config Free Keyless LLM (g4f & Pollinations.ai)
        if not candidates:
            try:
                candidates = self._detect_free_llm(sampled_transcript, max_clips * 3, min_duration, max_duration)
                if candidates:
                    print(f"[ViralMomentDetector] Successfully generated candidates using free keyless AI!")
            except Exception as e:
                print(f"[ViralMomentDetector] Free online LLM notice: {e}")

        # 5. Smart Algorithmic Heuristic fallback
        if not candidates:
            print("[ViralMomentDetector] Using smart algorithmic motivational detector (100% local & free)...")
            candidates = self._detect_heuristic(
                segments=segments,
                total_duration=total_duration,
                max_clips=max_clips * 3,
                min_dur=min_duration,
                max_dur=max_duration,
            )

        # 6. Strict Non-Overlapping Deduplication & Boundary Snapping
        refined_clips = self._deduplicate_and_refine_clips(
            raw_clips=candidates,
            segments=segments,
            total_duration=total_duration,
            max_clips=max_clips,
            min_dur=min_duration,
            max_dur=max_duration,
            min_distance_sec=120.0,
        )

        return refined_clips

    def _sample_transcript_across_video(
        self,
        segments: List[Dict[str, Any]],
        total_duration: float,
        target_char_limit: int = 24000,
    ) -> str:
        """
        Samples representative conversation windows across the entire duration of the podcast
        (beginning, middle, and end) with explicit timestamps in seconds.
        Skips cold intro / host sponsorship reads (first 35s of long videos).
        """
        if not segments:
            return ""

        # Filter out cold intro for long videos (>10 mins)
        min_start = 35.0 if total_duration > 600 else 0.0
        usable_segments = [s for s in segments if s.get("start", 0) >= min_start]
        if not usable_segments:
            usable_segments = segments

        total_text = "\n".join(
            f"[{s['start']:.1f}s - {s['end']:.1f}s] {s['text']}" for s in usable_segments
        )

        if len(total_text) <= target_char_limit:
            return total_text

        # Divide podcast into 8 to 12 evenly spaced chronological blocks across the full duration
        num_blocks = 10
        sampled_blocks: List[str] = []
        chars_per_block = target_char_limit // num_blocks

        duration_span = total_duration - min_start
        block_dur = duration_span / num_blocks

        for b in range(num_blocks):
            b_start = min_start + (b * block_dur)
            b_end = b_start + block_dur
            b_segs = [s for s in usable_segments if b_start <= s.get("start", 0) < b_end]
            if not b_segs:
                continue

            block_lines = [f"[{s['start']:.1f}s - {s['end']:.1f}s] {s['text']}" for s in b_segs]
            block_text = "\n".join(block_lines)

            if len(block_text) > chars_per_block:
                # Find best continuous window within this block with highest motivational keywords
                best_sub = block_text[:chars_per_block]
                best_kw = 0
                step = max(500, chars_per_block // 3)
                for offset in range(0, len(block_text) - chars_per_block + 1, step):
                    chunk = block_text[offset : offset + chars_per_block]
                    kw_score = sum(weight for kw, weight in self.MOTIVATIONAL_KEYWORDS.items() if kw in chunk.lower())
                    if kw_score > best_kw:
                        best_kw = kw_score
                        best_sub = chunk
                sampled_blocks.append(f"--- [PART {b+1}/{num_blocks} | Time {int(b_start)}s - {int(b_end)}s] ---\n{best_sub}")
            else:
                sampled_blocks.append(f"--- [PART {b+1}/{num_blocks} | Time {int(b_start)}s - {int(b_end)}s] ---\n{block_text}")

        return "\n\n".join(sampled_blocks)

    def _build_prompt(self, transcript: str, max_clips: int, min_dur: float, max_dur: float) -> str:
        return f"""You are a world-class viral video editor and inspirational storytelling expert (Alex Hormozi, David Goggins, Ryan Holiday style).

Your task: Select the top {max_clips} MOST POWERFUL, VALUE-DRIVEN, and MOTIVATIONAL moments from the transcript below.

DO NOT pick random banter, small talk, or generic questions. Focus strictly on moments that deliver REAL LIFE VALUE, DEEP MOTIVATION, and the "NEVER GIVE UP" MENTALITY.

CORE THEMES TO TARGET:
1. Overcoming Adversity & Pain: Being pushed to the absolute limit in life, business, or endurance and refusing to quit.
2. "Never Give Up" Mentality: Raw stories of resilience, self-belief, discipline, grit, overcoming self-doubt, or embracing the struggle.
3. Actionable Life Wisdom: Hard-earned life lessons, counterintuitive mindset shifts, or practical advice that genuinely inspires and elevates the viewer.

CRITICAL SHORT-FORM NARRATIVE STRUCTURE (35s to 58s):
Every clip MUST have a complete, satisfying story arc:
- [0s - 5s] The Hook: A bold statement, shocking realization, or intriguing question.
- [5s - 40s] The Meat / Struggle: The real battle, story of endurance, or core mindset explanation.
- [40s - 58s] The Payoff / Lesson: A powerful concluding punchline, realization, or life lesson that leaves the viewer deeply inspired.

CRITICAL TIMESTAMP RULES:
- Each clip duration MUST be between {min_dur:.0f} and {max_dur:.0f} seconds (ideal sweet spot: 40s - 55s).
- NEVER cut mid-sentence! Both start_time and end_time MUST align with complete sentences.
- SPREAD ACROSS THE VIDEO: Select clips from completely DIFFERENT parts of the video! Every clip's start_time MUST be at least 120 seconds away from any other clip.

TITLES:
- Write high-CTR, punchy, inspirational YouTube Shorts titles (under 60 characters, MUST end with '#Shorts'). E.g., 'Why Giving Up Is NOT An Option 🔥 #Shorts', 'The Mindset That Beats Extreme Pain 🧠 #Shorts'.

Return a STRICT JSON ARRAY ONLY (no other text, no markdown preamble):
[
  {{
    "clip_id": 1,
    "start_time": 1420.5,
    "end_time": 1472.0,
    "virality_score": 98,
    "hook_text": "The exact opening quote...",
    "reason": "Why this moment provides deep value or powerful inspiration",
    "youtube_title": "The Brutal Truth About Discipline #Shorts",
    "youtube_description": "Powerful 1-2 sentence description with hashtags #Shorts #Motivation #Discipline #Mindset",
    "tags": ["shorts", "motivation", "mindset", "nevergiveup", "discipline", "viral"]
  }}
]

Transcript:
{transcript}
"""

    def _detect_free_llm(
        self, transcript: str, max_clips: int, min_dur: float, max_dur: float
    ) -> List[Dict[str, Any]]:
        """Zero-API-key free LLM moment detection using g4f or pollinations.ai."""
        sample_transcript = transcript[:22000]
        prompt = self._build_prompt(sample_transcript, max_clips, min_dur, max_dur)

        # 1. Try g4f client
        try:
            from g4f.client import Client
            client = Client()
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are an elite viral video editor. Return ONLY a valid JSON array."},
                    {"role": "user", "content": prompt}
                ]
            )
            raw = resp.choices[0].message.content
            parsed = self._parse_json_response(raw, max_clips, min_dur, max_dur)
            if parsed:
                return parsed
        except Exception:
            pass

        # 2. Try text.pollinations.ai
        try:
            import requests
            r = requests.post(
                "https://text.pollinations.ai/",
                json={
                    "messages": [
                        {"role": "system", "content": "You are an elite YouTube Shorts editor. Output ONLY a valid JSON array."},
                        {"role": "user", "content": prompt}
                    ],
                    "model": "openai",
                    "jsonMode": True
                },
                timeout=25
            )
            if r.status_code == 200 and r.text:
                parsed = self._parse_json_response(r.text, max_clips, min_dur, max_dur)
                if parsed:
                    return parsed
        except Exception:
            pass

        return []

    def _detect_gemini(
        self, transcript: str, max_clips: int, min_dur: float, max_dur: float
    ) -> List[Dict[str, Any]]:
        """Uses Google GenAI SDK with Gemini 2.5 Flash."""
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=self.api_key)
            prompt = self._build_prompt(transcript, max_clips, min_dur, max_dur)

            response = client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.3,
                ),
            )
            return self._parse_json_response(response.text, max_clips, min_dur, max_dur)
        except Exception as e:
            raise RuntimeError(f"Gemini API generation error: {e}")

    def _detect_groq(
        self, transcript: str, max_clips: int, min_dur: float, max_dur: float
    ) -> List[Dict[str, Any]]:
        """Uses Groq API for rapid detection."""
        prompt = self._build_prompt(transcript, max_clips, min_dur, max_dur)
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {Config.GROQ_API_KEY}",
            "Content-Type": "application/json",
        }
        data = {
            "model": "llama-3.3-70b-versatile",
            "messages": [
                {"role": "system", "content": "You are a professional video editor. Output valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }

        req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            res_body = json.loads(resp.read().decode("utf-8"))
            content = res_body["choices"][0]["message"]["content"]
            return self._parse_json_response(content, max_clips, min_dur, max_dur)

    def _detect_ollama(
        self, transcript: str, max_clips: int, min_dur: float, max_dur: float
    ) -> List[Dict[str, Any]]:
        """Uses local Ollama if available."""
        prompt = self._build_prompt(transcript, max_clips, min_dur, max_dur)
        url = f"{Config.OLLAMA_BASE_URL.rstrip('/')}/api/generate"
        data = {
            "model": "llama3.2",
            "prompt": prompt,
            "stream": False,
            "format": "json",
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return self._parse_json_response(body.get("response", ""), max_clips, min_dur, max_dur)

    # -------------------------------------------------------------------------
    # Sentence Boundary Snapping & Strict Deduplication
    # -------------------------------------------------------------------------

    def _snap_to_clean_sentence(
        self,
        raw_start: float,
        raw_end: float,
        segments: List[Dict[str, Any]],
        min_dur: float = 35.0,
        max_dur: float = 58.0,
    ) -> Tuple[float, float]:
        """
        Snaps start_time and end_time to clean sentence boundaries
        so the speaker is never cut mid-sentence.
        """
        if not segments:
            return raw_start, raw_end

        # 1. Find segment closest to raw_start
        start_idx = 0
        min_start_diff = float("inf")
        for i, s in enumerate(segments):
            diff = abs(s.get("start", 0) - raw_start)
            if diff < min_start_diff:
                min_start_diff = diff
                start_idx = i

        snapped_start = segments[start_idx].get("start", raw_start)

        # 2. Find clean closing segment within [min_dur, max_dur]
        snapped_end = min(snapped_start + max_dur, raw_end)
        best_end = None
        best_punc_diff = float("inf")

        ideal_target = snapped_start + 48.0  # Sweet spot for retention

        for j in range(start_idx, len(segments)):
            seg = segments[j]
            seg_end = seg.get("end", 0)
            dur = seg_end - snapped_start

            if dur < min_dur:
                continue
            if dur > max_dur:
                break

            seg_text = seg.get("text", "").strip()
            # If segment ends with sentence terminator, strongly favor it
            has_terminator = bool(re.search(r'[.!?]["\']?$', seg_text))
            diff_from_ideal = abs(dur - 48.0)

            if has_terminator and diff_from_ideal < best_punc_diff:
                best_punc_diff = diff_from_ideal
                best_end = seg_end

        if best_end is not None:
            snapped_end = best_end
        else:
            # Fallback: take segment end within bounds closest to ideal duration
            for j in range(start_idx, len(segments)):
                seg_end = segments[j].get("end", 0)
                dur = seg_end - snapped_start
                if min_dur <= dur <= max_dur:
                    snapped_end = seg_end

        return round(snapped_start, 2), round(snapped_end, 2)

    def _deduplicate_and_refine_clips(
        self,
        raw_clips: List[Dict[str, Any]],
        segments: List[Dict[str, Any]],
        total_duration: float,
        max_clips: int,
        min_dur: float = 35.0,
        max_dur: float = 58.0,
        min_distance_sec: float = 120.0,
    ) -> List[Dict[str, Any]]:
        """
        Enforces:
        1. Clean sentence boundaries (zero mid-sentence cuts).
        2. Exact 35s - 58s duration.
        3. Skip cold intro (t >= 35s for long videos).
        4. Strict non-overlapping: every selected clip's start_time MUST be at least
           120s away from any already-selected clip.
        5. Fills any deficit with fresh heuristic candidates from other parts of the video.
        """
        min_allowed_start = 35.0 if total_duration > 600 else 0.0

        # Refine candidates
        refined_cands: List[Dict[str, Any]] = []
        for c in raw_clips:
            st = float(c.get("start_time", 0))
            et = float(c.get("end_time", st + 45.0))

            if st < min_allowed_start:
                st = min_allowed_start
                et = st + 45.0

            snapped_st, snapped_et = self._snap_to_clean_sentence(st, et, segments, min_dur, max_dur)
            dur = round(snapped_et - snapped_st, 2)

            if dur < min_dur or dur > max_dur:
                continue

            # Extract dialogue text in this window
            clip_segs = [s.get("text", "") for s in segments if s.get("start", 0) >= snapped_st - 0.5 and s.get("end", 0) <= snapped_et + 0.5]
            dialogue = _sanitize_youtube_text(" ".join(clip_segs))

            title = self.clean_and_validate_title(c.get("youtube_title"), fallback_dialogue=dialogue)

            refined_cands.append({
                "start_time": snapped_st,
                "end_time": snapped_et,
                "duration": dur,
                "virality_score": int(c.get("virality_score", 90)),
                "hook_text": _sanitize_youtube_text(str(c.get("hook_text") or dialogue[:80] + "...")),
                "reason": _sanitize_youtube_text(str(c.get("reason") or "High-impact motivational breakthrough")),
                "youtube_title": title,
                "youtube_description": _sanitize_youtube_text(f"{dialogue[:200]}...\n\n#Shorts #Motivation #Mindset #NeverGiveUp"),
                "tags": c.get("tags") or ["shorts", "motivation", "mindset", "nevergiveup", "discipline"],
            })

        # Sort candidate clips by virality_score descending
        refined_cands.sort(key=lambda x: x["virality_score"], reverse=True)

        selected: List[Dict[str, Any]] = []
        for cand in refined_cands:
            st = cand["start_time"]
            et = cand["end_time"]

            # Strict 120s separation and zero overlap
            is_valid = True
            for sel in selected:
                s_st = sel["start_time"]
                s_et = sel["end_time"]
                if abs(st - s_st) < min_distance_sec:
                    is_valid = False
                    break
                if not (et <= s_st - 30.0 or st >= s_et + 30.0):
                    is_valid = False
                    break

            if is_valid:
                cand["clip_id"] = len(selected) + 1
                selected.append(cand)
                if len(selected) >= max_clips:
                    break

        # If we need more clips, run heuristic across under-represented parts of the video
        if len(selected) < max_clips:
            heuristic_extras = self._detect_heuristic(
                segments=segments,
                total_duration=total_duration,
                max_clips=max_clips * 4,
                min_dur=min_dur,
                max_dur=max_dur,
            )
            for cand in heuristic_extras:
                st = cand["start_time"]
                et = cand["end_time"]
                is_valid = True
                for sel in selected:
                    s_st = sel["start_time"]
                    s_et = sel["end_time"]
                    if abs(st - s_st) < min_distance_sec:
                        is_valid = False
                        break
                    if not (et <= s_st - 30.0 or st >= s_et + 30.0):
                        is_valid = False
                        break

                if is_valid:
                    cand["clip_id"] = len(selected) + 1
                    selected.append(cand)
                    if len(selected) >= max_clips:
                        break

        # Final renumbering and ordering by clip_id
        for i, c in enumerate(selected, 1):
            c["clip_id"] = i

        return selected

    # -------------------------------------------------------------------------
    # Value-Driven Algorithmic Heuristic Scorer
    # -------------------------------------------------------------------------

    def _detect_heuristic(
        self,
        segments: List[Dict[str, Any]],
        total_duration: float,
        max_clips: int,
        min_dur: float,
        max_dur: float,
    ) -> List[Dict[str, Any]]:
        """
        Rule-based motivational & adversity detector with full-video chronological distribution.
        Evaluates sliding windows across chronological video epochs based on resilience keyword weights,
        narrative sentence structure, and emotional intensity.
        """
        if not segments:
            return []

        min_allowed_start = 35.0 if total_duration > 600 else 0.0
        usable_segments = [s for s in segments if s.get("start", 0) >= min_allowed_start]
        if not usable_segments:
            usable_segments = segments

        n = len(usable_segments)
        candidates: List[Dict[str, Any]] = []

        # Divide podcast into 5 chronological epochs to ensure full-video distribution
        epoch_count = 5
        epoch_size = max(1, n // epoch_count)

        for ep in range(epoch_count):
            ep_start_idx = ep * epoch_size
            ep_end_idx = min(n, (ep + 1) * epoch_size)

            for i in range(ep_start_idx, ep_end_idx):
                start_t = usable_segments[i]["start"]
                current_text = []

                for j in range(i, n):
                    seg = usable_segments[j]
                    end_t = seg["end"]
                    dur = end_t - start_t
                    current_text.append(seg.get("text", ""))

                    if dur < min_dur:
                        continue
                    if dur > max_dur:
                        break

                    text_combined = " ".join(current_text)
                    low_text = text_combined.lower()
                    words = re.findall(r"\w+", low_text)

                    # 1. Pacing score (optimal speech rate: 130-185 wpm)
                    w_count = len(words)
                    wpm = (w_count / dur) * 60 if dur > 0 else 0
                    pacing_score = 25 if (125 <= wpm <= 185) else 12

                    # 2. Motivational & resilience keyword score
                    keyword_score = 0
                    for kw, weight in self.MOTIVATIONAL_KEYWORDS.items():
                        if kw in low_text:
                            keyword_score += weight
                    keyword_score = min(45, keyword_score)

                    # 3. Complete sentence closure bonus
                    ends_cleanly = bool(re.search(r'[.!?]["\']?$', seg.get("text", "").strip()))
                    closure_bonus = 15 if ends_cleanly else 0

                    # 4. Opening hook bonus
                    first_seg_text = usable_segments[i].get("text", "").lower()
                    hook_bonus = 15 if any(kw in first_seg_text for kw in ["why", "never", "always", "pain", "quit", "remember", "people", "if you"]) else 0

                    total_score = min(99, max(50, pacing_score + keyword_score + closure_bonus + hook_bonus))

                    candidates.append({
                        "start_time": round(start_t, 2),
                        "end_time": round(end_t, 2),
                        "duration": round(dur, 2),
                        "virality_score": int(total_score),
                        "text": text_combined,
                    })

        candidates.sort(key=lambda x: x["virality_score"], reverse=True)

        selected: List[Dict[str, Any]] = []
        min_distance_sec = 120.0

        for cand in candidates:
            st = cand["start_time"]
            et = cand["end_time"]

            # Enforce 120s separation
            if any(abs(st - s["start_time"]) < min_distance_sec or not (et <= s["start_time"] - 30.0 or st >= s["end_time"] + 30.0) for s in selected):
                continue

            clean_text = self._clean_speech_text(cand["text"])
            viral_title = self.generate_viral_title(clean_text)

            selected.append({
                "clip_id": len(selected) + 1,
                "start_time": cand["start_time"],
                "end_time": cand["end_time"],
                "duration": cand["duration"],
                "virality_score": cand["virality_score"],
                "hook_text": _sanitize_youtube_text(clean_text[:80] + "..."),
                "reason": f"Powerful resilience dialogue scoring {cand['virality_score']}% motivational impact.",
                "youtube_title": _sanitize_youtube_text(viral_title),
                "youtube_description": _sanitize_youtube_text(f"{clean_text[:200]}...\n\n#Shorts #Motivation #Discipline #NeverGiveUp"),
                "tags": ["shorts", "motivation", "mindset", "nevergiveup", "discipline", "viral"],
            })

            if len(selected) >= max_clips:
                break

        return selected

    # -------------------------------------------------------------------------
    # Value-Driven Motivational Title Generator
    # -------------------------------------------------------------------------

    @staticmethod
    def _clean_speech_text(raw: str) -> str:
        """Strip speaker markers, bracketed sound effects, and stuttering."""
        t = re.sub(r'>>\s*', '', raw)
        t = re.sub(r'\[.*?\]', '', t)
        t = re.sub(r'\(.*?\)', '', t)
        t = re.sub(r'\b(uh|um|mhm|ah|er)\b', '', t, flags=re.IGNORECASE)
        t = re.sub(r'\s+', ' ', t).strip()
        return t

    def _smart_heuristic_title(self, text: str, context: Optional[str] = None) -> str:
        """Generate high-CTR, value-driven motivational titles."""
        clean = self._clean_speech_text(text)
        low = clean.lower()

        # Thematic high-CTR motivational titles with rotation to prevent duplicate titles
        if "quit" in low or "give up" in low or "stopping" in low:
            titles = [
                "Why Giving Up Is NEVER An Option 🔥 #Shorts",
                "What Happens When You Refuse To Quit 💥 #Shorts",
                "The Secret To Pushing Past The Urge To Quit #Shorts",
            ]
            return titles[abs(hash(clean[:30])) % len(titles)]

        if "pain" in low or "suffer" in low or "hurts" in low:
            titles = [
                "How Pain Makes You UNSTOPPABLE 🧠 #Shorts",
                "The Truth About Suffering And Growth 🔥 #Shorts",
                "Why You Must Embrace The Pain To Win #Shorts",
            ]
            return titles[abs(hash(clean[:30])) % len(titles)]

        if "discipline" in low or "hard work" in low or "routine" in low or "consistent" in low:
            titles = [
                "The Brutal Truth About Real Discipline #Shorts",
                "Discipline Over Motivation Every Single Day #Shorts",
                "The Unfair Advantage Of Hard Work 🔥 #Shorts",
            ]
            return titles[abs(hash(clean[:30])) % len(titles)]

        if "dream" in low or "delusion" in low or "action" in low:
            titles = [
                "Stop Dreaming. Start Putting In The Work! #Shorts",
                "Your Dreams Mean Nothing Without Action #Shorts",
                "Be Delusional And Go All In 🔥 #Shorts",
            ]
            return titles[abs(hash(clean[:30])) % len(titles)]

        if "mindset" in low or "mental" in low or "goggins" in low:
            titles = [
                "The 1% Mindset To Push Past Your Limit #Shorts",
                "How To Build An Unbreakable Mind 🧠 #Shorts",
                "The Mental Toughness That Changes Everything #Shorts",
            ]
            return titles[abs(hash(clean[:30])) % len(titles)]

        if "running" in low or "race" in low or "ultra" in low or "mile" in low or "kilometer" in low:
            return "He Ran Past The Point Of Exhaustion! 🏃‍♂️ #Shorts"
        if "fear" in low or "doubt" in low or "failure" in low:
            return "How To Overcome Self-Doubt And Win 💥 #Shorts"
        if "lesson" in low or "realized" in low or "advice" in low:
            return "The Hardest Life Lesson Nobody Tells You #Shorts"

        # Sentence-based clean declarative extraction
        sentences = [s.strip() for s in re.split(r'[.!?]+', clean) if len(s.strip()) > 15]
        if sentences:
            best_s = sentences[0]
            best_s = re.sub(r'^(okay|well|so|you know|and|like|no|yes|actually|in my opinion)\s*,?\s*', '', best_s, flags=re.IGNORECASE)
            words = best_s.split()
            if 3 <= len(words) <= 8:
                cand = " ".join(words).title()
                cand = re.sub(r'[^\w\s]', '', cand)
                return f"{cand} #Shorts"[:65]

        # General high-impact fallback
        words = clean.split()
        if len(words) >= 4:
            clean_words = [w for w in words[:6] if w.lower() not in {"and", "the", "a", "or", "so", "but"}]
            cand = " ".join(clean_words).title()
            cand = re.sub(r'[^\w\s]', '', cand)
            return f"{cand} #Shorts"[:65]

        return "The Mindset That Changes Everything 🔥 #Shorts"

    def generate_viral_title(self, text: str, context: Optional[str] = None) -> str:
        """Generate high-CTR, punchy YouTube Shorts title using free LLM or smart heuristic."""
        clean = self._clean_speech_text(text)

        # 1. Try g4f client
        try:
            from g4f.client import Client
            client = Client()
            prompt = (
                f"Write 1 ultra-motivational, punchy YouTube Shorts title (under 55 characters, ending with #Shorts) "
                f"for this quote: '{clean[:250]}'. Reply with ONLY the title string, no quotes or explanations."
            )
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
            )
            cand = resp.choices[0].message.content.strip().strip('"\'')
            return self.clean_and_validate_title(cand, fallback_dialogue=clean)
        except Exception:
            pass

        # 2. Smart heuristic fallback
        heuristic_title = self._smart_heuristic_title(clean, context)
        return self.clean_and_validate_title(heuristic_title, fallback_dialogue=clean)

    def rewrite_summary_titles(self, summary_path: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Rewrites and upgrades the titles of clips in pipeline_summary.json using AI Brain.
        """
        path = Path(summary_path) if summary_path else (Config.OUTPUT_DIR / "pipeline_summary.json")
        if not path.exists():
            return []

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        clips = data.get("clips", [])
        for clip in clips:
            text = clip.get("youtube_description", "") or clip.get("hook_text", "")
            new_title = self.generate_viral_title(text)
            clip["youtube_title"] = new_title

        atomic_write_json(path, data)

        return clips

    def _parse_json_response(
        self, text: str, max_clips: int, min_dur: float, max_dur: float
    ) -> List[Dict[str, Any]]:
        """Clean and parse JSON from LLM response."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z]*\n", "", cleaned)
            cleaned = re.sub(r"\n```$", "", cleaned)
        cleaned = cleaned.strip()

        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                for v in data.values():
                    if isinstance(v, list):
                        data = v
                        break

            if isinstance(data, list):
                valid_clips = []
                for idx, item in enumerate(data[:max_clips]):
                    st = float(item.get("start_time", 0))
                    et = float(item.get("end_time", st + 45.0))
                    dur = round(et - st, 2)
                    hook_str = str(item.get("hook_text", ""))
                    raw_title = str(item.get("youtube_title", ""))
                    validated_title = self.clean_and_validate_title(raw_title, fallback_dialogue=hook_str)
                    valid_clips.append({
                        "clip_id": idx + 1,
                        "start_time": round(st, 2),
                        "end_time": round(et, 2),
                        "duration": dur,
                        "virality_score": int(item.get("virality_score", 90)),
                        "hook_text": hook_str,
                        "reason": str(item.get("reason", "Inspirational high-value segment")),
                        "youtube_title": validated_title,
                        "youtube_description": str(item.get("youtube_description", "#Shorts #Motivation")),
                        "tags": item.get("tags", ["shorts", "motivation", "nevergiveup", "discipline"]),
                    })
                return valid_clips
        except Exception:
            pass

        return []
