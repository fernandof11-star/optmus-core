"""Conferencia entre o Optmus Web e o calculo local sobre o Notion.

Esta e a peca que decide se o Web pode ser desligado. Ela nao "aproxima" nada:
compara campo a campo e mostra os dois valores lado a lado quando divergem.

Duas escolhas deliberadas:

- **Divergencia nao e erro do relatorio.** O relatorio nao tenta explicar quem
  esta certo; ele mostra os dois numeros. Quem decide e voce, olhando a linha no
  Notion. Um relatorio que "resolve" a diferenca sozinho e um relatorio que
  esconde a unica informacao que importa.
- **Uma unica diferenca ja reprova.** ``equivalente`` so e verdadeiro com zero
  divergencias. Nao existe "quase igual" quando a decisao seguinte e apagar a
  outra fonte.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.logging import get_logger

log = get_logger("integrations.reconciliacao")

# Tolerancia so para ruido de ponto flutuante, NAO para diferenca real.
# Os dois lados arredondam para centavos, entao qualquer divergencia legitima e
# de pelo menos 0.01 - e precisa aparecer. Uma tolerancia de um centavo
# engoliria exatamente o tipo de erro que esta conferencia existe para achar
# (e diferenca de centavo costuma denunciar ordem de arredondamento diferente,
# que e informacao util, nao ruido).
TOLERANCIA = 1e-6


@dataclass(slots=True)
class Divergencia:
    indicador: str
    chave: str
    campo: str
    web: Any
    notion: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "indicador": self.indicador,
            "chave": self.chave,
            "campo": self.campo,
            "web": self.web,
            "notion": self.notion,
        }


@dataclass(slots=True)
class ResultadoIndicador:
    indicador: str
    comparados: int = 0
    divergencias: list[Divergencia] = field(default_factory=list)
    so_no_web: list[str] = field(default_factory=list)
    so_no_notion: list[str] = field(default_factory=list)
    erro: str | None = None

    @property
    def sem_dados(self) -> bool:
        """Nada foi comparado: os dois lados vieram vazios.

        Nao e falha do sistema - pode nao haver prazo aberto, por exemplo. Mas
        tambem nao e prova de nada, e por isso nao conta como equivalente.
        """
        return (
            self.erro is None
            and self.comparados == 0
            and not self.so_no_web
            and not self.so_no_notion
        )

    @property
    def equivalente(self) -> bool:
        """Exige pelo menos um registro comparado.

        Duas listas vazias nao divergem - e nao provam nada. Contar isso como
        equivalencia transformaria ausencia de evidencia em evidencia, no exato
        relatorio que vai embasar apagar a outra fonte.
        """
        return (
            self.erro is None
            and self.comparados > 0
            and not self.divergencias
            and not self.so_no_web
            and not self.so_no_notion
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "indicador": self.indicador,
            "equivalente": self.equivalente,
            "sem_dados": self.sem_dados,
            "comparados": self.comparados,
            "divergencias": [d.to_dict() for d in self.divergencias],
            "so_no_web": self.so_no_web,
            "so_no_notion": self.so_no_notion,
            "erro": self.erro,
        }


@dataclass(slots=True)
class Relatorio:
    indicadores: list[ResultadoIndicador] = field(default_factory=list)
    avisos: list[dict[str, Any]] = field(default_factory=list)

    @property
    def equivalente(self) -> bool:
        return bool(self.indicadores) and all(i.equivalente for i in self.indicadores)

    def to_dict(self) -> dict[str, Any]:
        return {
            "equivalente": self.equivalente,
            "veredito": self.veredito(),
            "indicadores": [i.to_dict() for i in self.indicadores],
            "avisos": self.avisos,
        }

    def veredito(self) -> str:
        """Frase que resume o relatorio.

        Precisa distinguir "comparei e bateu", "comparei e divergiu" e "nem
        consegui comparar" - as tres exigem acoes diferentes, e um veredito que
        diz "0 divergencias" quando nada foi comparado engana.
        """
        if self.equivalente:
            return "os numeros batem em tudo que foi comparado"

        divergencias = sum(len(i.divergencias) for i in self.indicadores)
        com_erro = [i.indicador for i in self.indicadores if i.erro]
        vazios = [i.indicador for i in self.indicadores if i.sem_dados]
        so_de_um_lado = sum(
            len(i.so_no_web) + len(i.so_no_notion) for i in self.indicadores
        )

        partes: list[str] = []
        if divergencias:
            partes.append(f"{divergencias} divergencia(s) de valor")
        if so_de_um_lado:
            partes.append(f"{so_de_um_lado} registro(s) presente(s) em so um lado")
        if com_erro:
            partes.append(f"nao consegui comparar: {', '.join(com_erro)}")
        if vazios:
            partes.append(f"sem dado nenhum para comparar: {', '.join(vazios)}")
        if not partes:
            partes.append("nenhum indicador foi comparado")
        return f"{'; '.join(partes)} - o Optmus Web ainda nao pode ser desligado"


def _iguais(a: Any, b: Any) -> bool:
    """Compara dois valores, descendo em dicionarios e listas.

    A descida importa: campos como ``melhorNoite`` e os ``trimestres`` de uma
    disciplina sao objetos aninhados. Comparados como texto, a ordem das chaves
    - que o JSON do Web nao garante igual a minha - viraria divergencia falsa,
    e a tolerancia de ponto flutuante se perderia no meio da string.
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return abs(float(a) - float(b)) <= TOLERANCIA
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_iguais(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_iguais(x, y) for x, y in zip(a, b, strict=True))
    return str(a or "").strip() == str(b or "").strip()


def comparar_listas(
    indicador: str,
    web: list[dict[str, Any]],
    notion: list[dict[str, Any]],
    *,
    chave: str,
    campos: tuple[str, ...],
) -> ResultadoIndicador:
    """Compara duas listas de registros indexadas por uma chave."""
    resultado = ResultadoIndicador(indicador=indicador)
    por_web = {str(item.get(chave)): item for item in web}
    por_notion = {str(item.get(chave)): item for item in notion}

    resultado.so_no_web = sorted(set(por_web) - set(por_notion))
    resultado.so_no_notion = sorted(set(por_notion) - set(por_web))

    for k in sorted(set(por_web) & set(por_notion)):
        resultado.comparados += 1
        for campo in campos:
            valor_web = por_web[k].get(campo)
            valor_notion = por_notion[k].get(campo)
            if not _iguais(valor_web, valor_notion):
                resultado.divergencias.append(
                    Divergencia(indicador, k, campo, valor_web, valor_notion)
                )
    return resultado


def comparar_objetos(
    indicador: str,
    web: dict[str, Any],
    notion: dict[str, Any],
    *,
    escalares: tuple[str, ...],
    listas: tuple[tuple[str, str, tuple[str, ...]], ...] = (),
) -> ResultadoIndicador:
    """Compara um indicador que devolve objeto, nao lista.

    ``escalares`` sao campos de valor unico; ``listas`` sao sub-listas dentro do
    objeto, cada uma com (campo, chave, campos_a_comparar).
    """
    resultado = ResultadoIndicador(indicador=indicador)
    for campo in escalares:
        resultado.comparados += 1
        if not _iguais(web.get(campo), notion.get(campo)):
            resultado.divergencias.append(
                Divergencia(indicador, "(raiz)", campo, web.get(campo), notion.get(campo))
            )

    for campo, chave, subcampos in listas:
        sub = comparar_listas(
            f"{indicador}.{campo}",
            list(web.get(campo) or []),
            list(notion.get(campo) or []),
            chave=chave,
            campos=subcampos,
        )
        resultado.comparados += sub.comparados
        resultado.divergencias.extend(sub.divergencias)
        resultado.so_no_web.extend(f"{campo}:{k}" for k in sub.so_no_web)
        resultado.so_no_notion.extend(f"{campo}:{k}" for k in sub.so_no_notion)
    return resultado


async def conferir(
    *,
    web_client: Any,
    stats: Any,
) -> Relatorio:
    """Roda os tres indicadores dos dois lados e compara.

    Falha de um indicador nao interrompe os outros: saber que dois batem e um
    nao e mais util do que abortar no primeiro problema.
    """
    relatorio = Relatorio()

    relatorio.indicadores.append(
        await _conferir_um(
            "financeiro_mensal",
            lambda: web_client.indicador("financeiro_mensal"),
            stats.monthly,
            chave="month",
            campos=("income", "expense", "balance", "label"),
        )
    )
    relatorio.indicadores.append(
        await _conferir_um(
            "trabalho",
            lambda: web_client.indicador("trabalho"),
            stats.work_tasks,
            chave="tipo",
            campos=("total", "ativo", "concluido"),
        )
    )
    relatorio.indicadores.append(
        await _conferir_um(
            "alertas",
            lambda: web_client.indicador("alertas"),
            stats.progress_alerts,
            chave="titulo",
            campos=("tipo", "data", "diasRestantes", "detalhe"),
        )
    )
    relatorio.indicadores.append(
        await _conferir_um(
            "gastos_por_categoria",
            lambda: web_client.indicador("gastos_por_categoria"),
            stats.category_spending,
            chave="categoria",
            campos=("total", "count"),
        )
    )
    relatorio.indicadores.append(
        await _conferir_um(
            "financeiro_semanal",
            lambda: web_client.indicador("financeiro_semanal"),
            stats.finance_weekly,
            chave="month",
            campos=("income", "expense", "balance", "label"),
        )
    )
    relatorio.indicadores.append(
        await _conferir_um(
            "treino_frequencia",
            lambda: web_client.indicador("treino_frequencia"),
            stats.workout_frequency,
            chave="label",
            campos=("quantidade",),
        )
    )
    relatorio.indicadores.append(
        await _conferir_um(
            "treino_mensal",
            lambda: web_client.indicador("treino_mensal"),
            stats.workout_monthly,
            chave="label",
            campos=("quantidade",),
        )
    )
    relatorio.indicadores.append(
        await _conferir_objeto(
            "taxa_de_poupanca",
            lambda: web_client.indicador("taxa_de_poupanca"),
            stats.savings_rate,
            escalares=("currentRate", "incomeSoFar", "expenseSoFar"),
            listas=(("trend", "month", ("rate", "label")),),
        )
    )
    relatorio.indicadores.append(
        await _conferir_objeto(
            "previsao_financeira",
            lambda: web_client.indicador("previsao_financeira"),
            stats.forecast,
            escalares=(
                "monthLabel", "daysElapsed", "daysInMonth", "incomeSoFar",
                "expenseSoFar", "netSoFar", "projectedNet", "historicalAverageNet",
            ),
        )
    )
    relatorio.indicadores.append(
        await _conferir_objeto(
            "estudos",
            lambda: web_client.indicador("estudos"),
            stats.study,
            escalares=(),
            listas=(("porStatus", "status", ("quantidade",)),),
        )
    )
    relatorio.indicadores.append(
        await _conferir_objeto(
            "notas_escolares",
            lambda: web_client.indicador("notas_escolares"),
            stats.grades,
            escalares=("totalAno", "minimoAprovacao"),
            listas=(
                ("trimestres", "numero", ("nome", "total", "minimo")),
                # "total" nao existe numa disciplina: o campo antigo comparava
                # None com None e passava sempre. Estes sao os campos reais.
                (
                    "disciplinas",
                    "disciplina",
                    ("obtido", "restante", "precisa", "situacao", "maximoPossivel",
                     "trimestres"),
                ),
            ),
        )
    )
    relatorio.indicadores.append(
        await _conferir_objeto(
            "tarefas",
            lambda: web_client.indicador("tarefas"),
            stats.tasks,
            escalares=("totalPendentes", "totalConcluidas", "totalAtrasadas"),
            listas=(
                ("porPrioridade", "prioridade", ("quantidade",)),
                ("atraso", "label", ("quantidade",)),
            ),
        )
    )
    relatorio.indicadores.append(
        await _conferir_objeto(
            "sono",
            lambda: web_client.indicador("sono"),
            stats.sleep,
            escalares=(
                "mediaHoras", "noitesRegistradas", "totalLinhas", "noitesNaMeta",
                "metaHoras", "melhorNoite", "piorNoite",
            ),
            listas=(
                ("ultimasNoites", "data", ("label", "horas", "qualidade")),
                ("porQualidade", "qualidade", ("quantidade",)),
            ),
        )
    )

    relatorio.avisos = [a.to_dict() for a in stats.avisos]
    log.info(
        "reconciliacao.concluida",
        equivalente=relatorio.equivalente,
        divergencias=sum(len(i.divergencias) for i in relatorio.indicadores),
    )
    return relatorio


async def _conferir_objeto(
    indicador: str,
    buscar_web: Any,
    calcular_notion: Any,
    *,
    escalares: tuple[str, ...],
    listas: tuple[tuple[str, str, tuple[str, ...]], ...] = (),
) -> ResultadoIndicador:
    try:
        do_web = await buscar_web()
        do_notion = await calcular_notion()
    except Exception as exc:  # noqa: BLE001 - um lado fora nao aborta os outros
        log.warning("reconciliacao.indicador_falhou", indicador=indicador, erro=str(exc))
        return ResultadoIndicador(indicador=indicador, erro=f"{type(exc).__name__}: {exc}")

    if not isinstance(do_web, dict) or not isinstance(do_notion, dict):
        return ResultadoIndicador(
            indicador=indicador, erro="formato inesperado: esperava objeto dos dois lados"
        )
    return comparar_objetos(
        indicador, do_web, do_notion, escalares=escalares, listas=listas
    )


async def _conferir_um(
    indicador: str,
    buscar_web: Any,
    calcular_notion: Any,
    *,
    chave: str,
    campos: tuple[str, ...],
) -> ResultadoIndicador:
    try:
        do_web = await buscar_web()
        do_notion = await calcular_notion()
    except Exception as exc:  # noqa: BLE001 - um lado fora nao aborta os outros
        log.warning("reconciliacao.indicador_falhou", indicador=indicador, erro=str(exc))
        return ResultadoIndicador(indicador=indicador, erro=f"{type(exc).__name__}: {exc}")

    if not isinstance(do_web, list) or not isinstance(do_notion, list):
        return ResultadoIndicador(
            indicador=indicador, erro="formato inesperado: esperava lista dos dois lados"
        )
    return comparar_listas(indicador, do_web, do_notion, chave=chave, campos=campos)
