"""
Local speech-to-text transcription with fast coarse pass and exact word-level clip pass.
"""

import json
import os
import subprocess
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import numpy as np
from faster_whisper import WhisperModel

from .config import Config


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
