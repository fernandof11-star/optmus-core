"""Fachada das quatro camadas de memoria.

Quem usa memoria (loop de voz, agente, futuras ferramentas da F3) fala com
:class:`MemorySystem`, nao com as camadas soltas. Duas razoes:

- **Recuperar e uma decisao, nao uma chamada.** ``recall`` consulta episodica e
  semantica juntas, funde por score e devolve um bloco pronto para o prompt.
  Espalhar essa politica pelos chamadores garante que cada um faca diferente.
- **A memoria e servico, nao variavel** (secao 3.3). Uma fachada com API
  propria e versionavel e auditavel; um dicionario global nao.
"""

from __future__ import annotations

from typing import Any

from core.config import Settings
from core.logging import get_logger
from memory.embeddings import EmbeddingProvider, conferir_dimensao, criar_provedor
from memory.episodic import EpisodicMemory
from memory.procedural import ProceduralMemory
from memory.profile import LivingProfile
from memory.scoring import MemoryHit
from memory.semantic import SemanticMemory
from memory.store import Store
from memory.working import WorkingMemory

log = get_logger("memory.system")


class MemorySystem:
    """As quatro camadas mais o perfil vivo, montadas e prontas."""

    def __init__(
        self, settings: Settings, store: Store, *, embedder: EmbeddingProvider | None = None
    ) -> None:
        self._settings = settings
        self._store = store
        self.embedder = embedder or criar_provedor(settings)

        self.working = WorkingMemory(
            ttl_minutes=settings.working_memory_ttl_min,
            max_turns=settings.working_memory_turns,
        )
        self.episodic = EpisodicMemory(
            store, self.embedder, meia_vida_dias=settings.episodic_half_life_days
        )
        self.semantic = SemanticMemory(
            store, self.embedder, meia_vida_dias=settings.semantic_half_life_days
        )
        self.procedural = ProceduralMemory(
            store, min_ocorrencias=settings.procedural_min_occurrences
        )
        self.profile = LivingProfile(settings.profile_file)
        self.aviso_dimensao: str | None = None

    async def start(self) -> None:
        """Confere o contrato do vetor e garante o arquivo de perfil."""
        self.aviso_dimensao = await conferir_dimensao(self._store, self.embedder)
        if self.aviso_dimensao:
            log.error("memoria.embedding_incompativel", aviso=self.aviso_dimensao)
        await self.profile.ensure()

    # ------------------------------------------------------------ escrita
    async def record_turn(
        self, pergunta: str, resposta: str, *, correlation_id: str | None = None
    ) -> int | None:
        """Fecha um turno: memoria de trabalho + episodio permanente."""
        if not resposta.strip():
            return None
        self.working.add_exchange(pergunta, resposta)
        return await self.episodic.record_exchange(
            pergunta, resposta, correlation_id=correlation_id
        )

    # ---------------------------------------------------------- leitura
    async def recall(
        self, consulta: str, *, limit: int | None = None, min_score: float | None = None
    ) -> list[MemoryHit]:
        """Busca nas camadas permanentes e funde por score."""
        limite = limit if limit is not None else self._settings.recall_limit
        corte = min_score if min_score is not None else self._settings.recall_min_score

        episodios = await self.episodic.recall(consulta, limit=limite, min_score=corte)
        fatos = await self.semantic.recall(consulta, limit=limite, min_score=corte)
        fundidos = sorted([*fatos, *episodios], key=lambda h: h.score, reverse=True)
        return fundidos[:limite]

    async def context_for(self, consulta: str) -> str:
        """Bloco de contexto para o prompt de sistema: perfil + recuperado.

        Vazio quando nao ha nada relevante - injetar cabecalho vazio so ensina
        o modelo a ignorar a secao.
        """
        partes: list[str] = []

        perfil = await self.profile.for_prompt()
        if perfil:
            partes.append(f"<perfil>\n{perfil}\n</perfil>")

        lembrancas = await self.recall(consulta)
        if lembrancas:
            linhas = "\n".join(
                f"- [{h.layer}] {h.content}" for h in lembrancas
            )
            partes.append(
                "<memoria>\n"
                "Recuperado da memoria. Use se for pertinente; ignore se nao for, "
                "e nao mencione que consultou memoria.\n"
                f"{linhas}\n"
                "</memoria>"
            )
        return "\n\n".join(partes)

    # ------------------------------------------------------------ estado
    async def stats(self) -> dict[str, Any]:
        contagem = await self._store.count_memories()
        return {
            "embedder": self.embedder.name,
            "semantico": self.embedder.semantico,
            "dimensao": self.embedder.dim,
            "busca_vetorial": self._store.vector_search_available,
            "aviso": self.aviso_dimensao,
            "trabalho": self.working.stats(),
            "episodica": contagem.get("episodica", 0),
            "semantica": contagem.get("semantica", 0),
            "procedural": contagem.get("procedural", 0),
            "superadas": contagem.get("superadas", 0),
            "perfil": str(self.profile.caminho),
        }

    async def reindexar(self, *, lote: int = 200) -> int:
        """Recalcula todos os vetores com o provedor atual.

        Necessario ao trocar de modelo de embedding: vetores de modelos
        diferentes nao sao comparaveis, e a busca degrada em silencio.
        """
        total = 0
        for layer in ("episodica", "semantica"):
            linhas = await self._store.list_memories(layer=layer, vigentes=False, limit=100000)
            for i in range(0, len(linhas), lote):
                fatia = linhas[i : i + lote]
                vetores = await self.embedder.embed([str(x["content"]) for x in fatia])
                for linha, vetor in zip(fatia, vetores, strict=True):
                    if await self._store.upsert_vector(int(linha["id"]), vetor):
                        total += 1
        await self._store.meta_set("embedding_dim", str(self.embedder.dim))
        await self._store.meta_set("embedding_provider", self.embedder.name)
        self.aviso_dimensao = None
        log.info("memoria.reindexada", vetores=total, provedor=self.embedder.name)
        return total
