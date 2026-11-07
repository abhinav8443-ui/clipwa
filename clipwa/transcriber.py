"""
Local speech-to-text transcription with fast coarse pass and exact word-level clip pass.
"""

import json
import os
import re
import subprocess
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import numpy as np
from faster_whisper import WhisperModel

from .config import Config


class BoundaryResult(tuple):
    """
    Tuple containing (start_time, end_time) with backwards-compatible unpacking
    and rich metadata attributes: start_time, end_time, start_text, end_text, duration, full_text.
    """
    def __new__(
        cls,
        start_time: float,
        end_time: float,
        start_text: str = "",
        end_text: str = "",
        full_text: str = "",
    ):
        st = round(float(start_time), 2)
        et = round(float(end_time), 2)
        instance = super().__new__(cls, (st, et))
        instance.start_time = st
        instance.end_time = et
        instance.start_text = start_text.strip()
        instance.end_text = end_text.strip()
        instance.full_text = full_text.strip()
        instance.duration = round(et - st, 2)
        return instance

    def __repr__(self) -> str:
        return (
            f"BoundaryResult(start={self.start_time}s, end={self.end_time}s, "
            f"dur={self.duration}s, start='{self.start_text}', end='{self.end_text}')"
        )


LEADING_FILLER_WORDS = {
    "and", "so", "um", "uh", "like", "you know", "well", "yeah", "right", "okay", "but", "look", "i mean",
}

CONTINUATION_CONJUNCTIONS = [
    "because",
    "which means",
    "and that's why",
    "that's why",
    "instead",
    "but here's the thing",
    "here's the thing",
    "here's why",
    "and therefore",
    "and so",
    "meaning that",
    "which is why",
    "but that",
    "and that",
    "but",
]

TRANSITION_MARKERS = [
    "anyway",
    "moving on",
    "now let's talk about",
    "let's talk about",
    "the next thing",
    "next up",
    "so my question is",
    "can you tell us",
    "what do you think",
    "let me ask you",
    "on to the next",
]


def snap_to_complete_thought(
    words: Optional[List[Dict[str, Any]]],
    segments: Optional[List[Dict[str, Any]]],
    proposed_start: float,
    proposed_end: float,
    min_dur: float = 28.0,
    max_dur: float = 58.5,
) -> BoundaryResult:
    """
    Word-level sentence boundary refinement:
    1. Clean Sentence Start:
       - Snaps start_time to the beginning of the sentence's first word (walks backward up to 6.0s
         or forward up to 4.0s) minus a 0.12s pre-roll buffer (clamped before previous word end).
       - Strips leading filler words ('And', 'So', 'Um', 'Uh', 'Like', 'You know').
    2. Complete Thought & Punchline End:
       - Searches window around proposed_end for candidate sentence endings (. ! ? with pause >= 0.30s).
       - Continuation Filter: Keeps concluding clause if next sentence starts with continuation conjunction
         ('Because...', 'Which means...', etc.) and completes within max_dur.
       - Early-Stop Rule: Stops immediately at punchline 5-10s before proposed_end if followed by pause >= 0.50s
         or transition phrase ('Anyway', 'Moving on', etc.) to prevent over-extension.
       - Natural 0.25s tail pad after final word (clamped before next word start).
    """
    all_words: List[Dict[str, Any]] = []
    if words:
        all_words = list(words)
    elif segments:
        for s in segments:
            if s.get("words"):
                all_words.extend(s["words"])
            elif s.get("text"):
                raw_words = s["text"].strip().split()
                if raw_words:
                    s_st = float(s.get("start", 0))
                    s_et = float(s.get("end", s_st + 1.0))
                    dur_w = (s_et - s_st) / len(raw_words)
                    for j, rw in enumerate(raw_words):
                        all_words.append({
                            "word": rw,
                            "start": round(s_st + j * dur_w, 3),
                            "end": round(s_st + (j + 1) * dur_w, 3),
                        })

    if not all_words:
        return BoundaryResult(proposed_start, proposed_end, "", "", "")

    all_words = sorted(all_words, key=lambda w: float(w.get("start", 0)))
    total_words = len(all_words)

    # -------------------------------------------------------------
    # 1. CLEAN SENTENCE START (start_time)
    # -------------------------------------------------------------
    target_idx = 0
    min_dist = float("inf")
    for i, w in enumerate(all_words):
        dist = abs(float(w.get("start", 0)) - proposed_start)
        if dist < min_dist:
            min_dist = dist
            target_idx = i

    def is_sentence_starter(idx: int) -> Tuple[bool, float]:
        if idx == 0:
            return True, 100.0
        prev = all_words[idx - 1]
        curr = all_words[idx]
        prev_word = str(prev.get("word", "")).strip()
        has_term_punct = bool(re.search(r'[.!?]["\']?$', prev_word))
        pause = max(0.0, float(curr.get("start", 0)) - float(prev.get("end", 0)))
        curr_word = str(curr.get("word", "")).strip()
        is_capitalized = bool(curr_word and curr_word[0].isupper())

        if has_term_punct:
            return True, 80.0 + (pause * 20.0)
        if pause >= 0.45:
            return True, 60.0 + (pause * 15.0)
        if pause >= 0.30 and is_capitalized:
            return True, 40.0 + (pause * 10.0)
        return False, 0.0

    # Search window around proposed_start: backward up to 6.0s, forward up to 4.0s
    candidate_starts: List[Tuple[int, float]] = []
    
    # Check target_idx itself
    is_start, q_score = is_sentence_starter(target_idx)
    if is_start:
        candidate_starts.append((target_idx, q_score + 20.0))

    # Walk backward up to 6.0s
    for i in range(target_idx - 1, -1, -1):
        if proposed_start - float(all_words[i].get("start", 0)) > 6.0:
            break
        is_s, score = is_sentence_starter(i)
        if is_s:
            dist_penalty = abs(float(all_words[i].get("start", 0)) - proposed_start) * 2.0
            candidate_starts.append((i, score - dist_penalty))

    # Walk forward up to 4.0s if needed
    for i in range(target_idx + 1, total_words):
        if float(all_words[i].get("start", 0)) - proposed_start > 4.0:
            break
        is_s, score = is_sentence_starter(i)
        if is_s:
            dist_penalty = abs(float(all_words[i].get("start", 0)) - proposed_start) * 3.0
            candidate_starts.append((i, score - dist_penalty))

    if candidate_starts:
        candidate_starts.sort(key=lambda x: x[1], reverse=True)
        start_idx = candidate_starts[0][0]
    else:
        # Fallback: find word in [proposed_start - 6.0, proposed_start + 4.0] with max pause
        best_pause = -1.0
        start_idx = target_idx
        for i in range(max(0, target_idx - 15), min(total_words, target_idx + 15)):
            w_st = float(all_words[i].get("start", 0))
            if proposed_start - 6.0 <= w_st <= proposed_start + 4.0:
                p_end = float(all_words[i - 1].get("end", 0)) if i > 0 else 0.0
                pause = w_st - p_end
                if pause > best_pause:
                    best_pause = pause
                    start_idx = i

    # Strip leading filler words ('and', 'so', 'um', 'uh', 'like', 'you know')
    def is_filler_at(idx: int) -> int:
        if idx >= total_words:
            return 0
        w0 = re.sub(r'[^\w]', '', str(all_words[idx].get("word", "")).lower())
        if idx + 1 < total_words:
            w1 = re.sub(r'[^\w]', '', str(all_words[idx + 1].get("word", "")).lower())
            if w0 == "you" and w1 == "know":
                return 2
        if w0 in LEADING_FILLER_WORDS:
            return 1
        return 0

    advance_count = 0
    while start_idx < total_words - 5 and advance_count < 3:
        skip = is_filler_at(start_idx)
        if skip > 0:
            start_idx += skip
            advance_count += skip
        else:
            break

    # Apply 0.12s pre-roll buffer (clamped so previous word audio is never included)
    w_start_raw = float(all_words[start_idx].get("start", proposed_start))
    if start_idx > 0:
        prev_end = float(all_words[start_idx - 1].get("end", w_start_raw))
        snapped_start = max(prev_end, w_start_raw - 0.12)
    else:
        snapped_start = max(0.0, w_start_raw - 0.12)

    # -------------------------------------------------------------
    # 2. COMPLETE THOUGHT & PUNCHLINE END (end_time)
    # -------------------------------------------------------------
    window_min = max(snapped_start + min_dur, proposed_end - 12.0)
    window_max = min(snapped_start + max_dur, proposed_end + 10.0)
    last_word_end = float(all_words[-1].get("end", window_max))
    window_max = min(window_max, last_word_end)
    if window_min >= window_max:
        window_min = max(snapped_start + min_dur, window_max - 5.0)

    class EndCandidate:
        def __init__(self, idx: int, score: float, is_early_stop: bool = False):
            self.idx = idx
            self.score = score
            self.is_early_stop = is_early_stop

    end_candidates: List[EndCandidate] = []

    for i in range(start_idx, total_words):
        w = all_words[i]
        w_end = float(w.get("end", 0))
        if w_end < window_min:
            continue
        if w_end > window_max:
            break

        w_text = str(w.get("word", "")).strip()
        has_term_punct = bool(re.search(r'[.!?]["\']?$', w_text))
        is_exclamation = bool(re.search(r'[!]["\']?$', w_text))

        if i + 1 < total_words:
            next_w = all_words[i + 1]
            pause_after = max(0.0, float(next_w.get("start", 0)) - w_end)
            next_phrase = " ".join(
                str(all_words[k].get("word", "")).lower()
                for k in range(i + 1, min(total_words, i + 6))
            )
        else:
            pause_after = 1.0
            next_phrase = ""

        # Continuation Filter:
        # If this word ends a sentence but next sentence starts with continuation conjunction
        is_continuation = False
        continuation_completed_within_bounds = False
        for conj in CONTINUATION_CONJUNCTIONS:
            if next_phrase == conj or next_phrase.startswith(conj + " "):
                is_continuation = True
                for j in range(i + 1, total_words):
                    cw = all_words[j]
                    cw_end = float(cw.get("end", 0))
                    if cw_end - snapped_start > max_dur:
                        break
                    cw_text = str(cw.get("word", "")).strip()
                    if re.search(r'[.!?]["\']?$', cw_text):
                        continuation_completed_within_bounds = True
                        break
                break

        # Early-Stop Rule (Prevent Over-Extension):
        # If speaker finishes punchline 5 to 10s before proposed_end with pause >= 0.50s or transition
        is_early_stop = False
        dur_so_far = w_end - snapped_start
        is_early_window = (proposed_end - 10.0 <= w_end <= proposed_end - 5.0) and (dur_so_far >= min_dur)
        has_transition = any(next_phrase.startswith(tm) for tm in TRANSITION_MARKERS)

        if has_term_punct and is_early_window and not is_continuation and (pause_after >= 0.50 or has_transition):
            is_early_stop = True

        score = 0.0
        if has_term_punct:
            score += 60.0
            if is_exclamation:
                score += 15.0  # Punchline emphasis
            if pause_after >= 0.40:
                score += 45.0
            elif pause_after >= 0.25:
                score += 30.0
            elif pause_after >= 0.15:
                score += 15.0
        elif pause_after >= 0.45:
            score += 35.0  # Breath pause without punctuation
        elif pause_after >= 0.30:
            score += 20.0

        if is_early_stop:
            score += 65.0  # Strongly favor stopping cleanly on punchline

        if is_continuation:
            if continuation_completed_within_bounds:
                score -= 85.0
            else:
                score -= 20.0

        # Proximity to proposed_end bonus if it finishes the speaker's concluding point
        dist_to_proposed_end = abs(w_end - proposed_end)
        if dist_to_proposed_end <= 2.2 and has_term_punct and pause_after >= 0.25:
            score += 35.0

        # Duration preference: sweet spot 42s - 54s
        dur_diff = abs(dur_so_far - 48.0)
        score -= (dur_diff * 0.7)

        end_candidates.append(EndCandidate(idx=i, score=score, is_early_stop=is_early_stop))

    if end_candidates:
        end_candidates.sort(key=lambda c: c.score, reverse=True)
        end_idx = end_candidates[0].idx
    else:
        # Fallback: pick word in [window_min, window_max] with largest pause
        best_p = -1.0
        end_idx = min(total_words - 1, start_idx + 10)
        for i in range(start_idx, total_words):
            w_end = float(all_words[i].get("end", 0))
            if window_min <= w_end <= window_max:
                p_next = float(all_words[i + 1].get("start", w_end + 0.5)) if i + 1 < total_words else w_end + 1.0
                p = p_next - w_end
                if p > best_p:
                    best_p = p
                    end_idx = i

    # Apply 0.25s tail pad after final spoken word (clamped before next word start)
    w_end_raw = float(all_words[end_idx].get("end", proposed_end))
    if end_idx + 1 < total_words:
        next_start = float(all_words[end_idx + 1].get("start", w_end_raw + 0.3))
        tail_pad = min(0.25, max(0.04, (next_start - w_end_raw) - 0.04))
    else:
        tail_pad = 0.25

    snapped_end = round(w_end_raw + tail_pad, 2)

    # Final clamping to min_dur and max_dur
    cur_dur = snapped_end - snapped_start
    if cur_dur > max_dur:
        snapped_end = round(snapped_start + max_dur, 2)
    elif cur_dur < min_dur:
        snapped_end = round(min(snapped_start + min_dur, last_word_end), 2)

    # Extract start and end text for logging and metadata
    clip_words = [
        w for w in all_words
        if float(w.get("start", 0)) >= snapped_start - 0.2
        and float(w.get("end", 0)) <= snapped_end + 0.2
    ]
    if clip_words:
        start_words = [str(w.get("word", "")).strip() for w in clip_words[:5]]
        end_words = [str(w.get("word", "")).strip() for w in clip_words[-5:]]
        start_text = " ".join(start_words)
        end_text = " ".join(end_words)
        full_text = " ".join(str(w.get("word", "")).strip() for w in clip_words)
    else:
        start_text = ""
        end_text = ""
        full_text = ""

    return BoundaryResult(
        start_time=snapped_start,
        end_time=snapped_end,
        start_text=start_text,
        end_text=end_text,
        full_text=full_text,
    )


class LocalTranscriber:
    """Local, 100% free speech-to-text with auto CUDA acceleration and multi-threading."""

    def __init__(
        self,
        model_size: Optional[str] = None,
        device: Optional[str] = None,
        compute_type: Optional[str] = None,
    ):
        self.model_size = model_size or Config.WHISPER_MODEL

        # Auto-detect CUDA acceleration
        if device is None:
            try:
                import ctranslate2
                if ctranslate2.get_cuda_device_count() > 0:
                    self.device = "cuda"
                    self.compute_type = compute_type or "float16"
                else:
                    self.device = "cpu"
                    self.compute_type = compute_type or "int8"
            except Exception:
                self.device = "cpu"
                self.compute_type = compute_type or "int8"
        else:
            self.device = device
            self.compute_type = compute_type or ("float16" if device == "cuda" else "int8")

        self.cpu_threads = os.cpu_count() or 4
        self._model: Optional[WhisperModel] = None

    @property
    def model(self) -> WhisperModel:
        """Lazy load Whisper model on first use."""
        if self._model is None:
            try:
                self._model = WhisperModel(
                    self.model_size,
                    device=self.device,
                    compute_type=self.compute_type,
                    cpu_threads=self.cpu_threads,
                )
            except Exception as e:
                # If CUDA fails (e.g. cuDNN missing), gracefully fallback to CPU
                if self.device == "cuda":
                    print(f"[LocalTranscriber] CUDA notice ({e}), falling back to CPU int8...")
                    self.device = "cpu"
                    self.compute_type = "int8"
                    self._model = WhisperModel(
                        self.model_size,
                        device="cpu",
                        compute_type="int8",
                        cpu_threads=self.cpu_threads,
                    )
                else:
                    raise e
        return self._model

    @staticmethod
    def load_audio(audio_input: Union[str, np.ndarray], sampling_rate: int = 16000) -> np.ndarray:
        """
        Decode audio into 16kHz float32 mono numpy array.
        Bypasses PyAV library issues by using built-in wave or direct FFmpeg pipe.
        """
        if isinstance(audio_input, np.ndarray):
            return audio_input.astype(np.float32)

        file_path = str(audio_input)
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Audio file not found: {file_path}")

        # If it's a standard WAV file matching 16kHz mono 16-bit
        if file_path.lower().endswith(".wav"):
            try:
                with wave.open(file_path, "rb") as wf:
                    n_channels = wf.getnchannels()
                    sampwidth = wf.getsampwidth()
                    framerate = wf.getframerate()
                    if n_channels == 1 and sampwidth == 2 and framerate == sampling_rate:
                        raw_bytes = wf.readframes(wf.getnframes())
                        return np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            except Exception:
                pass

        # Universal fallback: FFmpeg pipe decoding to raw 16kHz mono s16le PCM
        cmd = [
            Config.FFMPEG_PATH,
            "-nostdin",
            "-threads",
            "0",
            "-i",
            file_path,
            "-f",
            "s16le",
            "-ac",
            "1",
            "-acodec",
            "pcm_s16le",
            "-ar",
            str(sampling_rate),
            "-",
        ]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode != 0:
            raise RuntimeError(f"FFmpeg audio decode failed: {p.stderr.decode('utf-8', errors='ignore')}")

        return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0

    @staticmethod
    def format_timestamp(seconds: float) -> str:
        """Format seconds into HH:MM:SS or MM:SS string."""
        mins, secs = divmod(seconds, 60)
        hours, mins = divmod(mins, 60)
        if hours > 0:
            return f"{int(hours):02d}:{int(mins):02d}:{secs:05.2f}"
        return f"{int(mins):02d}:{secs:05.2f}"

    def transcribe(
        self,
        audio_path_or_array: Union[str, np.ndarray],
        word_timestamps: bool = True,
        beam_size: int = 1,
        language: Optional[str] = None,
        cache: bool = True,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Transcribe audio locally.
        Stage 1 coarse: word_timestamps=False, beam_size=1 (super fast)
        Stage 2 exact clip: word_timestamps=True on 30-60s clip
        """
        cache_path = None
        if isinstance(audio_path_or_array, str) and cache:
            cache_suffix = ".words.transcript.json" if word_timestamps else ".coarse.transcript.json"
            cache_path = Path(audio_path_or_array).with_suffix(cache_suffix)
            if cache_path.exists():
                try:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    pass

        if progress_callback:
            mode_desc = "exact word-level" if word_timestamps else "fast coarse"
            progress_callback({
                "stage": "transcribing",
                "message": f"Transcribing audio ({mode_desc} on {self.device})...",
            })

        # Load audio into normalized float32 numpy array
        audio_np = self.load_audio(audio_path_or_array, sampling_rate=16000)

        # Run faster-whisper with VAD and beam_size=1 for maximum throughput
        import itertools
        try:
            segments_gen, info = self.model.transcribe(
                audio_np,
                language=language,
                word_timestamps=word_timestamps,
                beam_size=beam_size,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
            )
            # Peek first item to verify CUDA execution
            first_item = next(segments_gen, None)
            if first_item is not None:
                segments_gen = itertools.chain([first_item], segments_gen)
        except Exception as e:
            if self.device == "cuda":
                print(f"[LocalTranscriber] CUDA library notice ({e}). Switching to multi-threaded CPU int8...")
                self.device = "cpu"
                self.compute_type = "int8"
                self._model = WhisperModel(
                    self.model_size,
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=self.cpu_threads,
                )
                segments_gen, info = self._model.transcribe(
                    audio_np,
                    language=language,
                    word_timestamps=word_timestamps,
                    beam_size=beam_size,
                    vad_filter=True,
                    vad_parameters=dict(min_silence_duration_ms=500),
                )
            else:
                raise e

        all_segments: List[Dict[str, Any]] = []
        all_words: List[Dict[str, Any]] = []
        full_text_parts: List[str] = []
        formatted_lines: List[str] = []

        total_duration = info.duration if hasattr(info, "duration") else (len(audio_np) / 16000.0)

        for idx, segment in enumerate(segments_gen):
            seg_words = []
            if segment.words:
                for w in segment.words:
                    word_dict = {
                        "word": w.word.strip(),
                        "start": round(w.start, 3),
                        "end": round(w.end, 3),
                        "probability": round(w.probability, 3) if hasattr(w, "probability") else 1.0,
                    }
                    seg_words.append(word_dict)
                    all_words.append(word_dict)
            elif not word_timestamps and segment.text:
                # Approximate word timestamps from segment for coarse pass
                raw_words = segment.text.strip().split()
                dur_per_w = (segment.end - segment.start) / max(1, len(raw_words))
                for j, rw in enumerate(raw_words):
                    ws = segment.start + j * dur_per_w
                    we = ws + dur_per_w
                    w_dict = {"word": rw, "start": round(ws, 3), "end": round(we, 3), "probability": 1.0}
                    seg_words.append(w_dict)
                    all_words.append(w_dict)

            seg_dict = {
                "id": idx,
                "start": round(segment.start, 3),
                "end": round(segment.end, 3),
                "text": segment.text.strip(),
                "words": seg_words,
            }
            all_segments.append(seg_dict)
            full_text_parts.append(segment.text.strip())

            start_fmt = self.format_timestamp(segment.start)
            end_fmt = self.format_timestamp(segment.end)
            formatted_lines.append(f"[{start_fmt} - {end_fmt}] {segment.text.strip()}")

            if progress_callback and total_duration > 0:
                pct = min(100.0, (segment.end / total_duration) * 100)
                progress_callback({
                    "stage": "transcribe_progress",
                    "percent": round(pct, 1),
                    "current_time": round(segment.end, 2),
                    "total_time": round(total_duration, 2),
                    "message": f"Transcribing: {pct:.1f}% ({int(segment.end)}s/{int(total_duration)}s)",
                })

        result: Dict[str, Any] = {
            "language": info.language if hasattr(info, "language") else "en",
            "language_probability": round(info.language_probability, 3) if hasattr(info, "language_probability") else 1.0,
            "duration": round(total_duration, 2),
            "full_text": " ".join(full_text_parts),
            "formatted_transcript": "\n".join(formatted_lines),
            "segments": all_segments,
            "words": all_words,
        }

        if cache_path:
            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(result, f, indent=2, ensure_ascii=False)
            except Exception:
                pass

        return result
