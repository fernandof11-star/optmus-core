"""Decaimento de relevancia.

    score = similaridade x recencia x frequencia

Sem o decaimento, uma conversa de seis meses atras compete de igual para igual
com a de ontem e o assistente fica burro com o tempo: ele lembra de tudo e
recupera o irrelevante.

Os tres fatores respondem a perguntas diferentes:

- **similaridade** - isto fala do que ele perguntou?
- **recencia** - isto ainda vale? Decai por meia-vida, configuravel por camada:
  episodio envelhece rapido (o que aconteceu terca passada importa menos),
  fato semantico envelhece devagar (onde ele mora nao muda toda semana).
- **frequencia** - isto se provou util antes? Cresce em log, nao linear, senao
  a memoria mais acessada domina toda busca para sempre.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class MemoryHit:
    """Uma memoria recuperada, com a conta que a colocou ali."""

    id: int
    layer: str
    content: str
    source: str
    confidence: float
    created_at: str
    last_access: str
    access_count: int
    similaridade: float
    recencia: float
    frequencia: float
    score: float
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "layer": self.layer,
            "content": self.content,
            "source": self.source,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "score": round(self.score, 4),
            "similaridade": round(self.similaridade, 4),
            "recencia": round(self.recencia, 4),
            "frequencia": round(self.frequencia, 4),
        }


def similaridade_de_distancia(distancia: float | None) -> float:
    """Converte distancia L2 de vetor normalizado em cosseno: 1 - d^2/2."""
    if distancia is None:
        return 0.0
    return max(0.0, min(1.0, 1.0 - (distancia * distancia) / 2.0))


def idade_em_dias(iso: str, *, agora: datetime | None = None) -> float:
    referencia = agora or datetime.now(UTC)
    try:
        quando = datetime.fromisoformat(iso)
    except ValueError:
        return 0.0
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=UTC)
    return max(0.0, (referencia - quando).total_seconds() / 86400)


def fator_recencia(iso: str, meia_vida_dias: float, *, agora: datetime | None = None) -> float:
    """1.0 hoje, 0.5 em uma meia-vida, nunca zero."""
    if meia_vida_dias <= 0:
        return 1.0
    return float(0.5 ** (idade_em_dias(iso, agora=agora) / meia_vida_dias))


def fator_frequencia(acessos: int) -> float:
    """Cresce em log e satura perto de 2: util e favorecido, nao dominante."""
    return 1.0 + math.log1p(max(0, acessos)) / math.log(10)


def pontuar(
    linha: dict[str, Any],
    *,
    similaridade: float,
    meia_vida_dias: float,
    agora: datetime | None = None,
) -> MemoryHit:
    import json

    recencia = fator_recencia(str(linha["created_at"]), meia_vida_dias, agora=agora)
    frequencia = fator_frequencia(int(linha.get("access_count", 0)))
    confianca = float(linha.get("confidence", 0.5))
    metadata = linha.get("metadata") or "{}"

    return MemoryHit(
        id=int(linha["id"]),
        layer=str(linha["layer"]),
        content=str(linha["content"]),
        source=str(linha["source"]),
        confidence=confianca,
        created_at=str(linha["created_at"]),
        last_access=str(linha["last_access"]),
        access_count=int(linha.get("access_count", 0)),
        similaridade=similaridade,
        recencia=recencia,
        frequencia=frequencia,
        score=similaridade * recencia * frequencia * confianca,
        metadata=json.loads(metadata) if isinstance(metadata, str) else dict(metadata),
    )
