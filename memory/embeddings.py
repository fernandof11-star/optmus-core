"""Embeddings locais.

Dois provedores atras da mesma interface:

- :class:`FastEmbedProvider` - modelo multilingue via ONNX, roda em CPU, sem
  GPU e sem mandar texto para lugar nenhum. E o unico com semantica de verdade:
  entende que "quem cuida do meu imposto" fala do contador.
- :class:`HashingEmbedder` - fallback deterministico sem dependencia nenhuma.
  **Isto e busca lexical disfarcada de vetor.** Casa palavra e pedaco de
  palavra, nao significado. Serve para o sistema funcionar offline, no primeiro
  boot e nos testes - nao para cumprir a promessa de memoria semantica.

A dimensao e contrato do banco: a tabela ``memory_vectors`` nasce com a largura
de ``OPTMUS_EMBEDDING_DIM`` e mudar isso depois exige reindexar tudo. Por isso
o provedor efetivo e a dimensao ficam gravados na tabela ``meta`` e sao
conferidos no boot.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, Final

from core.config import Settings
from core.logging import get_logger

log = get_logger("memory.embeddings")

CHAVE_META_DIM: Final[str] = "embedding_dim"
CHAVE_META_PROVEDOR: Final[str] = "embedding_provider"
_TOKEN: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")


class EmbeddingProvider(ABC):
    """Transforma texto em vetor normalizado (norma 1)."""

    name: str
    dim: int
    semantico: bool

    @abstractmethod
    async def embed(self, textos: Sequence[str]) -> list[list[float]]: ...

    async def embed_one(self, texto: str) -> list[float]:
        return (await self.embed([texto]))[0]


def tokenizar(texto: str) -> list[str]:
    """Minusculas, sem acento, so alfanumerico."""
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFD", texto.lower())
        if unicodedata.category(c) != "Mn"
    )
    return _TOKEN.findall(sem_acento)


class HashingEmbedder(EmbeddingProvider):
    """Bag-of-ngrams com hashing. Deterministico, sem dependencia, lexical."""

    name = "hashing"
    semantico = False

    def __init__(self, dim: int, *, ngramas: tuple[int, ...] = (3, 4)) -> None:
        self.dim = dim
        self._ngramas = ngramas

    async def embed(self, textos: Sequence[str]) -> list[list[float]]:
        return [self._vetor(t) for t in textos]

    def _vetor(self, texto: str) -> list[float]:
        vetor = [0.0] * self.dim
        tokens = tokenizar(texto)
        for token in tokens:
            self._somar(vetor, f"w:{token}", 1.0)
            for n in self._ngramas:
                for i in range(max(1, len(token) - n + 1)):
                    self._somar(vetor, f"g:{token[i : i + n]}", 0.5)
        return _normalizar(vetor)

    def _somar(self, vetor: list[float], chave: str, peso: float) -> None:
        digest = hashlib.blake2b(chave.encode("utf-8"), digest_size=8).digest()
        indice = int.from_bytes(digest[:4], "big") % self.dim
        sinal = 1.0 if digest[4] & 1 else -1.0
        vetor[indice] += sinal * peso


class FastEmbedProvider(EmbeddingProvider):
    """Modelo ONNX multilingue. Semantica de verdade, ainda 100% local."""

    name = "fastembed"
    semantico = True

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._modelo: Any = None
        self.dim = settings.embedding_dim

    def carregar(self) -> None:
        if self._modelo is not None:
            return
        from fastembed import TextEmbedding

        self._modelo = TextEmbedding(model_name=self._settings.embedding_model)
        descricao = next(
            (
                m for m in TextEmbedding.list_supported_models()
                if m["model"] == self._settings.embedding_model
            ),
            None,
        )
        if descricao is not None:
            self.dim = int(descricao["dim"])
        log.info("embeddings.modelo_carregado", modelo=self._settings.embedding_model, dim=self.dim)

    async def embed(self, textos: Sequence[str]) -> list[list[float]]:
        import asyncio

        self.carregar()
        vetores = await asyncio.to_thread(lambda: list(self._modelo.embed(list(textos))))
        return [_normalizar([float(x) for x in v]) for v in vetores]


def _normalizar(vetor: list[float]) -> list[float]:
    norma = math.sqrt(sum(x * x for x in vetor))
    if norma == 0.0:
        return vetor
    return [x / norma for x in vetor]


def criar_provedor(settings: Settings) -> EmbeddingProvider:
    """Resolve o provedor conforme ``OPTMUS_EMBEDDING_PROVIDER``.

    ``auto`` tenta o fastembed e cai no hashing sem derrubar nada - mas avisa,
    porque a diferenca entre os dois e a diferenca entre lembrar e nao lembrar.
    """
    escolha = settings.embedding_provider.lower()
    if escolha == "hashing":
        return HashingEmbedder(settings.embedding_dim)

    provedor = FastEmbedProvider(settings)
    try:
        provedor.carregar()
    except Exception as exc:
        if escolha == "fastembed":
            raise
        log.warning(
            "embeddings.fastembed_indisponivel",
            erro=f"{type(exc).__name__}: {exc}",
            impacto="memoria cai para busca lexical: acha palavra, nao significado",
            acao='pip install -e ".[memoria]"',
        )
        return HashingEmbedder(settings.embedding_dim)
    return provedor


async def conferir_dimensao(store: Any, provedor: EmbeddingProvider) -> str | None:
    """Compara o provedor atual com o que gravou os vetores do banco.

    Trocar de modelo sem reindexar nao da erro no SQLite - da resultado errado
    em silencio, que e pior. Devolve a mensagem do problema, ou ``None``.
    """
    dim_gravada = await store.meta_get(CHAVE_META_DIM)
    provedor_gravado = await store.meta_get(CHAVE_META_PROVEDOR)

    if dim_gravada is None:
        await store.meta_set(CHAVE_META_DIM, str(provedor.dim))
        await store.meta_set(CHAVE_META_PROVEDOR, provedor.name)
        return None

    if int(dim_gravada) != provedor.dim:
        return (
            f"dimensao de embedding mudou ({dim_gravada} -> {provedor.dim}): "
            "os vetores existentes nao servem mais. Reindexe com "
            "POST /memoria/reindexar."
        )
    if provedor_gravado != provedor.name:
        return (
            f"provedor de embedding mudou ({provedor_gravado} -> {provedor.name}): "
            "vetores antigos e novos nao sao comparaveis. Reindexe com "
            "POST /memoria/reindexar."
        )
    return None
