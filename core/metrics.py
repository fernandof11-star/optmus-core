"""Instrumentacao de latencia por etapa.

"Nao otimize antes de medir" (secao 12.6) so funciona se a medicao existir
desde a F1. Cada turno de voz e cronometrado etapa a etapa e o numero fica
disponivel em /metrics e no HUD (F4).

A metrica que importa e ``wake -> primeira_silaba``: o instante em que o
usuario ouve o som, nao o instante em que a resposta termina de ser gerada.
Por isso existe :meth:`TurnMetrics.mark`, separado de :meth:`stage`.
"""

from __future__ import annotations

import statistics
from collections import defaultdict, deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from core.logging import get_logger

log = get_logger("core.metrics")


def _ms(inicio: float, fim: float) -> float:
    return round((fim - inicio) * 1000, 2)


@dataclass(slots=True)
class TurnMetrics:
    """Cronometro de um turno (uma fala do usuario e a resposta)."""

    turn_id: str
    inicio: float = field(default_factory=perf_counter)
    etapas: dict[str, float] = field(default_factory=dict)
    marcos: dict[str, float] = field(default_factory=dict)

    @contextmanager
    def stage(self, nome: str) -> Iterator[None]:
        """Cronometra uma etapa (stt, router, llm, tts...)."""
        t0 = perf_counter()
        try:
            yield
        finally:
            self.etapas[nome] = _ms(t0, perf_counter())

    def mark(self, nome: str) -> float:
        """Marca um instante relativo ao inicio do turno. Idempotente."""
        if nome not in self.marcos:
            self.marcos[nome] = _ms(self.inicio, perf_counter())
        return self.marcos[nome]

    @property
    def total_ms(self) -> float:
        return _ms(self.inicio, perf_counter())

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "total_ms": self.total_ms,
            "etapas": dict(self.etapas),
            "marcos": dict(self.marcos),
        }


class LatencyTracker:
    """Janela deslizante de latencias, com percentis por etapa e por marco."""

    def __init__(self, *, window: int = 200, target_ms: int = 1200) -> None:
        self._window = window
        self._target_ms = target_ms
        self._series: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=window))
        self._turnos = 0
        self._acima_da_meta = 0

    def record(self, turn: TurnMetrics, *, meta_marco: str = "primeira_silaba") -> None:
        for nome, valor in turn.etapas.items():
            self._series[f"etapa.{nome}"].append(valor)
        for nome, valor in turn.marcos.items():
            self._series[f"marco.{nome}"].append(valor)
        self._series["turno.total"].append(turn.total_ms)
        self._turnos += 1

        alvo = turn.marcos.get(meta_marco)
        if alvo is not None and alvo > self._target_ms:
            self._acima_da_meta += 1
            log.warning(
                "latencia.acima_da_meta",
                turn_id=turn.turn_id,
                marco=meta_marco,
                medido_ms=alvo,
                meta_ms=self._target_ms,
                etapas=turn.etapas,
            )

    def summary(self) -> dict[str, Any]:
        return {
            "turnos": self._turnos,
            "meta_ms": self._target_ms,
            "acima_da_meta": self._acima_da_meta,
            "series": {nome: _resumo(list(v)) for nome, v in sorted(self._series.items()) if v},
        }

    def reset(self) -> None:
        self._series.clear()
        self._turnos = 0
        self._acima_da_meta = 0


def _resumo(valores: list[float]) -> dict[str, float | int]:
    ordenados = sorted(valores)
    return {
        "n": len(ordenados),
        "p50": round(statistics.median(ordenados), 2),
        "p95": round(ordenados[min(len(ordenados) - 1, int(len(ordenados) * 0.95))], 2),
        "max": round(ordenados[-1], 2),
    }
