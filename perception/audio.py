"""Captura de microfone em frames de tamanho fixo.

Todo o resto da percepcao (wake word, VAD, STT) consome o mesmo fluxo:
PCM 16-bit mono, mono-canal, frames de ``OPTMUS_AUDIO_FRAME_MS``.

A callback do PortAudio roda em thread propria e nao pode tocar no event loop
direto - por isso os frames entram numa fila via ``call_soon_threadsafe``. Fila
cheia descarta o frame mais antigo: perder 20ms de audio e melhor do que travar
a captura e perder a frase inteira.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Self

from core.config import Settings
from core.logging import get_logger

log = get_logger("perception.audio")

BYTES_POR_AMOSTRA = 2


class AudioIndisponivel(RuntimeError):
    """Sem dispositivo de entrada utilizavel."""


class MicrophoneStream:
    """Fluxo assincrono de frames do microfone."""

    def __init__(self, settings: Settings, *, queue_maxsize: int = 100) -> None:
        self._settings = settings
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=queue_maxsize)
        self._stream: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.descartados = 0

    @property
    def frame_bytes(self) -> int:
        return self.frame_samples * BYTES_POR_AMOSTRA

    @property
    def frame_samples(self) -> int:
        return int(self._settings.audio_sample_rate * self._settings.audio_frame_ms / 1000)

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def start(self) -> None:
        try:
            import sounddevice as sd
        except Exception as exc:
            raise AudioIndisponivel(
                'sounddevice ausente: pip install -e ".[voz]"'
            ) from exc

        self._loop = asyncio.get_running_loop()

        def _callback(indata: Any, frames: int, time: Any, status: Any) -> None:
            if status:
                log.warning("audio.status_do_dispositivo", status=str(status))
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._enfileirar, bytes(indata))

        try:
            self._stream = sd.RawInputStream(
                samplerate=self._settings.audio_sample_rate,
                blocksize=self.frame_samples,
                device=self._settings.audio_input_device,
                channels=1,
                dtype="int16",
                callback=_callback,
            )
            self._stream.start()
        except Exception as exc:
            raise AudioIndisponivel(f"{type(exc).__name__}: {exc}") from exc

        log.info(
            "audio.microfone_aberto",
            sample_rate=self._settings.audio_sample_rate,
            frame_ms=self._settings.audio_frame_ms,
            device=self._settings.audio_input_device or "padrao",
        )

    def _enfileirar(self, frame: bytes) -> None:
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.descartados += 1
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(frame)
            except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                pass

    async def stop(self) -> None:
        if self._stream is None:
            return
        stream, self._stream = self._stream, None
        await asyncio.to_thread(stream.stop)
        await asyncio.to_thread(stream.close)
        log.info("audio.microfone_fechado", frames_descartados=self.descartados)

    async def frames(self) -> AsyncIterator[bytes]:
        while True:
            yield await self._queue.get()
