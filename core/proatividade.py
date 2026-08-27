"""F7 - o Optmus fala sem ser chamado.

Esta e a unica funcao do sistema que **interrompe** alguem. Todas as outras
respondem a um pedido; esta decide sozinha que vale a pena falar. Por isso ela
tem mais freios que qualquer outra parte do Core, e por isso vem desligada.

## As seis regras, e o motivo de cada uma

**1. Quem decide SE avisa e o codigo; quem decide COMO e o modelo.** O gatilho
sai de dado real - um prazo no Notion, uma rotina detectada - por uma regra
deterministica. So a frase e escrita pelo modelo, para soar como o Optmus. O
contrario seria um modelo decidindo quando te interromper: nao da para orcar,
nao da para prever e nao da para testar.

**2. O compositor nao tem ferramenta nenhuma.** Ele so escreve texto. Um aviso
proativo nao tem humano esperando para confirmar nada, entao o portao de
``EXTERNO`` nao pode ser aplicado - e um caminho sem portao **jamais** pode
alcancar terceiros. Sem ferramentas, o WhatsApp nao esta ao alcance nem por
acidente. E guarda estrutural, nao promessa.

**3. Um aviso por ciclo.** Tres coisas vencendo hoje viram um aviso, nao tres.
Rajada e o que faz alguem silenciar o assistente para sempre.

**4. Orcamento diario rigido.** Contado por dia civil e gravado no banco. Ao
chegar a zero os avisos param - nao entram em fila para amanha, porque fila de
aviso vira enxurrada de manha.

**5. Janela de silencio descarta, nao adia.** Pelo mesmo motivo. E descartar
nao perde nada: a fonte e relida a cada ciclo, entao o prazo que ainda importar
as 8h aparece sozinho as 8h. O que sumir no meio da noite era o que deixou de
importar.

**6. Nunca inventa.** Sem gatilho, sem aviso. O modelo recebe os fatos e a
instrucao de nao acrescentar nada - e o teste que prova isso passa uma lista
vazia e exige silencio.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from core.config import Settings
from core.logging import get_logger
from memory.store import Store
from security.audit import AuditLog
from security.policy import RiskLevel

log = get_logger("core.proatividade")

CHAVE_GASTO = "proatividade:gasto:{dia}"
CHAVE_VISTO = "proatividade:visto:{chave}"
FERRAMENTA = "aviso_proativo"

INSTRUCAO = (
    "Voce vai avisar {nome} de algo que ele NAO perguntou. Escreva UMA frase, "
    "no maximo duas, no seu tom de sempre. Use somente os fatos abaixo - nao "
    "acrescente, nao estime, nao suponha nada que nao esteja escrito. Se os "
    "fatos nao justificarem um aviso, responda exatamente: SEM AVISO.\n\n"
    "FATOS:\n{fatos}"
)
SEM_AVISO = "SEM AVISO"


@dataclass(frozen=True, slots=True)
class Gatilho:
    """Um motivo para falar, com o dado que o justifica."""

    chave: str
    """Impressao digital do assunto. Dois gatilhos com a mesma chave sao o
    mesmo aviso, mesmo que o texto mude - e o que impede "voce tem prova
    amanha" a cada trinta minutos."""

    assunto: str
    fatos: str
    urgencia: int = 0


@runtime_checkable
class FonteDeGatilhos(Protocol):
    async def coletar(self, agora: datetime) -> list[Gatilho]: ...


@runtime_checkable
class Compositor(Protocol):
    async def escrever(self, fatos: str) -> str: ...


@runtime_checkable
class CanalDeAviso(Protocol):
    async def avisar(self, texto: str) -> bool: ...


@dataclass(slots=True)
class ResultadoCiclo:
    """O que aconteceu num ciclo - inclusive quando nada aconteceu.

    ``motivo`` existe para o ``/health``: "nenhum aviso" e resposta certa na
    maioria dos ciclos, e sem o motivo nao da para distinguir "nada a dizer" de
    "orcamento estourado" ou "fonte quebrada".
    """

    avisou: bool = False
    motivo: str = ""
    assunto: str | None = None
    texto: str | None = None
    gatilhos: int = 0
    falhas: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "avisou": self.avisou,
            "motivo": self.motivo,
            "assunto": self.assunto,
            "gatilhos": self.gatilhos,
            "falhas": self.falhas,
        }


def impressao(*partes: str) -> str:
    """Chave curta e estavel para deduplicacao."""
    cru = "|".join(p.strip().lower() for p in partes)
    return hashlib.sha256(cru.encode("utf-8")).hexdigest()[:16]


class Proatividade:
    """Decide se fala, escreve o que falar, e conta quanto ja falou."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        *,
        compositor: Compositor,
        canal: CanalDeAviso,
        fontes: list[FonteDeGatilhos] | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._compositor = compositor
        self._canal = canal
        self._fontes = fontes or []
        self._audit = audit

    # ------------------------------------------------------------ silencio
    def em_silencio(self, agora: datetime) -> bool:
        """Dentro da janela em que nao se fala.

        Trata a virada da meia-noite: 22h-8h e um intervalo que cruza o dia, e
        compara-lo como ``inicio <= h < fim`` daria falso para toda hora.
        """
        inicio = self._settings.proactive_quiet_start
        fim = self._settings.proactive_quiet_end
        hora = agora.hour
        if inicio == fim:
            return False
        if inicio < fim:
            return inicio <= hora < fim
        return hora >= inicio or hora < fim

    # ----------------------------------------------------------- orcamento
    def _chave_do_dia(self, agora: datetime) -> str:
        return CHAVE_GASTO.format(dia=agora.date().isoformat())

    async def gasto_hoje(self, agora: datetime | None = None) -> int:
        bruto = await self._store.meta_get(self._chave_do_dia(agora or datetime.now(UTC)))
        return int(bruto or 0)

    async def restante(self, agora: datetime | None = None) -> int:
        momento = agora or datetime.now(UTC)
        return max(0, self._settings.proactive_daily_budget - await self.gasto_hoje(momento))

    async def _gastar(self, agora: datetime) -> None:
        # Chave por data: o "reset diario" nao precisa de tarefa que zera nada.
        # Um contador que dependesse de alguem lembrar de zerar erraria em todo
        # reinicio do processo.
        chave = self._chave_do_dia(agora)
        await self._store.meta_set(chave, str(int(await self.gasto_hoje(agora)) + 1))

    # --------------------------------------------------------- repeticoes
    async def _ja_avisado(self, gatilho: Gatilho, agora: datetime) -> bool:
        bruto = await self._store.meta_get(CHAVE_VISTO.format(chave=gatilho.chave))
        if not bruto:
            return False
        quando = datetime.fromisoformat(bruto)
        return (agora - quando) < timedelta(hours=self._settings.proactive_cooldown_h)

    async def _marcar(self, gatilho: Gatilho, agora: datetime) -> None:
        await self._store.meta_set(CHAVE_VISTO.format(chave=gatilho.chave), agora.isoformat())

    # -------------------------------------------------------------- ciclo
    async def _coletar(self, agora: datetime) -> tuple[list[Gatilho], list[str]]:
        """Junta os gatilhos das fontes. Fonte quebrada nao derruba as outras.

        O Notion fora do ar nao pode calar uma rotina detectada localmente -
        senao a proatividade inteira depende do servico mais fragil.
        """
        gatilhos: list[Gatilho] = []
        falhas: list[str] = []
        for fonte in self._fontes:
            try:
                gatilhos.extend(await fonte.coletar(agora))
            except Exception as exc:  # noqa: BLE001 - fonte ruim nao mata o ciclo
                nome = type(fonte).__name__
                log.warning("proatividade.fonte_falhou", fonte=nome, erro=str(exc))
                falhas.append(f"{nome}: {exc}")
        return gatilhos, falhas

    async def ciclo(self, agora: datetime | None = None) -> ResultadoCiclo:
        momento = agora or datetime.now(UTC)

        if not self._settings.proactive_enabled:
            return ResultadoCiclo(motivo="proatividade desligada")
        if self.em_silencio(momento):
            return ResultadoCiclo(motivo="janela de silencio")
        if await self.restante(momento) <= 0:
            return ResultadoCiclo(motivo="orcamento do dia esgotado")

        gatilhos, falhas = await self._coletar(momento)
        if not gatilhos:
            return ResultadoCiclo(motivo="nada a dizer", falhas=falhas)

        # Do mais urgente para o menos: se so um vai sair, que seja o que mais
        # importa. Empate resolve pela chave, para o ciclo ser reproduzivel.
        candidatos = sorted(gatilhos, key=lambda g: (-g.urgencia, g.chave))
        escolhido: Gatilho | None = None
        for candidato in candidatos:
            if not await self._ja_avisado(candidato, momento):
                escolhido = candidato
                break

        if escolhido is None:
            return ResultadoCiclo(
                motivo="tudo ja avisado recentemente", gatilhos=len(gatilhos), falhas=falhas
            )

        texto = (await self._compositor.escrever(escolhido.fatos)).strip()
        if not texto or SEM_AVISO in texto.upper():
            # O modelo leu os fatos e concluiu que nao dava aviso. Marca assim
            # mesmo: reperguntar a cada trinta minutos gastaria o modelo para
            # ouvir o mesmo "nao" - e nao gasta orcamento, porque nada saiu.
            await self._marcar(escolhido, momento)
            return ResultadoCiclo(
                motivo="o modelo julgou que nao valia aviso",
                assunto=escolhido.assunto,
                gatilhos=len(gatilhos),
                falhas=falhas,
            )

        entregue = await self._canal.avisar(texto)
        if not entregue:
            # Nao gasta orcamento nem marca como visto: o aviso nao chegou, e
            # cobrar por uma entrega que falhou faria o teto silenciar o dia
            # inteiro por causa de uma rede instavel.
            return ResultadoCiclo(
                motivo="nenhum canal entregou o aviso",
                assunto=escolhido.assunto,
                gatilhos=len(gatilhos),
                falhas=falhas,
            )

        await self._gastar(momento)
        await self._marcar(escolhido, momento)
        if self._audit is not None:
            await self._audit.registrar(
                ferramenta=FERRAMENTA,
                risco=RiskLevel.ESCRITA,
                # "permitido" e o termo do esquema para "a politica deixou
                # passar". Nao ha confirmacao humana aqui de proposito, e o
                # orcamento diario e o que substitui o portao.
                decisao="permitido",
                parametros={"assunto": escolhido.assunto, "chave": escolhido.chave},
                resultado=texto,
                comando_origem="proatividade",
            )

        log.info("proatividade.avisou", assunto=escolhido.assunto, caracteres=len(texto))
        return ResultadoCiclo(
            avisou=True,
            motivo="ok",
            assunto=escolhido.assunto,
            texto=texto,
            gatilhos=len(gatilhos),
            falhas=falhas,
        )

    async def stats(self) -> dict[str, Any]:
        agora = datetime.now(UTC)
        return {
            "ligada": self._settings.proactive_enabled,
            "em_silencio": self.em_silencio(agora),
            "orcamento": self._settings.proactive_daily_budget,
            "gasto_hoje": await self.gasto_hoje(agora),
            "fontes": [type(f).__name__ for f in self._fontes],
        }


class AgendadorProativo:
    """Roda o ciclo a cada N minutos. Espelha o ``ConsolidatorScheduler``."""

    def __init__(self, proatividade: Proatividade, *, intervalo_min: int) -> None:
        self._proatividade = proatividade
        self._intervalo_s = intervalo_min * 60
        self.ciclos = 0
        self.ultima: ResultadoCiclo | None = None

    async def run_forever(self) -> None:
        log.info("proatividade.agendada", intervalo_min=self._intervalo_s // 60)
        while True:
            await asyncio.sleep(self._intervalo_s)
            try:
                self.ultima = await self._proatividade.ciclo()
                self.ciclos += 1
            except Exception as exc:  # o laco nao pode morrer
                # Um ciclo que explode nao pode matar o agendador: o proximo
                # tem chance de dar certo, e um agendador morto e silencioso.
                log.error("proatividade.ciclo_falhou", erro=str(exc), exc_info=True)

    def stats(self) -> dict[str, Any]:
        return {
            "ciclos": self.ciclos,
            "intervalo_min": self._intervalo_s // 60,
            "ultima": self.ultima.to_dict() if self.ultima else None,
        }
