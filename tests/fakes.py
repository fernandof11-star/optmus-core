"""Dublês do hardware e da nuvem.

A suite roda sem microfone, sem placa de som, sem chave de API e sem modelo
baixado. Isso nao e conveniencia de CI: e o que permite testar a ordem das
etapas e a latencia do pipeline sem depender do ambiente de quem roda.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from core.llm import Imagem, LLMClient, LLMTurn, TextSink, ToolCall
from expression.tts import AudioPlayer, TTSEngine


class FakeLLM(LLMClient):
    """Devolve turnos pre-programados, um por rodada, com streaming real."""

    name = "fake"

    def __init__(self, turnos: list[LLMTurn], *, atraso_s: float = 0.0) -> None:
        self._turnos = list(turnos)
        self._atraso = atraso_s
        self.chamadas: list[dict[str, Any]] = []

    async def available(self) -> bool:
        return True

    async def stream_turn(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: TextSink | None = None,
    ) -> LLMTurn:
        self.chamadas.append({"system": system, "messages": list(messages), "tools": tools})
        turno = self._turnos.pop(0) if self._turnos else LLMTurn(text="", stop_reason="end_turn")
        if on_text is not None and turno.text:
            for pedaco in _em_pedacos(turno.text):
                if self._atraso:
                    await asyncio.sleep(self._atraso)
                await on_text(pedaco)
        return turno


def _em_pedacos(texto: str, tamanho: int = 7) -> list[str]:
    return [texto[i : i + tamanho] for i in range(0, len(texto), tamanho)]


def turno_de_ferramenta(nome: str, entrada: dict[str, Any], *, id_: str = "tu_1") -> LLMTurn:
    return LLMTurn(
        text="",
        tool_calls=[ToolCall(id=id_, name=nome, input=entrada)],
        stop_reason="tool_use",
        assistant_content=[{"type": "tool_use", "id": id_, "name": nome, "input": entrada}],
    )


class FakeTools:
    """Registro de ferramentas de mentira (o de verdade chega na F3)."""

    def __init__(
        self,
        respostas: dict[str, str] | None = None,
        *,
        imagens: dict[str, Imagem] | None = None,
    ) -> None:
        self._respostas = respostas or {}
        self._imagens = imagens or {}
        self.executadas: list[tuple[str, dict[str, Any]]] = []

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": nome,
                "description": f"ferramenta de teste {nome}",
                "input_schema": {"type": "object", "properties": {}},
            }
            for nome in self._respostas
        ]

    async def execute(
        self, name: str, arguments: dict[str, Any], *, correlation_id: str | None = None
    ) -> Any:
        from core.agent import ToolOutcome

        self.executadas.append((name, arguments))
        if name not in self._respostas:
            return ToolOutcome(f"ferramenta desconhecida: {name}", is_error=True)
        imagem = self._imagens.get(name)
        return ToolOutcome(
            self._respostas[name], imagens=(imagem,) if imagem is not None else ()
        )


class FakeTTSEngine(TTSEngine):
    """Sintetiza 100 bytes de "audio" por frase e registra o que foi falado."""

    def __init__(self, name: str = "fake", *, disponivel: bool = True) -> None:
        self.name = name
        self.sample_rate = 16000
        self._disponivel = disponivel
        self.falas: list[str] = []

    async def available(self) -> bool:
        return self._disponivel

    async def stream(self, texto: str) -> AsyncIterator[bytes]:  # type: ignore[override]
        self.falas.append(texto)
        yield b"\x00" * 100


class ExplodindoTTSEngine(FakeTTSEngine):
    """Diz que esta disponivel e falha ao sintetizar - o pior caso real."""

    async def stream(self, texto: str) -> AsyncIterator[bytes]:  # type: ignore[override]
        raise RuntimeError("motor de voz caiu no meio da frase")
        yield b""  # pragma: no cover - inalcancavel, mantem o tipo de gerador


class GravadorDePlayer(AudioPlayer):
    def __init__(self) -> None:
        self.bytes_tocados = 0
        self.parou = False

    async def play(self, chunks: AsyncIterator[bytes], *, sample_rate: int) -> int:
        total = 0
        async for pedaco in chunks:
            total += len(pedaco)
        self.bytes_tocados += total
        return total

    async def stop(self) -> None:
        self.parou = True


class FakeTranscriber:
    """Devolve textos na ordem, com latencia medida como o real."""

    def __init__(self, textos: list[str]) -> None:
        self._textos = list(textos)
        self.chamadas = 0

    async def load(self) -> None:
        return None

    async def transcribe(self, pcm: bytes) -> Any:
        from perception.stt import Transcription

        self.chamadas += 1
        texto = self._textos.pop(0) if self._textos else ""
        return Transcription(text=texto, duracao_ms=12.0, audio_ms=800.0, idioma="pt")


class FakeWake:
    """Dispara N vezes e depois encerra o fluxo."""

    name = "fake"

    def __init__(self, disparos: int = 1) -> None:
        self._restantes = disparos

    async def wait_for_wake(self, frames: AsyncIterator[bytes]) -> bool:
        if self._restantes <= 0:
            return False
        self._restantes -= 1
        return True

    async def trigger(self) -> None:
        self._restantes += 1


async def frames_de_teste(quantidade: int, *, com_voz: int) -> AsyncIterator[bytes]:
    """`com_voz` frames barulhentos seguidos de silencio."""
    alto = (b"\x00\x40" * 160)
    baixo = b"\x00\x00" * 160
    for i in range(quantidade):
        yield alto if i < com_voz else baixo
