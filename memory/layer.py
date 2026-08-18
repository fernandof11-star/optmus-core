"""Base comum das camadas vetoriais (episodica e semantica).

As duas guardam texto, vetor e metadado no mesmo lugar e recuperam pelo mesmo
score. O que muda entre elas e a *politica*: meia-vida do decaimento, o que
conta como fonte e o que acontece quando dois registros se contradizem. Essa
politica mora nas subclasses; a mecanica mora aqui.

Sem vetores (sqlite-vec fora, ou embedding indisponivel), a recuperacao cai
para casamento de termos. Piora muito - e por isso aparece no /health.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.logging import get_logger
from memory.embeddings import EmbeddingProvider, tokenizar
from memory.scoring import MemoryHit, pontuar, similaridade_de_distancia
from memory.store import Store

log = get_logger("memory.layer")


class VectorMemoryLayer:
    """Gravacao e recuperacao com score de relevancia."""

    layer: str = "episodica"
    meia_vida_dias: float = 14.0

    def __init__(
        self,
        store: Store,
        embedder: EmbeddingProvider,
        *,
        meia_vida_dias: float | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        if meia_vida_dias is not None:
            self.meia_vida_dias = meia_vida_dias

    @property
    def store(self) -> Store:
        return self._store

    async def _gravar(
        self,
        content: str,
        *,
        source: str,
        confidence: float = 0.5,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        memory_id = await self._store.insert_memory(
            layer=self.layer,
            content=content,
            source=source,
            confidence=confidence,
            metadata=metadata,
        )
        try:
            vetor = await self._embedder.embed_one(content)
            await self._store.upsert_vector(memory_id, vetor)
        except Exception as exc:  # noqa: BLE001 - memoria sem vetor > memoria perdida
            log.error(
                "memoria.vetor_falhou",
                memory_id=memory_id,
                erro=f"{type(exc).__name__}: {exc}",
                impacto="registro salvo, mas so recuperavel por termo",
            )
        return memory_id

    async def recall(
        self,
        consulta: str,
        *,
        limit: int = 5,
        min_score: float = 0.0,
        k_candidatos: int = 30,
        registrar_acesso: bool = True,
        agora: datetime | None = None,
    ) -> list[MemoryHit]:
        """Busca por relevancia: similaridade x recencia x frequencia."""
        if not consulta.strip():
            return []

        linhas = await self._buscar(consulta, k_candidatos)
        agora = agora or datetime.now(UTC)
        pontuadas = [
            pontuar(
                linha,
                similaridade=similaridade_de_distancia(linha.get("distance"))
                if "distance" in linha
                else _similaridade_lexical(consulta, str(linha["content"])),
                meia_vida_dias=self.meia_vida_dias,
                agora=agora,
            )
            for linha in linhas
        ]
        melhores = sorted(
            (h for h in pontuadas if h.score >= min_score), key=lambda h: h.score, reverse=True
        )[:limit]

        if registrar_acesso and melhores:
            await self._store.touch_memories([h.id for h in melhores])
        return melhores

    async def _buscar(self, consulta: str, k: int) -> list[dict[str, Any]]:
        if self._store.vector_search_available:
            vetor = await self._embedder.embed_one(consulta)
            linhas = await self._store.vector_search(vetor, k=k, layers=[self.layer])
            if linhas:
                return linhas
        return await self._store.lexical_search(tokenizar(consulta), k=k, layers=[self.layer])

    async def count(self) -> int:
        contagem = await self._store.count_memories()
        return contagem.get(self.layer, 0)


def _similaridade_lexical(consulta: str, conteudo: str) -> float:
    """Jaccard de tokens. Fraco de proposito - so evita ordenar por nada."""
    a, b = set(tokenizar(consulta)), set(tokenizar(conteudo))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
