"""Consolidador noturno - o "sono" do sistema.

Toda madrugada: le os episodios do dia, extrai deles os fatos que valem a pena
guardar, detecta padroes que viraram rotina e marca o que ja foi digerido.

Na industria isso se chama *memory compaction*. Sem ele, o banco vira lixo em
tres meses: milhares de episodios crus, nenhum fato consolidado, e uma busca
que devolve dez conversas parecidas em vez de a informacao.

Duas fronteiras deliberadas:

- **Nao escreve no ``perfil.md``.** O perfil entra em todo prompt e so muda por
  ferramenta explicita. O consolidador propoe fatos na camada semantica, onde
  errar custa uma busca ruim, nao todas as conversas seguintes.
- **Nao apaga episodio.** Consolidar marca ``consolidated_at``; o episodio
  continua la, decaindo pela relevancia. A retencao e outra decisao, e e sua.

Sem cerebro configurado, a extracao de fatos e pulada e o resto roda igual -
deteccao de rotina e heuristica pura e nao depende de LLM.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from core.bus import EventBus
from core.config import Settings
from core.llm import LLMClient, LLMError
from core.logging import get_logger
from memory.episodic import EpisodicMemory
from memory.procedural import ProceduralMemory
from memory.semantic import SemanticMemory

log = get_logger("memory.consolidator")

PROMPT_EXTRACAO = """Voce esta consolidando a memoria de um assistente pessoal.

Abaixo estao episodios das ultimas horas. Extraia APENAS fatos duraveis sobre o
usuario ou o mundo dele - coisas que continuarao verdadeiras daqui a semanas.

Guarde: preferencias, pessoas, lugares, projetos, restricoes, habitos declarados.
Descarte: pedidos pontuais, resultados de consulta, horarios, qualquer coisa
que ja nao seja verdade amanha.

Responda SO com um array JSON, sem texto em volta:
[{{"fato": "...", "confianca": 0.0-1.0}}]
Array vazio se nao houver nada durador.

EPISODIOS:
{episodios}"""


@dataclass(slots=True)
class ResultadoConsolidacao:
    episodios: int = 0
    fatos_novos: int = 0
    fatos_repetidos: int = 0
    rotinas_novas: int = 0
    duracao_ms: float = 0.0
    erro: str | None = None
    detalhes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "episodios": self.episodios,
            "fatos_novos": self.fatos_novos,
            "fatos_repetidos": self.fatos_repetidos,
            "rotinas_novas": self.rotinas_novas,
            "duracao_ms": round(self.duracao_ms, 1),
            "erro": self.erro,
        }


class Consolidator:
    """Executa uma passada de consolidacao."""

    def __init__(
        self,
        settings: Settings,
        *,
        episodic: EpisodicMemory,
        semantic: SemanticMemory,
        procedural: ProceduralMemory,
        llm: LLMClient | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._settings = settings
        self._episodic = episodic
        self._semantic = semantic
        self._procedural = procedural
        self._llm = llm
        self._bus = bus

    async def run(self, *, janela_dias: int = 1, limite: int = 300) -> ResultadoConsolidacao:
        inicio = datetime.now(UTC)
        resultado = ResultadoConsolidacao()

        episodios = await self._episodic.do_dia(dias=janela_dias, limit=limite)
        resultado.episodios = len(episodios)
        if not episodios:
            log.info("consolidador.nada_a_fazer")
            return self._fechar(resultado, inicio)

        try:
            await self._extrair_fatos(episodios, resultado)
            await self._detectar_rotinas(resultado)
            await self._episodic.store.mark_consolidated([int(e["id"]) for e in episodios])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            resultado.erro = f"{type(exc).__name__}: {exc}"
            log.error("consolidador.falhou", erro=resultado.erro, exc_info=True)

        return self._fechar(resultado, inicio)

    def _fechar(
        self, resultado: ResultadoConsolidacao, inicio: datetime
    ) -> ResultadoConsolidacao:
        resultado.duracao_ms = (datetime.now(UTC) - inicio).total_seconds() * 1000
        log.info("consolidador.concluido", **resultado.to_dict())
        return resultado

    async def _extrair_fatos(
        self, episodios: list[dict[str, Any]], resultado: ResultadoConsolidacao
    ) -> None:
        if self._llm is None or not await self._llm.available():
            log.info(
                "consolidador.sem_cerebro",
                impacto="rotinas ainda sao detectadas; extracao de fatos fica de fora",
            )
            return

        texto = "\n".join(f"- {e['created_at']}: {e['content']}" for e in episodios[:100])
        try:
            turno = await self._llm.stream_turn(
                system="Voce extrai fatos duraveis. Responde so JSON.",
                messages=[{"role": "user", "content": PROMPT_EXTRACAO.format(episodios=texto)}],
            )
        except LLMError as exc:
            resultado.detalhes.append(f"extracao falhou: {exc}")
            log.warning("consolidador.extracao_falhou", erro=str(exc))
            return

        for fato in _parse_fatos(turno.text):
            conteudo = fato["fato"]
            duplicado = await self._semantic.ja_sabe(conteudo)
            if duplicado is not None:
                resultado.fatos_repetidos += 1
                continue
            await self._semantic.remember(
                conteudo,
                source="consolidador",
                confidence=float(fato.get("confianca", 0.6)),
                metadata={"origem": "consolidacao_noturna"},
            )
            resultado.fatos_novos += 1

    async def _detectar_rotinas(self, resultado: ResultadoConsolidacao) -> None:
        # Rotina precisa de historico: olha 60 dias, nao so o dia consolidado.
        desde = (datetime.now(UTC) - timedelta(days=60)).isoformat(timespec="milliseconds")
        historico = await self._episodic.store.list_memories(
            layer="episodica", desde=desde, limit=2000
        )
        for rotina in self._procedural.detectar(historico):
            if await self._procedural.registrar(rotina) is not None:
                resultado.rotinas_novas += 1


def _parse_fatos(texto: str) -> list[dict[str, Any]]:
    """Extrai o array JSON da resposta, tolerando texto em volta."""
    casou = re.search(r"\[.*\]", texto, re.DOTALL)
    if casou is None:
        return []
    try:
        dados = json.loads(casou.group(0))
    except json.JSONDecodeError:
        log.warning("consolidador.json_invalido", trecho=texto[:200])
        return []
    return [
        item
        for item in dados
        if isinstance(item, dict) and isinstance(item.get("fato"), str) and item["fato"].strip()
    ]


class ConsolidatorScheduler:
    """Dispara o consolidador uma vez por dia, na hora configurada."""

    def __init__(
        self, consolidator: Consolidator, *, hora: int, bus: EventBus | None = None
    ) -> None:
        self._consolidator = consolidator
        self._hora = hora
        self._bus = bus
        self.ultima: ResultadoConsolidacao | None = None
        self.execucoes = 0

    def segundos_ate_proxima(self, agora: datetime | None = None) -> float:
        momento = agora or datetime.now()
        alvo = momento.replace(hour=self._hora, minute=0, second=0, microsecond=0)
        if alvo <= momento:
            alvo += timedelta(days=1)
        return (alvo - momento).total_seconds()

    async def run_forever(self) -> None:
        log.info("consolidador.agendado", hora=self._hora)
        while True:
            await asyncio.sleep(self.segundos_ate_proxima())
            await self.executar()

    async def executar(self) -> ResultadoConsolidacao:
        resultado = await self._consolidator.run()
        self.ultima = resultado
        self.execucoes += 1
        if self._bus is not None:
            await self._bus.emit(
                "memoria.consolidada", source="consolidador", payload=resultado.to_dict()
            )
        return resultado

    def stats(self) -> dict[str, Any]:
        return {
            "hora": self._hora,
            "execucoes": self.execucoes,
            "proxima_em_s": round(self.segundos_ate_proxima()),
            "ultima": self.ultima.to_dict() if self.ultima else None,
        }
