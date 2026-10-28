"""
Local speech-to-text transcription with word-level timestamps using faster-whisper.
Uses FFmpeg / standard library wave decoding to ensure 100% compatibility across all Python versions.
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
    """Local, 100% free speech-to-text with word-level timestamps."""

    def __init__(
        self,
        model_size: Optional[str] = None,
        device: Optional[str] = None,
        compute_type: Optional[str] = None,
    ):
        self.model_size = model_size or Config.WHISPER_MODEL
        self.device = device or Config.WHISPER_DEVICE
        self.compute_type = compute_type or Config.WHISPER_COMPUTE_TYPE
        self._model: Optional[WhisperModel] = None

    @property
    def model(self) -> WhisperModel:
        """Lazy load Whisper model on first use."""
        if self._model is None:
            self._model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
            )
        return self._model

    @staticmethod
    def load_audio(audio_input: Union[str, np.ndarray], sampling_rate: int = 16000) -> np.ndarray:
        """
        Decode audio file into 16kHz float32 mono numpy array.
        Bypasses PyAV library issues by using built-in wave or FFmpeg pipe.
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
        language: Optional[str] = None,
        cache: bool = True,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Transcribe audio locally with word-level timestamps.
        Returns detailed transcript dict.
        """
        cache_path = None
        if isinstance(audio_path_or_array, str) and cache:
            cache_path = Path(audio_path_or_array).with_suffix(".transcript.json")
            if cache_path.exists():
                try:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    pass

        if progress_callback:
            progress_callback({"stage": "transcribing", "status": "Transcribing audio locally..."})

        # Load audio into normalized float32 numpy array
        audio_np = self.load_audio(audio_path_or_array, sampling_rate=16000)

        # Run faster-whisper with word timestamps
        segments_gen, info = self.model.transcribe(
            audio_np,
            language=language,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )

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
                    "stage": "transcribing",
                    "percent": round(pct, 1),
                    "current_time": round(segment.end, 2),
                    "total_time": round(total_duration, 2),
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
