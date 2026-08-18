"""Wake word.

openWakeWord com modelo customizado para "Optmus". Local, custo zero.

**Nao pule o treino.** Um modelo treinado com audio limpo de estudio falha na
vida real. Sao necessarias ~500 amostras da SUA voz gravadas com o ruido do
SEU ambiente (ar-condicionado, teclado, TV ligada). Sem o modelo configurado,
o Core cai no gatilho manual - o loop de voz continua utilizavel pelo HUD e
pela API, so nao escuta sozinho.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from core.config import Settings
from core.logging import get_logger

log = get_logger("perception.wake")


class WakeDetector(ABC):
    """Espera pelo gatilho que abre um turno de voz."""

    name: str

    @abstractmethod
    async def wait_for_wake(self, frames: AsyncIterator[bytes]) -> bool:
        """Consome frames ate detectar o gatilho. ``False`` = fluxo encerrado."""

    async def trigger(self) -> None:
        """Gatilho externo (HUD, atalho, API)."""
        return None


class ManualWakeDetector(WakeDetector):
    """Sem modelo treinado: so dispara quando alguem chama :meth:`trigger`."""

    name = "manual"

    def __init__(self) -> None:
        self._evento = asyncio.Event()

    async def wait_for_wake(self, frames: AsyncIterator[bytes]) -> bool:
        await self._evento.wait()
        self._evento.clear()
        return True

    async def trigger(self) -> None:
        self._evento.set()


class OpenWakeWordDetector(WakeDetector):
    """Wake word real via openWakeWord, com gatilho manual em paralelo."""

    name = "openwakeword"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._modelo: Any = None
        self._manual = asyncio.Event()
        self._ultimo_disparo = 0.0

    def carregar(self) -> None:
        if self._modelo is not None:
            return
        caminho = self._settings.wake_model_path
        if caminho is None or not caminho.exists():
            raise FileNotFoundError(f"modelo de wake word ausente: {caminho}")
        from openwakeword.model import Model

        self._modelo = Model(wakeword_models=[str(caminho)])
        log.info("wake.modelo_carregado", caminho=str(caminho))

    async def wait_for_wake(self, frames: AsyncIterator[bytes]) -> bool:
        self.carregar()
        import numpy as np

        async for frame in frames:
            if self._manual.is_set():
                self._manual.clear()
                return True

            amostras = np.frombuffer(frame, dtype=np.int16)
            pontuacoes = await asyncio.to_thread(self._modelo.predict, amostras)
            maior = max(pontuacoes.values()) if pontuacoes else 0.0
            if maior < self._settings.wake_threshold:
                continue

            agora = time.monotonic()
            if agora - self._ultimo_disparo < self._settings.wake_cooldown_s:
                continue
            self._ultimo_disparo = agora
            log.info("wake.detectado", pontuacao=round(float(maior), 3))
            return True
        return False

    async def trigger(self) -> None:
        self._manual.set()


def criar_detector(settings: Settings) -> WakeDetector:
    """openWakeWord quando ha modelo treinado; gatilho manual quando nao ha."""
    caminho = settings.wake_model_path
    if caminho is not None and caminho.exists():
        return OpenWakeWordDetector(settings)
    log.warning(
        "wake.sem_modelo",
        impacto="escuta continua desligada; use o gatilho manual",
        acao="treine um modelo openWakeWord e aponte OPTMUS_WAKE_MODEL_PATH",
    )
    return ManualWakeDetector()
