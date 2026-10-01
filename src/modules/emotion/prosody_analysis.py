import asyncio
import os
from typing import Any, AsyncGenerator, Dict, List

import numpy as np
from ray import serve
from ray.serve import handle

from src.core.module import ModuleWithHandle
from src.modules.speech_to_text.events import Voice

from .events import Emotion

_MODEL_NAME = os.environ.get("HURI_EMO_MODEL", "superb/hubert-large-superb-er")


@serve.deployment(name="EMOHandle", max_ongoing_requests=8)
class EMOHandle:
    """Prosody emotion model.

    :model_name: name of the Emotion Analysis model.
    :sample_rate: sample rate of the audio passed to predict().
    """

    def __init__(
        self,
        model_name: str = _MODEL_NAME,
        sample_rate: int = 16000,
    ):
        from transformers import (
            AutoModelForAudioClassification,
            Wav2Vec2FeatureExtractor,
        )

        self.model = AutoModelForAudioClassification.from_pretrained(model_name)
        self.feature_extractor = Wav2Vec2FeatureExtractor.from_pretrained(model_name)
        self.sample_rate = sample_rate

    async def predict(self, audio: np.ndarray) -> Dict[str, Any]:
        # Blocking torch inference runs off the replica's event loop.
        return await asyncio.to_thread(self._predict_sync, audio)

    def _predict_sync(self, audio_np: np.ndarray) -> Dict[str, Any]:
        import torch

        inputs = self.feature_extractor(
            audio_np, sampling_rate=self.sample_rate, return_tensors="pt", padding=True
        )

        with torch.no_grad():
            logits = self.model(**inputs).logits
            probs = torch.softmax(logits, dim=-1)[0]

        predicted_id = int(torch.argmax(probs).item())

        labels = self.model.config.id2label

        return {
            "label": labels[predicted_id],
            "confidence": float(probs[predicted_id]),
            "scores": {labels[i]: float(probs[i]) for i in range(len(labels))},
        }


class EMO(ModuleWithHandle):
    """EMO Module

    Prosody Analysis of user voice speech.

    input: voice,
    output: emotion

    :sample_rate: size of received voice audio. Usually 8000, 16000 or 48000.
    :block_duration: size of received voice audio (in s).
    :analysis_window: duration of audio per analysis (in s).
    """

    _handle_cls = EMOHandle
    input_type = "voice"
    output_type = "emotion"

    def __init__(
        self,
        _handle: handle.DeploymentHandle,
        sample_rate: int = 16000,
        block_duration: float = 0.020,  # s
        analysis_window: float = 4.0,  # s
        **kwargs,
    ):
        super().__init__(_handle=_handle, **kwargs)

        self.sample_rate = sample_rate
        self.window_size = int(analysis_window / block_duration)

        self.buffer: List[np.ndarray] = []

        self.running = False
        self.lock: asyncio.Lock = asyncio.Lock()

        # Set when the end of the voice is received. Remembered if an analysis
        # is in flight, so that call handles it once it finishes.
        self._end_requested: bool = False

    async def process(self, voice: Voice) -> AsyncGenerator[Emotion, None]:
        async with self.lock:
            if voice.data is None:
                self._end_requested = True
            else:
                self.buffer.append(voice.data)
            if self.running:
                return
            self.running = True

        try:
            async for emotion in self._analyze_pending():
                yield emotion
        finally:
            self.running = False

    async def _analyze_pending(self) -> AsyncGenerator[Emotion, None]:
        """Run analyses until there is nothing left to do.

        Loops so that an end-of-turn that arrives while a window is being
        analyzed is handled right after it, in this same call. Each pass is
        yielded as its own Emotion.
        """

        while True:
            async with self.lock:
                end = self._end_requested
                if end:
                    self._end_requested = False
                    chunks = list(self.buffer)
                else:
                    if len(self.buffer) < self.window_size:
                        return
                    chunks = self.buffer[: self.window_size]

            # On end, analyze the remaining buffer; fall back to a short zero
            # buffer when it is empty so the model still gets valid input.
            audio = (
                np.concatenate(chunks, axis=0)
                if chunks
                else np.zeros(self.sample_rate // 10, dtype=np.float32)
            )
            result = await self._handle.predict.remote(audio)

            async with self.lock:
                # Frames received during the await are kept for the next pass.
                self.buffer = self.buffer[len(chunks) :]

            yield Emotion(result["label"], result["confidence"], result["scores"], end)
            if not self._end_requested:
                return
