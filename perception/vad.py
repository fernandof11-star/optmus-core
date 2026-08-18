"""Deteccao de fim de fala.

Duas implementacoes atras da mesma interface:

- ``webrtcvad`` quando instalado (classificador treinado, robusto a ruido);
- energia RMS como fallback, que funciona em qualquer maquina sem dependencia.

O parametro que importa e ``vad_silence_ms``: curto demais corta o usuario no
meio da frase, longo demais adiciona latencia morta em cada turno. 700ms e um
comeco razoavel para PT-BR - ajuste medindo, nao adivinhando.
"""

from __future__ import annotations

import array
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum

from core.config import Settings
from core.logging import get_logger

log = get_logger("perception.vad")


class MotivoFim(StrEnum):
    SILENCIO = "silencio"
    LIMITE_DE_TEMPO = "limite_de_tempo"
    FLUXO_ENCERRADO = "fluxo_encerrado"
    CURTA_DEMAIS = "curta_demais"


@dataclass(slots=True)
class Utterance:
    """Um trecho de fala capturado entre o wake word e o silencio."""

    pcm: bytes
    duracao_ms: float
    motivo: MotivoFim
    frames_com_voz: int = 0

    @property
    def utilizavel(self) -> bool:
        return bool(self.pcm) and self.motivo is not MotivoFim.CURTA_DEMAIS


def rms(frame: bytes) -> float:
    """Energia media do frame (PCM 16-bit little-endian)."""
    if not frame:
        return 0.0
    amostras = array.array("h")
    amostras.frombytes(frame[: len(frame) - (len(frame) % 2)])
    if not amostras:
        return 0.0
    return math.sqrt(sum(a * a for a in amostras) / len(amostras))


class VoiceActivityDetector:
    """Classifica um frame como voz ou silencio."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._webrtc = self._carregar_webrtc()
        self.modo = "webrtcvad" if self._webrtc is not None else "energia"

    def _carregar_webrtc(self) -> object | None:
        """Resolve o backend conforme ``OPTMUS_VAD_BACKEND``.

        A escolha e explicita de proposito: qual VAD esta ativo muda onde a
        fala e cortada, e descobrir isso por "qual pacote esta instalado" torna
        o comportamento dependente do ambiente.
        """
        if self._settings.vad_backend == "energia":
            return None
        # webrtcvad so aceita 10, 20 ou 30ms e taxas especificas.
        if self._settings.audio_frame_ms not in (10, 20, 30):
            return None
        if self._settings.audio_sample_rate not in (8000, 16000, 32000, 48000):
            return None
        try:
            import webrtcvad
        except Exception:
            if self._settings.vad_backend == "webrtcvad":
                raise
            return None
        return webrtcvad.Vad(2)

    def is_speech(self, frame: bytes) -> bool:
        if self._webrtc is not None:
            try:
                return bool(self._webrtc.is_speech(frame, self._settings.audio_sample_rate))
            except Exception:  # noqa: BLE001 - frame de tamanho errado cai na energia
                pass
        return rms(frame) >= self._settings.vad_energy_threshold


class UtteranceCapture:
    """Acumula frames ate o silencio (ou o teto de duracao) encerrar a fala."""

    def __init__(self, settings: Settings, vad: VoiceActivityDetector | None = None) -> None:
        self._settings = settings
        self._vad = vad or VoiceActivityDetector(settings)

    async def capture(self, frames: AsyncIterator[bytes]) -> Utterance:
        frame_ms = self._settings.audio_frame_ms
        max_frames = int(self._settings.vad_max_utterance_s * 1000 / frame_ms)
        frames_de_silencio_para_encerrar = max(1, self._settings.vad_silence_ms // frame_ms)

        capturados: list[bytes] = []
        silencio_seguido = 0
        com_voz = 0
        motivo = MotivoFim.FLUXO_ENCERRADO

        async for frame in frames:
            capturados.append(frame)
            if self._vad.is_speech(frame):
                com_voz += 1
                silencio_seguido = 0
            else:
                silencio_seguido += 1

            if com_voz and silencio_seguido >= frames_de_silencio_para_encerrar:
                motivo = MotivoFim.SILENCIO
                break
            if len(capturados) >= max_frames:
                motivo = MotivoFim.LIMITE_DE_TEMPO
                log.warning("vad.limite_de_tempo", segundos=self._settings.vad_max_utterance_s)
                break

        duracao = len(capturados) * frame_ms
        if com_voz * frame_ms < self._settings.vad_min_utterance_ms:
            motivo = MotivoFim.CURTA_DEMAIS

        return Utterance(
            pcm=b"".join(capturados),
            duracao_ms=float(duracao),
            motivo=motivo,
            frames_com_voz=com_voz,
        )
