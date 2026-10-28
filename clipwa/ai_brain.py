"""
AI Brain for viral moment detection and metadata generation.
Supports Google GenAI (Gemini 2.5 Flash), Groq, Ollama, Zero-API-key Free LLMs (g4f & Pollinations),
and Smart Algorithmic Heuristic fallback.
"""

import json
import os
import re
from pathlib import Path
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
        "danger", "worst", "best", "powerful", "huge", "revolutionary", "pain",
        "limits", "exercise", "running", "treadmill", "mindset", "unbreakable", "goggins"
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
            text = transcript_data.get("full_text", "")
            title = self.generate_viral_title(text)
            return [{
                "clip_id": 1,
                "start_time": 0.0,
                "end_time": round(total_duration, 2),
                "duration": round(total_duration, 2),
                "virality_score": 90,
                "hook_text": title,
                "reason": "Complete concise video segment",
                "youtube_title": title,
                "youtube_description": f"{title}\n\n#Shorts #Viral",
                "tags": ["shorts", "viral", "trending"],
            }]

        formatted_transcript = transcript_data.get("formatted_transcript", "")
        if not formatted_transcript:
            formatted_transcript = "\n".join(
                f"[{s['start']:.1f}s - {s['end']:.1f}s] {s['text']}" for s in segments
            )

        # 1. Attempt Gemini if API key is provided
        if self.provider == "gemini" and self.api_key:
            try:
                return self._detect_gemini(formatted_transcript, max_clips, min_duration, max_duration)
            except Exception as e:
                print(f"[ViralMomentDetector] Gemini API notice ({e}), checking alternative providers...")

        # 2. Attempt Groq if API key is provided
        if Config.GROQ_API_KEY:
            try:
                return self._detect_groq(formatted_transcript, max_clips, min_duration, max_duration)
            except Exception as e:
                print(f"[ViralMomentDetector] Groq API notice ({e}), checking alternative providers...")

        # 3. Check local Ollama
        try:
            res = self._detect_ollama(formatted_transcript, max_clips, min_duration, max_duration)
            if res:
                return res
        except Exception:
            pass

        # 4. Zero-Config Free Keyless LLM (g4f & Pollinations.ai)
        try:
            res = self._detect_free_llm(formatted_transcript, max_clips, min_duration, max_duration)
            if res and len(res) > 0:
                print(f"[ViralMomentDetector] Successfully generated {len(res)} viral moments using free keyless AI!")
                return res
        except Exception as e:
            print(f"[ViralMomentDetector] Free online LLM notice: {e}")

        # 5. Smart Algorithmic Heuristic fallback
        print("[ViralMomentDetector] Using smart algorithmic viral detector (100% local & free)...")
        return self._detect_heuristic(segments, max_clips, min_duration, max_duration)

    def _build_prompt(self, transcript: str, max_clips: int, min_dur: float, max_dur: float) -> str:
        return f"""You are an elite short-form video editor and viral content strategist (specializing in YouTube Shorts, TikTok, and Instagram Reels).

Analyze the following transcript with timestamps and select the top {max_clips} most viral, high-retention moments.

Target constraints:
- Duration of each clip must be between {min_dur:.0f} and {max_dur:.0f} seconds.
- Look for: high emotional peaks, powerful advice, controversial opinions, shock value, counterintuitive secrets, or cliffhanger stories.
- Write ultra-viral, high-CTR YouTube Shorts titles (MUST end with '#Shorts' and be under 60 characters). NEVER write dull titles.

Return STRICT JSON ONLY, adhering to this exact schema:
[
  {{
    "clip_id": 1,
    "start_time": 12.5,
    "end_time": 45.0,
    "virality_score": 95,
    "hook_text": "The first 3 seconds hook quote...",
    "reason": "Why this specific moment will hook viewers and stop the scroll",
    "youtube_title": "The Brutal Truth About Success #Shorts",
    "youtube_description": "Captivating 1-2 sentence description with relevant hashtags",
    "tags": ["shorts", "viral", "success", "motivation"]
  }}
]

Transcript:
{transcript}
"""

    def _detect_free_llm(
        self, transcript: str, max_clips: int, min_dur: float, max_dur: float
    ) -> List[Dict[str, Any]]:
        """Zero-API-key free LLM moment detection using g4f or pollinations.ai."""
        sample_transcript = transcript[:10000]
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
                timeout=20
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
    # Smart Heuristic Title Generator
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
        """Generate high-CTR, natural viral titles without random word stitching."""
        clean = self._clean_speech_text(text)
        low = clean.lower()

        # Thematic hooks for fitness, habits, recovery, mindset
        if "exercise" in low or "treadmill" in low or "gym" in low or "workout" in low:
            return "The 5-Minute Exercise Everyone Skips! 🏃‍♂️ #Shorts"
        if "pranav" in low or "friend" in low or "show you" in low or "recovery" in low or "insane" in low or "crazy" in low:
            return "This Crazy Secret Blew My Mind! 🤯 #Shorts"
        if "train more" in low or "work harder" in low or "good for me" in low or "goggins" in low or "doubt" in low:
            return "The Mindset That Makes You Unstoppable 🔥 #Shorts"
        if "money" in low or "rich" in low or "dollar" in low or "business" in low:
            return "The Financial Rule Nobody Talks About 💰 #Shorts"
        if "secret" in low or "hack" in low or "mistake" in low:
            return "The Biggest Mistake You're Making Right Now ⚠️ #Shorts"

        # Sentence-based clean declarative title extraction
        sentences = [s.strip() for s in re.split(r'[.!?]+', clean) if len(s.strip()) > 15]
        if sentences:
            best_s = sentences[0]
            best_s = re.sub(r'^(okay|well|so|you know|and|like|no|yes|actually|in my opinion)\s*,?\s*', '', best_s, flags=re.IGNORECASE)
            words = best_s.split()
            if 3 <= len(words) <= 9:
                cand = " ".join(words).title()
                cand = re.sub(r'[^\w\s]', '', cand)
                return f"{cand} #Shorts"[:65]

        # General high-engagement fallback
        words = clean.split()
        if len(words) >= 4:
            clean_words = [w for w in words[:7] if w.lower() not in {"and", "the", "a", "or", "so", "but"}]
            cand = " ".join(clean_words).title()
            cand = re.sub(r'[^\w\s]', '', cand)
            return f"{cand} #Shorts"[:65]

        return "Must-Watch High-Retention Clip 🔥 #Shorts"

    def generate_viral_title(self, text: str, context: Optional[str] = None) -> str:
        """Generate high-CTR, punchy YouTube Shorts title using free LLM or smart heuristic."""
        clean = self._clean_speech_text(text)

        # 1. Try g4f client
        try:
            from g4f.client import Client
            client = Client()
            prompt = (
                f"Write 1 ultra-viral, catchy, curiosity-inducing YouTube Shorts title (under 55 characters, ending with #Shorts) "
                f"for a clip with this dialogue: '{clean[:250]}'. Reply with ONLY the title string, no quotes or explanations."
            )
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
            )
            cand = resp.choices[0].message.content.strip().strip('"\'')
            if "#Shorts" not in cand and "#shorts" not in cand:
                cand = f"{cand} #Shorts"
            if len(cand) <= 70:
                return cand
        except Exception:
            pass

        # 2. Smart heuristic fallback
        return self._smart_heuristic_title(clean, context)

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

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        return clips

    def _detect_heuristic(
        self,
        segments: List[Dict[str, Any]],
        max_clips: int,
        min_dur: float,
        max_dur: float,
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
                pacing_score = 30 if (120 <= wpm <= 190) else 15

                # Keyword score
                matched_keywords = sum(1 for w in words if w in self.VIRAL_KEYWORDS)
                keyword_score = min(35, matched_keywords * 7)

                # Question & punctuation excitement
                q_count = text_combined.count("?")
                excl_count = text_combined.count("!")
                punct_score = min(20, (q_count * 8) + (excl_count * 5))

                # Hook boost if first segment starts with strong trigger
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

        candidates.sort(key=lambda x: x["virality_score"], reverse=True)

        selected: List[Dict[str, Any]] = []
        for cand in candidates:
            overlap = False
            for s in selected:
                if not (cand["end_time"] <= s["start_time"] or cand["start_time"] >= s["end_time"]):
                    overlap = True
                    break
            if not overlap:
                cand_id = len(selected) + 1
                clean_text = self._clean_speech_text(cand["text"])
                words = clean_text.split()
                hook = " ".join(words[:6]) + "..." if len(words) > 6 else clean_text
                viral_title = self.generate_viral_title(cand["text"])

                selected.append({
                    "clip_id": cand_id,
                    "start_time": cand["start_time"],
                    "end_time": cand["end_time"],
                    "duration": cand["duration"],
                    "virality_score": cand["virality_score"],
                    "hook_text": hook,
                    "reason": f"High speaker engagement with {cand['virality_score']}% retention momentum.",
                    "youtube_title": viral_title,
                    "youtube_description": f"{clean_text[:200]}...\n\n#Shorts #Viral #Trending",
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
            pass

        return []
