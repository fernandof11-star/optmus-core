"""Transcricao com faster-whisper local.

Ambiente sem GPU: modelo ``small`` quantizado em int8. Dado pessoal nao sai da
maquina e nao ha custo por minuto.

**A latencia de cada transcricao e medida e logada desde a F1** (secao 4.5). A
decisao de migrar para STT em nuvem deve sair do numero medido, nao da
sensacao. Se ``stt.duracao_ms`` estourar sistematicamente o orcamento de 1,2s
de wake-word ate a primeira silaba, ai sim vale testar ``medium`` local ou
Deepgram/AssemblyAI.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from core.config import Settings
from core.logging import get_logger

log = get_logger("perception.stt")


class STTIndisponivel(RuntimeError):
    """faster-whisper ausente ou modelo nao carregavel."""


@dataclass(slots=True)
class Transcription:
    text: str
    duracao_ms: float
    audio_ms: float
    idioma: str | None = None
    modelo: str | None = None

    @property
    def vazia(self) -> bool:
        return not self.text.strip()

    @property
    def fator_tempo_real(self) -> float:
        """<1 significa transcrever mais rapido do que o audio dura."""
        return round(self.duracao_ms / self.audio_ms, 3) if self.audio_ms else 0.0


class Transcriber:
    """faster-whisper carregado sob demanda, executado fora do event loop."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._modelo: Any = None
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """Carrega o modelo. Chame no boot para nao pagar isso no 1o turno."""
        async with self._lock:
            if self._modelo is not None:
                return
            try:
                from faster_whisper import WhisperModel
            except Exception as exc:
                raise STTIndisponivel(
                    'faster-whisper ausente: pip install -e ".[voz]"'
                ) from exc

            t0 = perf_counter()
            self._modelo = await asyncio.to_thread(
                WhisperModel,
                self._settings.whisper_model,
                device="cpu",
                compute_type=self._settings.whisper_compute_type,
            )
            log.info(
                "stt.modelo_carregado",
                modelo=self._settings.whisper_model,
                compute_type=self._settings.whisper_compute_type,
                carga_ms=round((perf_counter() - t0) * 1000, 1),
            )

    @property
    def carregado(self) -> bool:
        return self._modelo is not None

    async def transcribe(self, pcm: bytes) -> Transcription:
        """Transcreve PCM 16-bit mono e loga a latencia real."""
        await self.load()
        import numpy as np

        audio_ms = len(pcm) / 2 / self._settings.audio_sample_rate * 1000
        amostras = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

        t0 = perf_counter()
        segmentos, info = await asyncio.to_thread(
            self._modelo.transcribe,
            amostras,
            language=self._settings.whisper_language,
            beam_size=1,  # feixe 1: metade da latencia, perda minima em comando curto
            vad_filter=False,  # o VAD ja cortou a fala; filtrar de novo so custa tempo
        )
        texto = " ".join(s.text.strip() for s in segmentos).strip()
        duracao = round((perf_counter() - t0) * 1000, 1)

        resultado = Transcription(
            text=texto,
            duracao_ms=duracao,
            audio_ms=round(audio_ms, 1),
            idioma=getattr(info, "language", None),
            modelo=self._settings.whisper_model,
        )
        log.info(
            "stt.transcricao",
            duracao_ms=duracao,
            audio_ms=resultado.audio_ms,
            fator_tempo_real=resultado.fator_tempo_real,
            caracteres=len(texto),
        )
        return resultado
