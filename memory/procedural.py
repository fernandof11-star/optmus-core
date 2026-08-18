"""Camada 4 - memoria procedural.

Rotinas aprendidas: "toda sexta ele pede o resumo da semana", "sempre que
chega em casa manda tocar musica". Derivada, nao declarada - ninguem escreve
essas regras, elas sao inferidas dos episodios pelo consolidador.

O detector e deliberadamente conservador. Um padrao so vira rotina com um
minimo de ocorrencias **em semanas diferentes**: tres pedidos na mesma sexta-
feira sao uma tarefa, nao um habito. Padrao falso vira proatividade errada, e
proatividade errada na F7 e o motivo numero um de alguem desligar o assistente.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.logging import get_logger
from memory.embeddings import tokenizar
from memory.store import Store

log = get_logger("memory.procedural")

DIAS = ("segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo")
# Palavras sem valor de intencao: nao ajudam a agrupar pedidos parecidos.
VAZIAS = frozenset(
    """a as o os um uma de do da dos das em no na nos nas por para com sem
    me meu minha e ou que qual quais isso isto ai la aqui agora hoje ja
    optmus por favor""".split()
)


@dataclass(frozen=True, slots=True)
class Rotina:
    assinatura: str
    gatilho: str
    ocorrencias: int
    exemplos: list[str]
    dia_da_semana: int | None = None
    hora: int | None = None

    def descrever(self) -> str:
        quando = f"toda {DIAS[self.dia_da_semana]}" if self.dia_da_semana is not None else "sempre"
        if self.hora is not None:
            quando += f" por volta das {self.hora}h"
        return f"{quando}, o usuario costuma pedir: {self.assinatura}"


class ProceduralMemory:
    """Detecta e guarda rotinas derivadas dos episodios."""

    layer = "procedural"

    def __init__(self, store: Store, *, min_ocorrencias: int = 3) -> None:
        self._store = store
        self._min = min_ocorrencias

    def detectar(self, episodios: list[dict[str, Any]]) -> list[Rotina]:
        """Agrupa episodios por (dia da semana, intencao) e conta repeticoes."""
        grupos: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
        for episodio in episodios:
            quando = _quando(episodio)
            if quando is None:
                continue
            assinatura = _assinatura(str(episodio.get("content", "")))
            if not assinatura:
                continue
            grupos[(quando.weekday(), assinatura)].append(episodio)

        rotinas: list[Rotina] = []
        for (dia, assinatura), itens in grupos.items():
            semanas = {
                _quando(i).isocalendar()[:2]  # type: ignore[union-attr]
                for i in itens
                if _quando(i) is not None
            }
            if len(itens) < self._min or len(semanas) < self._min:
                continue
            horas = [q.hour for i in itens if (q := _quando(i)) is not None]
            rotinas.append(
                Rotina(
                    assinatura=assinatura,
                    gatilho=f"dia_da_semana={dia}",
                    ocorrencias=len(itens),
                    exemplos=[str(i.get("content", ""))[:120] for i in itens[:3]],
                    dia_da_semana=dia,
                    hora=round(sum(horas) / len(horas)) if horas else None,
                )
            )
        return rotinas

    async def registrar(self, rotina: Rotina) -> int | None:
        """Grava a rotina. Se ja existe uma igual, so reforca a contagem."""
        existente = await self._store.fetchone(
            "SELECT id, metadata FROM memories "
            "WHERE layer = ? AND superseded_by IS NULL AND content = ?",
            (self.layer, rotina.descrever()),
        )
        if existente is not None:
            await self._store.touch_memories([int(existente["id"])])
            return None

        memory_id = await self._store.insert_memory(
            layer=self.layer,
            content=rotina.descrever(),
            source="consolidador",
            confidence=min(0.95, 0.5 + 0.1 * rotina.ocorrencias),
            metadata={
                "assinatura": rotina.assinatura,
                "gatilho": rotina.gatilho,
                "dia_da_semana": rotina.dia_da_semana,
                "hora": rotina.hora,
                "ocorrencias": rotina.ocorrencias,
                "exemplos": rotina.exemplos,
            },
        )
        log.info(
            "memoria.rotina_detectada",
            memory_id=memory_id,
            assinatura=rotina.assinatura,
            ocorrencias=rotina.ocorrencias,
        )
        return memory_id

    async def rotinas(self, limit: int = 100) -> list[dict[str, Any]]:
        return await self._store.list_memories(layer=self.layer, vigentes=True, limit=limit)

    async def para_agora(self, agora: datetime | None = None) -> list[dict[str, Any]]:
        """Rotinas cujo gatilho bate com o momento atual (insumo da F7)."""
        import json

        momento = agora or datetime.now()
        pertinentes = []
        for linha in await self.rotinas():
            meta = json.loads(linha.get("metadata") or "{}")
            if meta.get("dia_da_semana") != momento.weekday():
                continue
            hora = meta.get("hora")
            if hora is None or abs(int(hora) - momento.hour) <= 1:
                pertinentes.append(linha)
        return pertinentes

    async def count(self) -> int:
        contagem = await self._store.count_memories()
        return contagem.get(self.layer, 0)


def _quando(episodio: dict[str, Any]) -> datetime | None:
    try:
        quando = datetime.fromisoformat(str(episodio["created_at"]))
    except (KeyError, ValueError):
        return None
    return quando.astimezone(UTC) if quando.tzinfo else quando.replace(tzinfo=UTC)


def _assinatura(conteudo: str) -> str:
    """Reduz o pedido a suas palavras de intencao, em ordem estavel.

    "me manda o resumo da semana" e "manda o resumo da semana ai" viram a mesma
    assinatura; "abre o youtube" nao.
    """
    pergunta = re.split(r"\s*->\s*", conteudo, maxsplit=1)[0]
    tokens = [t for t in tokenizar(pergunta) if t not in VAZIAS and len(t) > 2]
    return " ".join(sorted(set(tokens))[:6])
