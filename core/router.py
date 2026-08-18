"""Roteador de intencao de duas camadas.

Camada 1 - deterministica: regex sobre a fala normalizada. Responde em menos
de 1ms, custo zero, sem rede. Cobre o que e fixo: horas, data, parar, cancelar,
silenciar, saudacao, status.

Camada 2 - LLM: tudo que nao casou.

Por que isso existe: 40-60% dos comandos do dia a dia sao triviais. Manda-los
para o modelo grande e pagar latencia e token por "que horas sao". Alem do
custo, a camada 1 e o que faz o kill switch funcionar mesmo com a rede fora -
"para" NUNCA pode depender de uma chamada HTTP.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from core.config import Settings
from core.logging import get_logger

log = get_logger("core.router")

DIAS: Final[tuple[str, ...]] = (
    "segunda-feira", "terca-feira", "quarta-feira", "quinta-feira",
    "sexta-feira", "sabado", "domingo",
)
MESES: Final[tuple[str, ...]] = (
    "janeiro", "fevereiro", "marco", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)


class Camada(StrEnum):
    DETERMINISTICA = "deterministica"
    LLM = "llm"


class Acao(StrEnum):
    """Efeito colateral que o roteador pede ao loop de voz."""

    RESPONDER = "responder"
    PARAR = "parar"
    SILENCIAR = "silenciar"


@dataclass(frozen=True, slots=True)
class RouteResult:
    camada: Camada
    texto: str
    resposta: str | None = None
    acao: Acao = Acao.RESPONDER
    regra: str | None = None

    @property
    def resolvido(self) -> bool:
        """True quando a camada 1 ja tem a resposta - o LLM nem e chamado."""
        return self.camada is Camada.DETERMINISTICA


Handler = Callable[[re.Match[str], Settings], str]


@dataclass(frozen=True, slots=True)
class Regra:
    nome: str
    padrao: re.Pattern[str]
    handler: Handler
    acao: Acao = Acao.RESPONDER


def normalizar(texto: str) -> str:
    """Minusculas, sem acento, sem pontuacao, espacos colapsados.

    O STT devolve "Que horas sao?" e tambem "que horas são" - a camada 1 nao
    pode depender de qual saiu.
    """
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFD", texto.lower())
        if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", sem_acento)).strip()


# ------------------------------------------------------------------ handlers
def _hora(_: re.Match[str], s: Settings) -> str:
    agora = datetime.now()
    return f"{agora.hour} e {agora.minute:02d}" if agora.minute else f"{agora.hour} em ponto"


def _data(_: re.Match[str], s: Settings) -> str:
    hoje = datetime.now()
    return f"{DIAS[hoje.weekday()]}, {hoje.day} de {MESES[hoje.month - 1]}"


def _parar(_: re.Match[str], s: Settings) -> str:
    return "Parado."


def _silenciar(_: re.Match[str], s: Settings) -> str:
    return ""


def _saudacao(_: re.Match[str], s: Settings) -> str:
    return f"Pois nao, {s.user_honorific}."


def _identidade(_: re.Match[str], s: Settings) -> str:
    return "Optmus. Sistema local, sempre ligado."


def _agradecimento(_: re.Match[str], s: Settings) -> str:
    return "As ordens."


REGRAS: Final[tuple[Regra, ...]] = (
    # PARAR vem primeiro de proposito: o kill switch nunca disputa com outra regra.
    Regra("parar", re.compile(r"\b(parar? tudo|para tudo|pare|parar|cancela\w*|abortar)\b"),
          _parar, Acao.PARAR),
    Regra("silenciar", re.compile(r"\b(silencio|cala|quieto|fica quieto|modo silencioso)\b"),
          _silenciar, Acao.SILENCIAR),
    Regra("hora", re.compile(r"\b(que horas sao|que hora e|me da as horas|horas agora)\b"), _hora),
    Regra("data", re.compile(r"\b(que dia e hoje|qual (a )?data|que dia e|data de hoje)\b"), _data),
    Regra("saudacao", re.compile(r"^(oi|ola|bom dia|boa tarde|boa noite|e ai|opa)$"), _saudacao),
    Regra("identidade", re.compile(r"\b(quem e voce|qual seu nome|voce e o que)\b"), _identidade),
    Regra("agradecimento", re.compile(r"^(obrigado|obrigada|valeu|beleza|ok|certo)$"),
          _agradecimento),
)


class IntentRouter:
    """Decide se a fala morre na camada 1 ou sobe para o LLM."""

    def __init__(self, settings: Settings, regras: tuple[Regra, ...] = REGRAS) -> None:
        self._settings = settings
        self._regras = regras
        self.acertos_camada1 = 0
        self.total = 0

    def route(self, texto: str) -> RouteResult:
        self.total += 1
        normalizado = normalizar(texto)
        if not normalizado:
            return RouteResult(Camada.DETERMINISTICA, texto, resposta="", regra="vazio")

        for regra in self._regras:
            casou = regra.padrao.search(normalizado)
            if casou is None:
                continue
            self.acertos_camada1 += 1
            resposta = regra.handler(casou, self._settings)
            log.debug("router.camada1", regra=regra.nome, texto=normalizado)
            return RouteResult(
                camada=Camada.DETERMINISTICA,
                texto=texto,
                resposta=resposta,
                acao=regra.acao,
                regra=regra.nome,
            )

        log.debug("router.camada2", texto=normalizado)
        return RouteResult(camada=Camada.LLM, texto=texto)

    @property
    def taxa_camada1(self) -> float:
        return round(self.acertos_camada1 / self.total, 4) if self.total else 0.0

    def stats(self) -> dict[str, float | int]:
        return {
            "total": self.total,
            "camada1": self.acertos_camada1,
            "camada2": self.total - self.acertos_camada1,
            "taxa_camada1": self.taxa_camada1,
        }
