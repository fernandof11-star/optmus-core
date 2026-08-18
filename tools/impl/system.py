"""``sistema_status`` - o Optmus olhando para o proprio estado.

Quando o usuario pergunta "voce esta bem?" ou "o que esta fora do ar?", a
resposta tem que vir de dado, nao de otimismo. Esta ferramenta devolve o que o
/health devolve, em texto curto o bastante para virar fala.
"""

from __future__ import annotations

import platform
from typing import Any, ClassVar

from core.config import Settings
from core.metrics import LatencyTracker
from memory.system import MemorySystem
from security.policy import RiskLevel
from tools.registry import Tool, ToolResult


class SistemaStatusTool(Tool):
    name = "sistema_status"
    description = (
        "Estado interno do proprio Optmus: latencia recente, memoria, quais "
        "subsistemas estao degradados. Use quando o usuario perguntar como voce "
        "esta, se algo esta fora do ar, ou por que algo nao funcionou."
    )
    risk = RiskLevel.LEITURA
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(
        self,
        settings: Settings,
        *,
        memory: MemorySystem,
        tracker: LatencyTracker,
        degradacoes: Any = None,
    ) -> None:
        self._settings = settings
        self._memory = memory
        self._tracker = tracker
        self._degradacoes = degradacoes or (lambda: [])

    async def execute(self, **kwargs: Any) -> ToolResult:
        metricas = self._tracker.summary()
        memoria = await self._memory.stats()
        primeira_silaba = metricas["series"].get("marco.primeira_silaba", {})
        degradacoes = list(self._degradacoes())

        partes = [
            f"Python {platform.python_version()} em {platform.system()}.",
            f"Turnos nesta sessao: {metricas['turnos']}.",
        ]
        if primeira_silaba:
            partes.append(
                f"Latencia ate a primeira silaba: mediana {primeira_silaba['p50']:.0f}ms, "
                f"meta {metricas['meta_ms']}ms, {metricas['acima_da_meta']} acima da meta."
            )
        partes.append(
            f"Memoria: {memoria['episodica']} episodios, {memoria['semantica']} fatos, "
            f"{memoria['procedural']} rotinas, busca por {memoria['embedder']}."
        )
        partes.append(
            "Tudo nominal." if not degradacoes else "Degradado: " + "; ".join(degradacoes)
        )
        return ToolResult(
            content=" ".join(partes), dados={"metricas": metricas, "memoria": memoria}
        )
