"""Sintese de voz com streaming e cadeia de fallback.

A voz E a identidade (secao 4.6): ElevenLabs streaming como primaria, Piper
local como fallback offline, voz do sistema como ultimo recurso. Sem nenhuma
delas, o texto ainda sai no log e no HUD - o Optmus fica mudo, nao morto.

Duas regras de latencia governam este arquivo:

1. Sempre a API de streaming. Gerar o audio inteiro antes de tocar insere
   1-3s de silencio morto no exato momento em que o usuario espera resposta.
2. Falar por frase, nao por resposta. :class:`SentenceBuffer` corta o texto do
   LLM na primeira pontuacao util e manda sintetizar enquanto o resto ainda
   esta sendo gerado.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Final

from core.config import Settings
from core.logging import get_logger

log = get_logger("expression.tts")

FIM_DE_FRASE: Final[re.Pattern[str]] = re.compile(r"[.!?;:\n]")
CABECALHO_WAV: Final[int] = 44


class SentenceBuffer:
    """Acumula deltas do LLM e libera frases assim que ficam falaveis.

    Nao corta em toda pontuacao: fragmentos de 3 caracteres viram audio
    picotado e soam pior do que esperar mais meio segundo.
    """

    def __init__(self, min_chars: int = 12) -> None:
        self._min_chars = min_chars
        self._buffer = ""

    def push(self, delta: str) -> list[str]:
        """Adiciona um pedaco e devolve as frases prontas (pode ser vazio)."""
        self._buffer += delta
        prontas: list[str] = []
        while True:
            casou = FIM_DE_FRASE.search(self._buffer)
            if casou is None:
                break
            corte = casou.end()
            frase = self._buffer[:corte].strip()
            if len(frase) < self._min_chars:
                # Pontuacao cedo demais (abreviacao, numero): segue acumulando.
                proxima = FIM_DE_FRASE.search(self._buffer, corte)
                if proxima is None:
                    break
                corte = proxima.end()
                frase = self._buffer[:corte].strip()
            self._buffer = self._buffer[corte:]
            if frase:
                prontas.append(frase)
        return prontas

    def flush(self) -> str:
        """Devolve o resto e zera. Chame ao fim do stream."""
        resto, self._buffer = self._buffer.strip(), ""
        return resto


# --------------------------------------------------------------------- saida
class AudioPlayer(ABC):
    """Destino do audio sintetizado."""

    @abstractmethod
    async def play(self, chunks: AsyncIterator[bytes], *, sample_rate: int) -> int: ...

    @abstractmethod
    async def stop(self) -> None: ...


class NullPlayer(AudioPlayer):
    """Sem placa de som: conta bytes e loga. Mantem o pipeline testavel."""

    def __init__(self) -> None:
        self.bytes_tocados = 0

    async def play(self, chunks: AsyncIterator[bytes], *, sample_rate: int) -> int:
        total = 0
        async for pedaco in chunks:
            total += len(pedaco)
        self.bytes_tocados += total
        log.debug("tts.player_nulo", bytes=total, sample_rate=sample_rate)
        return total

    async def stop(self) -> None:
        return None


class SoundDevicePlayer(AudioPlayer):
    """Reproducao real via PortAudio (sounddevice)."""

    def __init__(self, device: str | int | None = None) -> None:
        self._device = device
        self._parar = asyncio.Event()

    async def play(self, chunks: AsyncIterator[bytes], *, sample_rate: int) -> int:
        import sounddevice as sd

        self._parar.clear()
        stream = sd.RawOutputStream(
            samplerate=sample_rate, channels=1, dtype="int16", device=self._device
        )
        stream.start()
        total = 0
        try:
            async for pedaco in chunks:
                if self._parar.is_set():
                    break
                await asyncio.to_thread(stream.write, pedaco)
                total += len(pedaco)
        finally:
            await asyncio.to_thread(stream.stop)
            await asyncio.to_thread(stream.close)
        return total

    async def stop(self) -> None:
        self._parar.set()


def criar_player(settings: Settings) -> AudioPlayer:
    """Player real quando ha PortAudio; nulo quando nao ha."""
    try:
        import sounddevice  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - ausencia de audio e degradacao, nao erro
        log.warning("tts.sem_saida_de_audio", erro=f"{type(exc).__name__}: {exc}")
        return NullPlayer()
    return SoundDevicePlayer(settings.audio_output_device)


# -------------------------------------------------------------------- motores
class TTSEngine(ABC):
    """Um sintetizador. ``stream`` devolve PCM 16-bit mono."""

    name: str
    sample_rate: int = 16000

    @abstractmethod
    async def available(self) -> bool: ...

    @abstractmethod
    def stream(self, texto: str) -> AsyncIterator[bytes]: ...


class ElevenLabsEngine(TTSEngine):
    """Voz primaria. Streaming PCM - nunca a API de arquivo inteiro."""

    name = "elevenlabs"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.sample_rate = 16000

    async def available(self) -> bool:
        return (
            self._settings.elevenlabs_api_key is not None
            and bool(self._settings.elevenlabs_voice_id)
        )

    async def stream(self, texto: str) -> AsyncIterator[bytes]:  # type: ignore[override]
        import httpx

        assert self._settings.elevenlabs_api_key is not None
        url = (
            "https://api.elevenlabs.io/v1/text-to-speech/"
            f"{self._settings.elevenlabs_voice_id}/stream"
        )
        async with httpx.AsyncClient(timeout=30.0) as http:
            async with http.stream(
                "POST",
                url,
                params={"output_format": f"pcm_{self.sample_rate}"},
                headers={"xi-api-key": self._settings.elevenlabs_api_key.get_secret_value()},
                json={"text": texto, "model_id": self._settings.elevenlabs_model},
            ) as resposta:
                resposta.raise_for_status()
                async for pedaco in resposta.aiter_bytes():
                    if pedaco:
                        yield pedaco


class PiperEngine(TTSEngine):
    """Fallback offline. Roda como processo, escreve PCM cru no stdout."""

    name = "piper"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.sample_rate = 22050

    async def available(self) -> bool:
        caminho = self._settings.piper_model_path
        return (
            caminho is not None
            and caminho.exists()
            and shutil.which(self._settings.piper_binary) is not None
        )

    async def stream(self, texto: str) -> AsyncIterator[bytes]:  # type: ignore[override]
        assert self._settings.piper_model_path is not None
        processo = await asyncio.create_subprocess_exec(
            self._settings.piper_binary,
            "--model",
            str(self._settings.piper_model_path),
            "--output_raw",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert processo.stdin is not None and processo.stdout is not None
        processo.stdin.write(texto.encode("utf-8"))
        await processo.stdin.drain()
        processo.stdin.close()
        try:
            while pedaco := await processo.stdout.read(4096):
                yield pedaco
        finally:
            await processo.wait()


class SystemVoiceEngine(TTSEngine):
    """Ultimo recurso no Windows: SAPI via PowerShell. Sem dependencia extra."""

    name = "sistema"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.sample_rate = 16000

    async def available(self) -> bool:
        import sys

        return sys.platform == "win32" and shutil.which("powershell") is not None

    async def stream(self, texto: str) -> AsyncIterator[bytes]:  # type: ignore[override]
        destino = Path(tempfile.gettempdir()) / f"optmus_tts_{id(texto):x}.wav"
        escapado = texto.replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
            f"{self.sample_rate}, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
            "[System.Speech.AudioFormat.AudioChannel]::Mono); "
            f"$s.SetOutputToWaveFile('{destino}', $f); $s.Speak('{escapado}'); $s.Dispose()"
        )
        processo = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-NonInteractive", "-Command", script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await processo.wait()
        try:
            dados = await asyncio.to_thread(destino.read_bytes)
            yield dados[CABECALHO_WAV:]
        finally:
            destino.unlink(missing_ok=True)


# ---------------------------------------------------------------- orquestrador
class SpeechSynthesizer:
    """Fala texto usando o primeiro motor disponivel da cadeia."""

    def __init__(
        self,
        engines: list[TTSEngine],
        player: AudioPlayer,
        *,
        on_first_audio: Callable[[], Any] | None = None,
    ) -> None:
        self._engines = engines
        self._player = player
        self._on_first_audio = on_first_audio
        self._motor: TTSEngine | None = None
        self._parar = asyncio.Event()
        self.motor_ativo: str | None = None

    def set_first_audio_hook(self, callback: Callable[[], Any] | None) -> None:
        """Quem marca a latencia ``wake -> primeira silaba`` troca de turno."""
        self._on_first_audio = callback

    async def _selecionar(self) -> TTSEngine | None:
        if self._motor is not None:
            return self._motor
        for engine in self._engines:
            try:
                if await engine.available():
                    self._motor = engine
                    self.motor_ativo = engine.name
                    log.info("tts.motor_selecionado", motor=engine.name)
                    return engine
            except Exception as exc:  # noqa: BLE001
                log.warning("tts.motor_indisponivel", motor=engine.name, erro=str(exc))
        log.warning("tts.mudo", motivo="nenhum motor de voz disponivel")
        return None

    async def speak(self, texto: str) -> bool:
        """Sintetiza e toca uma frase. ``False`` se ninguem falou."""
        texto = texto.strip()
        if not texto or self._parar.is_set():
            return False

        engine = await self._selecionar()
        if engine is None:
            log.info("tts.texto_sem_voz", texto=texto)
            return False

        primeiro = True

        async def _com_marco() -> AsyncIterator[bytes]:
            nonlocal primeiro
            async for pedaco in engine.stream(texto):
                if primeiro:
                    primeiro = False
                    if self._on_first_audio is not None:
                        self._on_first_audio()
                if self._parar.is_set():
                    break
                yield pedaco

        try:
            await self._player.play(_com_marco(), sample_rate=engine.sample_rate)
            return True
        except Exception as exc:  # noqa: BLE001 - falha de voz nao derruba o turno
            log.error("tts.falha", motor=engine.name, erro=f"{type(exc).__name__}: {exc}")
            self._motor = None
            self.motor_ativo = None
            return False

    async def speak_stream(self, deltas: AsyncIterator[str]) -> str:
        """Consome os deltas do LLM e fala frase a frase, sem esperar o fim."""
        buffer = SentenceBuffer()
        completo: list[str] = []
        async for delta in deltas:
            completo.append(delta)
            for frase in buffer.push(delta):
                if self._parar.is_set():
                    break
                await self.speak(frase)
        resto = buffer.flush()
        if resto and not self._parar.is_set():
            await self.speak(resto)
        return "".join(completo)

    async def stop(self) -> None:
        """Kill switch: corta a fala em andamento."""
        self._parar.set()
        await self._player.stop()

    def resume(self) -> None:
        self._parar.clear()


def criar_sintetizador(
    settings: Settings, *, on_first_audio: Callable[[], Any] | None = None
) -> SpeechSynthesizer:
    """Cadeia padrao: ElevenLabs -> Piper -> voz do sistema."""
    return SpeechSynthesizer(
        [ElevenLabsEngine(settings), PiperEngine(settings), SystemVoiceEngine(settings)],
        criar_player(settings),
        on_first_audio=on_first_audio,
    )
