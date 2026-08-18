"""Camada 2 - memoria episodica.

O que aconteceu, quando e com quem. Um episodio e um registro datado de um
evento: uma conversa, uma acao executada, um dispositivo que caiu.

Meia-vida curta (14 dias por padrao): o que aconteceu terca retrasada importa
menos que o que aconteceu ontem. Nada e apagado - so pesa menos na busca.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.logging import get_logger
from memory.layer import VectorMemoryLayer

log = get_logger("memory.episodic")


class EpisodicMemory(VectorMemoryLayer):
    layer = "episodica"
    meia_vida_dias = 14.0

    async def record(
        self,
        content: str,
        *,
        source: str = "conversa",
        participantes: list[str] | None = None,
        correlation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Grava um episodio. Chamado ao fim de cada turno de voz."""
        extra: dict[str, Any] = {
            "participantes": participantes or [],
            "correlation_id": correlation_id,
            **(metadata or {}),
        }
        memory_id = await self._gravar(content, source=source, confidence=1.0, metadata=extra)
        log.debug("memoria.episodio", memory_id=memory_id, source=source)
        return memory_id

    async def record_exchange(
        self,
        pergunta: str,
        resposta: str,
        *,
        correlation_id: str | None = None,
        source: str = "conversa",
    ) -> int:
        """Um turno completo vira um episodio unico e legivel.

        Guardar pergunta e resposta separadas quebra o contexto na hora de
        recuperar: "onde eu moro" sozinho nao diz nada sem "Sao Paulo".
        """
        return await self.record(
            f"{pergunta.strip()} -> {resposta.strip()}",
            source=source,
            correlation_id=correlation_id,
            metadata={"pergunta": pergunta, "resposta": resposta},
        )

    async def do_dia(self, *, dias: int = 1, limit: int = 500) -> list[dict[str, Any]]:
        """Episodios ainda nao digeridos pelo consolidador."""
        desde = (datetime.now(UTC) - timedelta(days=dias)).isoformat(timespec="milliseconds")
        return await self._store.list_memories(
            layer=self.layer,
            pendentes_de_consolidacao=True,
            desde=desde,
            limit=limit,
        )

    async def pendentes(self, limit: int = 500) -> list[dict[str, Any]]:
        return await self._store.list_memories(
            layer=self.layer, pendentes_de_consolidacao=True, limit=limit
        )
