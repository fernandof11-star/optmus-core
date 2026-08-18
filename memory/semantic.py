"""Camada 3 - memoria semantica.

Fatos sobre o usuario e o mundo dele: onde mora, como toma cafe, quem e o
contador, qual projeto esta ativo. Permanente e atualizavel, com meia-vida
longa - onde ele mora nao muda toda semana.

**Contradicao nao sobrescreve.** Quando um fato novo contradiz um antigo, o
antigo e marcado como superado com data e continua no banco. Duas razoes:
auditoria (por que o Optmus achava X em marco?) e reversao (a "correcao" pode
ter vindo de uma transcricao errada). Historico e valioso; sobrescrever e
escolher a verdade errada sem deixar rastro.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.logging import get_logger
from memory.layer import VectorMemoryLayer

log = get_logger("memory.semantic")


@dataclass(frozen=True, slots=True)
class FatoGravado:
    id: int
    conteudo: str
    superou: int | None = None

    @property
    def corrigiu(self) -> bool:
        return self.superou is not None


class SemanticMemory(VectorMemoryLayer):
    layer = "semantica"
    meia_vida_dias = 180.0

    async def remember(
        self,
        conteudo: str,
        *,
        source: str = "conversa",
        confidence: float = 0.7,
        supersedes: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> FatoGravado:
        """Grava um fato. ``supersedes`` versiona o fato antigo, nao o apaga."""
        memory_id = await self._gravar(
            conteudo, source=source, confidence=confidence, metadata=metadata
        )
        if supersedes is not None:
            await self._store.supersede_memory(supersedes, memory_id)
            log.info("memoria.fato_corrigido", antigo=supersedes, novo=memory_id)
        return FatoGravado(id=memory_id, conteudo=conteudo, superou=supersedes)

    async def contradict(
        self,
        antigo_id: int,
        novo_conteudo: str,
        *,
        source: str = "conversa",
        confidence: float = 0.8,
    ) -> FatoGravado:
        """Atalho explicito para "isto mudou": grava o novo e supera o antigo."""
        return await self.remember(
            novo_conteudo, source=source, confidence=confidence, supersedes=antigo_id
        )

    async def vigentes(self, limit: int = 200) -> list[dict[str, Any]]:
        """Fatos que ainda valem - os superados ficam fora."""
        return await self._store.list_memories(layer=self.layer, vigentes=True, limit=limit)

    async def historico(self, limit: int = 200) -> list[dict[str, Any]]:
        """Tudo, inclusive o que foi corrigido. E a trilha de auditoria."""
        return await self._store.list_memories(layer=self.layer, vigentes=False, limit=limit)

    async def ja_sabe(self, conteudo: str, *, limiar: float = 0.92) -> int | None:
        """Devolve o id de um fato praticamente igual, se existir.

        Evita que o consolidador grave "mora em Sao Paulo" toda noite.
        """
        parecidos = await self.recall(
            conteudo, limit=1, k_candidatos=5, registrar_acesso=False
        )
        if parecidos and parecidos[0].similaridade >= limiar:
            return parecidos[0].id
        return None
