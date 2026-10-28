"""
AI Brain for viral moment detection and metadata generation.
Supports Google GenAI (Gemini 2.5 Flash), Groq, Ollama, and Heuristic rule-based fallback.
"""

import json
import os
import re
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.error

from .config import Config


class ViralMomentDetector:
    """Identifies top retention, high-virality clips from transcript."""

    VIRAL_KEYWORDS = {
        "secret", "insane", "crazy", "shocking", "unbelievable", "never", "always",
        "mistake", "warning", "truth", "money", "billion", "future", "genius",
        "hidden", "hack", "trick", "rule", "destroy", "win", "stop", "change",
        "danger", "worst", "best", "powerful", "huge", "revolutionary"
    }

    def __init__(
        self,
        provider: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.provider = provider or Config.DEFAULT_LLM_PROVIDER
        self.api_key = api_key or Config.GEMINI_API_KEY
        self.model = model or Config.DEFAULT_GEMINI_MODEL

    def detect_moments(
        self,
        transcript_data: Dict[str, Any],
        max_clips: int = 3,
        min_duration: float = 25.0,
        max_duration: float = 60.0,
    ) -> List[Dict[str, Any]]:
        """
        Analyze transcript and return list of top viral moment dictionaries.
        """
        segments = transcript_data.get("segments", [])
        total_duration = transcript_data.get("duration", 0)

        if not segments:
            return []

        # If total video is already shorter than max_duration, return entire video as 1 clip
        if total_duration <= max_duration:
            title = transcript_data.get("full_text", "")[:40].strip() or "Must Watch Clip"
            return [{
                "clip_id": 1,
                "start_time": 0.0,
                "end_time": round(total_duration, 2),
                "duration": round(total_duration, 2),
                "virality_score": 90,
                "hook_text": title,
                "reason": "Complete concise video segment",
                "youtube_title": f"{title} #Shorts",
                "youtube_description": f"{title}\n\n#Shorts #Viral",
                "tags": ["shorts", "viral", "trending"],
            }]

        formatted_transcript = transcript_data.get("formatted_transcript", "")
        if not formatted_transcript:
            formatted_transcript = "\n".join(
                f"[{s['start']:.1f}s - {s['end']:.1f}s] {s['text']}" for s in segments
            )

        # Attempt selected provider with fallback chain
        if self.provider == "gemini" and self.api_key:
            try:
                return self._detect_gemini(formatted_transcript, max_clips, min_duration, max_duration)
            except Exception as e:
                print(f"[ViralMomentDetector] Gemini API failed ({e}), checking fallback...")

        if Config.GROQ_API_KEY:
            try:
                return self._detect_groq(formatted_transcript, max_clips, min_duration, max_duration)
            except Exception as e:
                print(f"[ViralMomentDetector] Groq API failed ({e}), checking fallback...")

        # Check local Ollama
        try:
            return self._detect_ollama(formatted_transcript, max_clips, min_duration, max_duration)
        except Exception:
            pass

        # Final resilient fallback: Heuristic rule-based detection
        print("[ViralMomentDetector] Using rule-based heuristic viral detector (100% local & free)...")
        return self._detect_heuristic(segments, max_clips, min_duration, max_duration)

    def _build_prompt(self, transcript: str, max_clips: int, min_dur: float, max_dur: float) -> str:
        return f"""You are an elite short-form video editor and viral content strategist (specializing in YouTube Shorts, TikTok, and Instagram Reels).

Analyze the following transcript with timestamps and select the top {max_clips} most viral, high-retention moments.

REQUIREMENTS:
1. Duration: Each clip MUST be between {int(min_dur)} and {int(max_dur)} seconds long.
2. Hook: Must start with an immediate curiosity gap, surprising insight, heated debate, or high-energy quote.
3. Payoff: Must have a clear conclusion, punchline, or realization.
4. Retention: The story or idea should be fully understandable without requiring prior context.
5. Strict JSON output: Respond with ONLY a valid JSON array of objects.

JSON SCHEMA:
[
  {{
    "clip_id": 1,
    "start_time": float,
    "end_time": float,
    "duration": float,
    "virality_score": int (1 to 100),
    "hook_text": "Short catchy 1-line hook for text overlay",
    "reason": "Why this clip will hold viewer attention",
    "youtube_title": "Catchy YouTube Shorts title under 60 chars with #Shorts",
    "youtube_description": "Engaging description with key takeaway and #Shorts",
    "tags": ["3 to 6 relevant hashtags without #"]
  }}
]

TRANSCRIPT:
{transcript}
"""

    def _detect_gemini(
        self, transcript: str, max_clips: int, min_dur: float, max_dur: float
    ) -> List[Dict[str, Any]]:
        """Run viral moment detection via google-genai SDK."""
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.api_key)
        prompt = self._build_prompt(transcript, max_clips, min_dur, max_dur)

        response = client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )

        text = response.text or ""
        return self._parse_json_response(text, max_clips, min_dur, max_dur)

    def _detect_groq(
        self, transcript: str, max_clips: int, min_dur: float, max_dur: float
    ) -> List[Dict[str, Any]]:
        """Fallback to Groq free API."""
        prompt = self._build_prompt(transcript, max_clips, min_dur, max_dur)
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {Config.GROQ_API_KEY}",
        }
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [
                {"role": "system", "content": "You are a viral shorts editor. Respond with raw JSON only."},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            return self._parse_json_response(content, max_clips, min_dur, max_dur)

    def _detect_ollama(
        self, transcript: str, max_clips: int, min_dur: float, max_dur: float
    ) -> List[Dict[str, Any]]:
        """Fallback to local Ollama instance."""
        prompt = self._build_prompt(transcript, max_clips, min_dur, max_dur)
        url = f"{Config.OLLAMA_BASE_URL.rstrip('/')}/api/generate"
        payload = {
            "model": "llama3",
            "prompt": prompt,
            "format": "json",
            "stream": False,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return self._parse_json_response(data.get("response", ""), max_clips, min_dur, max_dur)

    def _detect_heuristic(
        self, segments: List[Dict[str, Any]], max_clips: int, min_dur: float, max_dur: float
    ) -> List[Dict[str, Any]]:
        """
        Rule-based algorithmic peak viral detector.
        Evaluates sliding windows across segments based on keyword frequency,
        questions, sentence density, and emotional spikes.
        """
        if not segments:
            return []

        candidates = []
        n = len(segments)

        for i in range(n):
            start_t = segments[i]["start"]
            current_text = []
            score = 0

            for j in range(i, n):
                seg = segments[j]
                end_t = seg["end"]
                dur = end_t - start_t
                current_text.append(seg["text"])

                if dur < min_dur:
                    continue
                if dur > max_dur:
                    break

                text_combined = " ".join(current_text).lower()
                words = re.findall(r"\w+", text_combined)

                # Base scoring
                w_count = len(words)
                wpm = (w_count / dur) * 60 if dur > 0 else 0

                # Ideal speaking rate for shorts is 130-180 WPM
                pacing_score = 30 if (120 <= wpm <= 190) else 15

                # Keyword score
                matched_keywords = sum(1 for w in words if w in self.VIRAL_KEYWORDS)
                keyword_score = min(35, matched_keywords * 7)

                # Question & punctuation excitement
                q_count = text_combined.count("?")
                excl_count = text_combined.count("!")
                punct_score = min(20, (q_count * 8) + (excl_count * 5))

                # Hook boost if first segment starts with a question or strong trigger
                first_words = re.findall(r"\w+", segments[i]["text"].lower())[:5]
                hook_boost = 15 if any(w in self.VIRAL_KEYWORDS for w in first_words) else 0

                total_score = min(99, max(50, pacing_score + keyword_score + punct_score + hook_boost))

                candidates.append({
                    "start_time": round(start_t, 2),
                    "end_time": round(end_t, 2),
                    "duration": round(dur, 2),
                    "virality_score": int(total_score),
                    "text": " ".join(current_text),
                })

        # Sort by virality score descending
        candidates.sort(key=lambda x: x["virality_score"], reverse=True)

        # Select non-overlapping top moments
        selected: List[Dict[str, Any]] = []
        for cand in candidates:
            # Check overlap with existing selected
            overlap = False
            for s in selected:
                if not (cand["end_time"] <= s["start_time"] or cand["start_time"] >= s["end_time"]):
                    overlap = True
                    break
            if not overlap:
                cand_id = len(selected) + 1
                words = cand["text"].split()
                hook = " ".join(words[:6]) + "..." if len(words) > 6 else cand["text"]
                title_text = " ".join(words[:8]).title()
                clean_title = re.sub(r'[^\w\s]', '', title_text)

                selected.append({
                    "clip_id": cand_id,
                    "start_time": cand["start_time"],
                    "end_time": cand["end_time"],
                    "duration": cand["duration"],
                    "virality_score": cand["virality_score"],
                    "hook_text": hook,
                    "reason": f"High speaker engagement with {cand['virality_score']}% retention momentum.",
                    "youtube_title": f"{clean_title} #Shorts"[:65],
                    "youtube_description": f"{cand['text'][:200]}...\n\n#Shorts #Viral #Trending",
                    "tags": ["shorts", "viral", "trending", "clip", "highlight"],
                })
                if len(selected) >= max_clips:
                    break

        return selected

    def _parse_json_response(
        self, text: str, max_clips: int, min_dur: float, max_dur: float
    ) -> List[Dict[str, Any]]:
        """Clean and parse JSON from LLM response."""
        cleaned = text.strip()
        # Remove markdown codeblocks
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z]*\n", "", cleaned)
            cleaned = re.sub(r"\n```$", "", cleaned)
        cleaned = cleaned.strip()

        try:
            data = json.loads(cleaned)
            # If wrapped in a dictionary key like {"clips": [...]}
            if isinstance(data, dict):
                for v in data.values():
                    if isinstance(v, list):
                        data = v
                        break

            if isinstance(data, list):
                valid_clips = []
                for idx, item in enumerate(data[:max_clips]):
                    st = float(item.get("start_time", 0))
                    et = float(item.get("end_time", st + min_dur))
                    dur = round(et - st, 2)
                    valid_clips.append({
                        "clip_id": idx + 1,
                        "start_time": round(st, 2),
                        "end_time": round(et, 2),
                        "duration": dur,
                        "virality_score": int(item.get("virality_score", 85)),
                        "hook_text": str(item.get("hook_text", "")),
                        "reason": str(item.get("reason", "High-retention segment")),
                        "youtube_title": str(item.get("youtube_title", f"Viral Moment #{idx+1} #Shorts")),
                        "youtube_description": str(item.get("youtube_description", "#Shorts")),
                        "tags": item.get("tags", ["shorts", "viral"]),
                    })
                return valid_clips
        except Exception as e:
            print(f"[ViralMomentDetector] Error parsing JSON ({e}). Raw: {text[:200]}")

        return []
