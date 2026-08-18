"""Agregacoes locais, replicando o que o Optmus Web hoje entrega.

Tres saidas, com o **mesmo formato** dos endpoints do Web, para poderem ser
comparadas campo a campo:

    monthly()         ->  /api/stats/monthly
    work_tasks()      ->  /api/work-tasks
    progress_alerts() ->  /api/progress-alerts

Formato identico e proposital: o objetivo desta fase nao e melhorar nada, e
provar equivalencia. Qualquer "melhoria" aqui vira uma diferenca no relatorio de
conferencia e some no meio do ruido.

Regras confirmadas pela conferencia de 2026-08-12, contra dados reais:

- **Soma com sinal, nunca abs().** Um "Saida" com valor negativo e estorno -
  dinheiro que voltou - e precisa REDUZIR a despesa. Aplicar abs() transformava
  um estorno de 200 num gasto novo de 200, errando o total em 400.
- **Linha sem Tipo nao entra em nenhum total**, igual ao Web. Mas vira aviso:
  sumir em silencio de um relatorio financeiro e inaceitavel.

Onde a regra do Web ainda nao e observavel de fora - janela de meses, tarefa
arquivada, fuso do calculo de dias - segue marcado com PRESUMIDO. Divergencia
nesses pontos e informacao, nao bug: e o que a conferencia serve para revelar.
"""

from __future__ import annotations

import asyncio
import json
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

from core.config import Settings
from core.logging import get_logger
from integrations.notion import (
    NotionClient,
    ler_checkbox,
    ler_data,
    ler_numero,
    ler_texto,
    propriedades,
)
from integrations.notion_map import NotionMap

log = get_logger("integrations.notion_stats")

MESES_ABREV: Final[tuple[str, ...]] = (
    "Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
    "Jul", "Ago", "Set", "Out", "Nov", "Dez",
)


class MapaIncompleto(RuntimeError):
    """O mapa do Notion nao tem o que esta agregacao precisa."""


@dataclass(slots=True)
class Aviso:
    """Algo que a agregacao encontrou e que pode explicar uma divergencia."""

    campo: str
    detalhe: str
    linhas: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"campo": self.campo, "detalhe": self.detalhe, "linhas": self.linhas}


class NotionStats:
    """As tres agregacoes, calculadas direto das bases do Notion."""

    def __init__(
        self, settings: Settings, mapa: NotionMap, client: NotionClient | None = None
    ) -> None:
        self._settings = settings
        self._mapa = mapa
        self._client = client or NotionClient(settings)
        self.avisos: list[Aviso] = []
        self._cache: dict[str, list[dict[str, Any]]] = {}
        self._em_voo: dict[str, asyncio.Task[list[dict[str, Any]]]] = {}

    async def _consultar(
        self,
        database_id: str,
        filtro: dict[str, Any] | None = None,
        ordenacao: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Consulta com memoria de curta duracao, por instancia.

        ``monthly()`` e chamado por tres agregacoes diferentes com o mesmo
        filtro; o relatorio mensal dispara nove agregacoes de uma vez. Sem isto
        a mesma consulta ia tres vezes para a rede.

        O ``_em_voo`` importa porque as agregacoes rodam em paralelo: sem ele,
        tres corrotinas conferem o cache no mesmo instante, todas veem vazio e
        as tres consultam - o cache so pegaria a partir da quarta.

        **A validade e a instancia**, que vive uma requisicao. Um NotionStats de
        vida longa serviria dado velho; nao existe nenhum hoje, e nao deve
        passar a existir sem trocar isto por um cache com prazo.
        """
        # A ordenacao entra na chave: mesma base e mesmo filtro em ordem
        # diferente sao respostas diferentes, e reaproveitar uma pela outra
        # trocaria a ordem sem ninguem perceber.
        chave = json.dumps(
            [database_id, filtro, ordenacao], sort_keys=True, ensure_ascii=False
        )
        if chave in self._cache:
            return self._cache[chave]
        if chave not in self._em_voo:
            self._em_voo[chave] = asyncio.create_task(
                self._client.consultar(database_id, filtro=filtro, ordenacao=ordenacao)
            )
        try:
            paginas = await self._em_voo[chave]
        finally:
            self._em_voo.pop(chave, None)
        self._cache[chave] = paginas
        return paginas

    def _avisar(self, campo: str, detalhe: str, linhas: int = 0) -> None:
        """Registra um aviso, sem repetir o mesmo achado.

        ``monthly()`` e chamado por tres agregacoes (mensal, poupanca e
        previsao) na mesma instancia, entao um unico lancamento sem Tipo
        aparecia tres vezes no relatorio - dando a impressao de tres linhas
        problematicas onde ha uma.
        """
        novo = Aviso(campo=campo, detalhe=detalhe, linhas=linhas)
        if any(
            a.campo == novo.campo and a.detalhe == novo.detalhe and a.linhas == novo.linhas
            for a in self.avisos
        ):
            return
        self.avisos.append(novo)
        log.warning("notion_stats.aviso", campo=campo, detalhe=detalhe, linhas=linhas)

    # -------------------------------------------------------------- monthly
    async def monthly(self, *, hoje: date | None = None) -> list[dict[str, Any]]:
        """Receita, despesa e saldo por mes - formato de /api/stats/monthly."""
        mapa = self._mapa.financeiro
        if not mapa.database_id:
            raise MapaIncompleto("financeiro.database_id nao configurado")

        hoje = hoje or datetime.now(UTC).date()
        meses = self._settings.notion_months_window
        primeiro = _primeiro_dia(hoje, meses - 1)

        # Filtra no servidor: puxar a base inteira para descartar em memoria e
        # desperdicio de chamada e estoura a paginacao em base grande.
        paginas = await self._consultar(
            mapa.database_id,
            filtro={"property": mapa.data, "date": {"on_or_after": primeiro.isoformat()}},
            ordenacao=[{"property": mapa.data, "direction": "ascending"}],
        )

        acumulado: dict[str, dict[str, float]] = {
            chave: {"income": 0.0, "expense": 0.0}
            for chave in _chaves_de_mes(primeiro, hoje)
        }
        sem_data = sem_valor = sem_tipo = tipo_vazio = 0

        for pagina in paginas:
            props = propriedades(pagina)
            quando = ler_data(props.get(mapa.data))
            valor = ler_numero(props.get(mapa.valor))
            if quando is None:
                sem_data += 1
                continue
            if valor is None:
                sem_valor += 1
                continue

            chave = quando[:7]
            if chave not in acumulado:
                continue

            rotulo = (ler_texto(props.get(mapa.tipo)) or "").strip()
            if not rotulo:
                # CONFERIDO: linha sem tipo nao entra em nenhum total - e o que
                # o Optmus Web faz. Some do relatorio, entao vira aviso: sumir
                # em silencio de um total financeiro e inaceitavel.
                tipo_vazio += 1
                continue
            if _casa(rotulo, mapa.valores_receita):
                # Soma COM SINAL, sem abs(): um lancamento negativo e estorno.
                # Aplicar abs() transformaria dinheiro que voltou em gasto novo.
                acumulado[chave]["income"] += valor
            elif _casa(rotulo, mapa.valores_despesa):
                acumulado[chave]["expense"] += valor
            else:
                sem_tipo += 1

        if sem_data:
            self._avisar("financeiro.data", "linhas sem data foram ignoradas", sem_data)
        if sem_valor:
            self._avisar("financeiro.valor", "linhas sem valor foram ignoradas", sem_valor)
        if sem_tipo:
            self._avisar(
                "financeiro.tipo",
                "linhas com tipo fora de valores_receita/valores_despesa",
                sem_tipo,
            )
        if tipo_vazio:
            self._avisar(
                "financeiro.tipo_vazio",
                "linhas sem Tipo preenchido foram ignoradas (mesma regra do Web) - "
                "nao entram em receita nem em despesa",
                tipo_vazio,
            )

        return [
            {
                "month": chave,
                "label": _rotulo_mes(chave),
                "income": round(v["income"], 2),
                "expense": round(v["expense"], 2),
                "balance": round(v["income"] - v["expense"], 2),
            }
            for chave, v in sorted(acumulado.items())
        ]

    # ------------------------------------------------- derivados do mensal
    async def savings_rate(self, *, hoje: date | None = None) -> dict[str, Any]:
        """Taxa de poupanca - formato de /api/stats/savings-rate.

        ``rate`` e ``null`` quando nao houve receita no mes: dividir por zero
        nao vira 0%, vira "nao da para dizer" - e o Web concorda.
        """
        hoje = hoje or datetime.now(UTC).date()
        meses = await self.monthly(hoje=hoje)
        atual = meses[-1] if meses else {"income": 0.0, "expense": 0.0}
        return {
            "currentRate": _taxa(atual["income"], atual["expense"]),
            "incomeSoFar": atual["income"],
            "expenseSoFar": atual["expense"],
            "trend": [
                {"month": m["month"], "label": m["label"], "rate": _taxa(m["income"], m["expense"])}
                for m in meses
            ],
        }

    async def forecast(self, *, hoje: date | None = None) -> dict[str, Any]:
        """Projecao do mes - formato de /api/stats/forecast.

        PRESUMIDO: a projecao extrapola linearmente pelo dia do mes, e a media
        historica cobre os meses ANTERIORES da janela (sem o atual).
        """
        hoje = hoje or datetime.now(UTC).date()
        meses = await self.monthly(hoje=hoje)
        atual = meses[-1] if meses else {"balance": 0.0, "income": 0.0, "expense": 0.0}
        # MEDIDO: o Web divide pelos ultimos 3 meses anteriores, contando os
        # zerados. Nao e "meses com movimento" - so 1 mes tinha movimento e a
        # conta bateu exatamente com 3 no divisor.
        anteriores = meses[:-1][-self._settings.notion_forecast_history_months :]
        dias_no_mes = monthrange(hoje.year, hoje.month)[1]

        media = (
            round(sum(m["balance"] for m in anteriores) / len(anteriores), 2)
            if anteriores
            else 0.0
        )
        return {
            # "Ago/2026" com ano de 4 digitos - diferente do "Ago/26" do mensal.
            "monthLabel": f"{MESES_ABREV[hoje.month - 1]}/{hoje.year}",
            "daysElapsed": hoje.day,
            "daysInMonth": dias_no_mes,
            "incomeSoFar": atual["income"],
            "expenseSoFar": atual["expense"],
            "netSoFar": atual["balance"],
            "projectedNet": round(atual["balance"] / hoje.day * dias_no_mes, 2),
            "historicalAverageNet": media,
        }

    async def category_spending(self, *, hoje: date | None = None) -> list[dict[str, Any]]:
        """Gastos do mes por categoria - formato de /api/stats/category-spending."""
        mapa = self._mapa.financeiro
        if not mapa.database_id:
            raise MapaIncompleto("financeiro.database_id nao configurado")

        hoje = hoje or datetime.now(UTC).date()
        inicio = hoje.replace(day=1)
        paginas = await self._consultar(
            mapa.database_id,
            filtro={"property": mapa.data, "date": {"on_or_after": inicio.isoformat()}},
        )

        por_categoria: dict[str, dict[str, float]] = {}
        for pagina in paginas:
            props = propriedades(pagina)
            quando = ler_data(props.get(mapa.data))
            valor = ler_numero(props.get(mapa.valor))
            rotulo = (ler_texto(props.get(mapa.tipo)) or "").strip()
            if quando is None or valor is None or quando[:7] != inicio.isoformat()[:7]:
                continue
            if not _casa(rotulo, mapa.valores_despesa):
                continue
            # PRESUMIDO: categoria vazia cai em "Outros", como o Web parece fazer.
            categoria = (ler_texto(props.get(mapa.categoria)) or "Outros").strip()
            grupo = por_categoria.setdefault(categoria, {"total": 0.0, "count": 0})
            grupo["total"] += valor
            grupo["count"] += 1

        return [
            {"categoria": nome, "total": round(v["total"], 2), "count": int(v["count"])}
            for nome, v in sorted(
                por_categoria.items(), key=lambda kv: kv[1]["total"], reverse=True
            )
        ]

    async def finance_weekly(self, *, hoje: date | None = None) -> list[dict[str, Any]]:
        """Oito semanas de receita/despesa - formato de /api/stats/finance-weekly."""
        mapa = self._mapa.financeiro
        if not mapa.database_id:
            raise MapaIncompleto("financeiro.database_id nao configurado")

        hoje = hoje or datetime.now(UTC).date()
        semanas = self._settings.notion_weeks_window
        primeiro = hoje - timedelta(weeks=semanas)

        paginas = await self._consultar(
            mapa.database_id,
            filtro={"property": mapa.data, "date": {"on_or_after": primeiro.isoformat()}},
        )

        baldes: dict[int, dict[str, float]] = {
            i: {"income": 0.0, "expense": 0.0} for i in range(semanas)
        }
        for pagina in paginas:
            props = propriedades(pagina)
            quando = ler_data(props.get(mapa.data))
            valor = ler_numero(props.get(mapa.valor))
            rotulo = (ler_texto(props.get(mapa.tipo)) or "").strip()
            if quando is None or valor is None or not rotulo:
                continue
            atras = _semanas_atras(hoje, date.fromisoformat(quando))
            if not 0 <= atras < semanas:
                continue
            if _casa(rotulo, mapa.valores_receita):
                baldes[atras]["income"] += valor
            elif _casa(rotulo, mapa.valores_despesa):
                baldes[atras]["expense"] += valor

        return [
            {
                "month": f"w-{atras}",
                "label": "Essa sem." if atras == 0 else f"-{atras} sem.",
                "income": round(baldes[atras]["income"], 2),
                "expense": round(baldes[atras]["expense"], 2),
                "balance": round(baldes[atras]["income"] - baldes[atras]["expense"], 2),
            }
            for atras in sorted(baldes, reverse=True)
        ]

    # -------------------------------------------------------------- estudos
    async def study(self, *, hoje: date | None = None) -> dict[str, Any]:
        """Estudos por status e proximo prazo - formato de /api/stats/study.

        PRESUMIDO: "proximo prazo" e a data MAIS ANTIGA da base, nao a proxima
        data futura - e o que o Web devolve hoje (aponta para um prazo vencido).
        """
        mapa = self._mapa.estudos
        if not mapa.database_id:
            raise MapaIncompleto("estudos.database_id nao configurado")

        paginas = await self._consultar(mapa.database_id)
        por_status: dict[str, int] = {}
        candidatos: list[dict[str, Any]] = []

        for pagina in paginas:
            props = propriedades(pagina)
            status = (ler_texto(props.get(mapa.status)) or "Sem status").strip()
            por_status[status] = por_status.get(status, 0) + 1
            quando = ler_data(props.get(mapa.data))
            if quando is not None:
                candidatos.append(
                    {
                        "titulo": ler_texto(props.get(mapa.titulo)) or "(sem titulo)",
                        "disciplina": ler_texto(props.get(mapa.disciplina)) or "",
                        "tipo": ler_texto(props.get(mapa.tipo)) or "",
                        "data": quando,
                    }
                )

        candidatos.sort(key=lambda c: c["data"])
        return {
            "porStatus": [
                {"status": nome, "quantidade": qtd} for nome, qtd in sorted(por_status.items())
            ],
            "proximoPrazo": candidatos[0] if candidatos else None,
        }

    async def study_month(self, *, hoje: date | None = None) -> list[dict[str, Any]]:
        """Provas e trabalhos com data no mes - bloco do relatorio mensal.

        NAO e ``progress_alerts`` com outra roupa, e a diferenca importa:

        - o alerta esconde o que ja foi **concluido**; aqui o concluido aparece
          (``ingles``, ``Concluído``, esta no PDF do Web);
        - o alerta usa janela de +-30 dias corridos; aqui o recorte e o **mes
          civil** (``matematiica``, 29/07, some do PDF de agosto).

        MEDIDO tambem: linha sem ``Tipo`` sai como **"Outro"** no PDF, nao em
        branco - e ``Prova CALCULO 2`` nao tem tipo nenhum no Notion.
        """
        mapa = self._mapa.estudos
        if not mapa.database_id:
            raise MapaIncompleto("estudos.database_id nao configurado")

        hoje = hoje or datetime.now(UTC).date()
        primeiro = hoje.replace(day=1)
        ultimo = _ultimo_dia(hoje)

        paginas = await self._consultar(
            mapa.database_id, filtro=_entre(mapa.data, primeiro, ultimo)
        )
        itens: list[dict[str, Any]] = []
        for pagina in paginas:
            props = propriedades(pagina)
            quando = ler_data(props.get(mapa.data))
            if quando is None:
                continue
            itens.append(
                {
                    "titulo": ler_texto(props.get(mapa.titulo)) or "(sem titulo)",
                    "disciplina": ler_texto(props.get(mapa.disciplina)) or "",
                    "tipo": (ler_texto(props.get(mapa.tipo)) or "").strip() or "Outro",
                    "status": (ler_texto(props.get(mapa.status)) or "").strip(),
                    "data": quando,
                }
            )
        itens.sort(key=lambda i: str(i["data"]))
        return itens

    # --------------------------------------------------------------- tarefas
    async def tasks(self) -> dict[str, Any]:
        """Tarefas - formato de /api/stats/tasks.

        **Sem recorte de mes, de proposito.** O PDF do Web diz, em nota de
        rodape, que "o Notion nao guarda a data em que cada tarefa foi
        concluida"; o numero e o total historico. A base *tem* uma coluna
        ``Data de conclusão`` - a justificativa do Web esta errada -, mas o que
        precisa bater agora e o numero dele. Filtrar por mes aqui daria 0 contra
        os 5 do Web.

        NAO CONFERIDO: ``porPrioridade`` e os baldes de ``atraso``. As cinco
        tarefas da base estao **todas concluidas**, e o Web devolveu
        ``porPrioridade: []`` mesmo com prioridade preenchida em quatro delas -
        o que indica que so pendente entra nesses dois blocos. E a leitura que
        implementei, mas nenhuma tarefa pendente exercitou o caminho.
        """
        mapa = self._mapa.tarefas
        if not mapa.database_id:
            raise MapaIncompleto("tarefas.database_id nao configurado")

        hoje = datetime.now(UTC).date()
        paginas = await self._consultar(mapa.database_id)
        concluidas = 0
        pendentes: list[dict[str, Any]] = []

        for pagina in paginas:
            props = propriedades(pagina)
            if _casa(ler_texto(props.get(mapa.status)) or "", mapa.valores_concluido):
                concluidas += 1
                continue
            prazo = ler_data(props.get(mapa.prazo))
            atraso: int | None = None
            if prazo:
                try:
                    atraso = (hoje - date.fromisoformat(prazo[:10])).days
                except ValueError:
                    atraso = None
            pendentes.append(
                {
                    "titulo": ler_texto(props.get(mapa.titulo)) or "(sem titulo)",
                    "prioridade": (ler_texto(props.get(mapa.prioridade)) or "").strip(),
                    "prazo": prazo,
                    "atraso": atraso,
                }
            )

        por_prioridade: dict[str, int] = {}
        baldes = dict.fromkeys(_BALDES_ATRASO, 0)
        atrasadas: list[dict[str, Any]] = []
        for tarefa in pendentes:
            nome = str(tarefa["prioridade"])
            if nome:
                por_prioridade[nome] = por_prioridade.get(nome, 0) + 1
            baldes[_balde_atraso(tarefa["atraso"])] += 1
            if tarefa["atraso"] is not None and int(tarefa["atraso"]) > 0:
                atrasadas.append(tarefa)

        return {
            "totalPendentes": len(pendentes),
            "totalConcluidas": concluidas,
            "totalAtrasadas": len(atrasadas),
            "porPrioridade": [
                {"prioridade": nome, "quantidade": qtd}
                for nome, qtd in sorted(por_prioridade.items())
            ],
            "atraso": [{"label": nome, "quantidade": baldes[nome]} for nome in _BALDES_ATRASO],
            "atrasadas": atrasadas,
        }

    async def grades(self) -> dict[str, Any]:
        """Notas escolares - formato de /api/stats/grades.

        A fonte e a base propria "Notas por trimestre" (Disciplina x Trimestre x
        Pontos), NAO a coluna ``Nota`` da base Estudos. Isso foi medido, nao
        deduzido: com duas linhas de Estudos preenchidas - nota 5,0 (Tipo
        "Prova", "Concluído") e nota 3,64 (sem tipo, "Em andamento") - o Web
        devolveu ``disciplinas: []`` nas duas vezes. So depois que a base de
        notas foi compartilhada a disciplina apareceu do lado dele.

        Derivacoes conferidas contra uma disciplina com 18 pontos no 1o
        trimestre (total 30/30/40, minimo de aprovacao 60)::

            obtido        = soma dos pontos lancados                  18
            restante      = soma do 'total' dos trimestres SEM nota   70
            maximoPossivel= obtido + restante                         88
            precisa       = minimoAprovacao - obtido                  42

        PRESUMIDO: os rotulos de ``situacao`` fora de "em andamento". So esse
        foi observado; "aprovado" (obtido >= minimo) e "reprovado" (maximo
        possivel < minimo) sao a leitura natural, mas nenhum dos dois foi visto.
        """
        notas = self._mapa.notas
        mapa = self._mapa.notas_trimestre
        if not mapa.database_id:
            self._avisar(
                "notas_escolares.fonte",
                "notas_trimestre.database_id nao configurado: so as constantes "
                "sao reproduzidas, sem disciplinas",
            )
            return {
                "disciplinas": [],
                "totalAno": notas.total_ano,
                "minimoAprovacao": notas.minimo_aprovacao,
                "trimestres": notas.trimestres,
            }

        # nome do trimestre -> definicao, para casar o select com a constante
        por_nome = {str(t["nome"]): t for t in notas.trimestres}
        pontos: dict[str, dict[str, float]] = {}
        ordem: list[str] = []
        sem_trimestre = 0

        for pagina in await self._consultar(mapa.database_id):
            props = propriedades(pagina)
            disciplina = (ler_texto(props.get(mapa.disciplina)) or "").strip()
            if not disciplina:
                continue
            rotulo = (ler_texto(props.get(mapa.trimestre)) or "").strip()
            if rotulo not in por_nome:
                sem_trimestre += 1
                continue
            valor = ler_numero(props.get(mapa.pontos))
            if valor is None:
                continue
            if disciplina not in pontos:
                pontos[disciplina] = {}
                ordem.append(disciplina)
            # Duas linhas do mesmo trimestre somam: e lancamento, nao substituicao.
            pontos[disciplina][rotulo] = pontos[disciplina].get(rotulo, 0.0) + valor

        if sem_trimestre:
            self._avisar(
                "notas_escolares.trimestre",
                "linhas com trimestre vazio ou fora dos trimestres configurados "
                "foram ignoradas",
                sem_trimestre,
            )

        disciplinas: list[dict[str, Any]] = []
        for nome in ordem:
            lancado = pontos[nome]
            detalhe: list[dict[str, Any]] = []
            obtido = 0.0
            restante = 0.0
            for definicao in notas.trimestres:
                marcados = lancado.get(str(definicao["nome"]))
                detalhe.append({**definicao, "pontos": marcados})
                if marcados is None:
                    restante += float(definicao["total"])
                else:
                    obtido += marcados
            maximo = obtido + restante
            disciplinas.append(
                {
                    "disciplina": nome,
                    "trimestres": detalhe,
                    "obtido": obtido,
                    "restante": restante,
                    "precisa": notas.minimo_aprovacao - obtido,
                    "situacao": _situacao(obtido, maximo, notas.minimo_aprovacao),
                    "maximoPossivel": maximo,
                }
            )

        return {
            "disciplinas": disciplinas,
            "totalAno": notas.total_ano,
            "minimoAprovacao": notas.minimo_aprovacao,
            "trimestres": notas.trimestres,
        }

    # ------------------------------------------------------------------ sono
    async def sleep(self) -> dict[str, Any]:
        """Registro de sono - formato de /api/stats/sleep.

        A base nao tem coluna de data: ``Noite`` e o **titulo**, no formato
        ``DD/MM`` (as vezes com espaco sobrando). O Web devolve ``data:
        "2026-08-09"`` para o titulo ``" 09/08"``, ou seja: dia/mes, ano
        corrente, e ``label`` e o titulo ja aparado.

        Conferido contra duas noites (8h "Bom", 5h "Ruim") em 91 linhas::

            totalLinhas       = TODAS as linhas da base, inclusive as vazias  91
            noitesRegistradas = so as que tem horas                            2
            mediaHoras        = (8 + 5) / 2                                  6.5
            noitesNaMeta      = noites com horas >= metaHoras                  1

        PRESUMIDO: o ano. Com so ``DD/MM`` no titulo, uma noite de dezembro
        lida em janeiro cai no ano errado - nao da para saber sem uma linha que
        atravesse a virada. PRESUMIDO tambem: ``ultimasNoites`` sai completa,
        sem limite de quantidade; com duas linhas nao da para ver corte nenhum.
        """
        mapa = self._mapa.sono
        if not mapa.database_id:
            raise MapaIncompleto("sono.database_id nao configurado")

        hoje = datetime.now(UTC).date()
        paginas = await self._consultar(mapa.database_id)
        noites: list[dict[str, Any]] = []
        sem_noite = 0
        formato_estranho = 0

        for pagina in paginas:
            props = propriedades(pagina)
            horas = ler_numero(props.get(mapa.horas))
            if horas is None:
                continue
            rotulo = (ler_texto(props.get(mapa.noite)) or "").strip()
            if not rotulo:
                sem_noite += 1
                continue
            quando = _dia_mes(rotulo, hoje.year)
            if quando is None:
                formato_estranho += 1
                continue
            noites.append(
                {
                    "label": rotulo,
                    "data": quando.isoformat(),
                    "horas": horas,
                    "qualidade": ler_texto(props.get(mapa.qualidade)) or None,
                }
            )

        if sem_noite:
            # Sao horas registradas que nao contam em lugar nenhum: nem na media,
            # nem em porQualidade. Vale dizer, porque o conserto e trivial.
            self._avisar(
                "sono.sem_noite",
                "linhas com horas preenchidas mas sem a noite ficam de fora dos "
                "totais (o Web tambem as ignora) - preencha a noite como '08/08'",
                sem_noite,
            )
        if formato_estranho:
            # NAO CONFERIDO: so vi o Web descartar noite VAZIA. Se ele aceita
            # outro formato de texto, este ramo diverge - e por isso ele avisa.
            self._avisar(
                "sono.formato",
                "linhas com a noite preenchida fora do formato DD/MM foram "
                "ignoradas - nao confirmado se o Web faz o mesmo",
                formato_estranho,
            )

        noites.sort(key=lambda n: str(n["data"]))
        por_qualidade: dict[str, int] = {}
        for noite in noites:
            nome = noite["qualidade"]
            if nome:
                por_qualidade[nome] = por_qualidade.get(nome, 0) + 1

        total_horas = sum(float(n["horas"]) for n in noites)
        meta = mapa.meta_horas
        return {
            "ultimasNoites": noites,
            "mediaHoras": round(total_horas / len(noites), 2) if noites else 0,
            "noitesRegistradas": len(noites),
            "totalLinhas": len(paginas),
            "porQualidade": [
                {"qualidade": nome, "quantidade": qtd}
                for nome, qtd in sorted(por_qualidade.items())
            ],
            "melhorNoite": max(noites, key=lambda n: float(n["horas"])) if noites else None,
            "piorNoite": min(noites, key=lambda n: float(n["horas"])) if noites else None,
            "noitesNaMeta": sum(1 for n in noites if float(n["horas"]) >= meta),
            "metaHoras": meta,
        }

    # --------------------------------------------------------------- treino
    async def workout_frequency(self, *, hoje: date | None = None) -> list[dict[str, Any]]:
        """Treinos por semana - formato de /api/stats/workout-frequency."""
        hoje = hoje or datetime.now(UTC).date()
        semanas = self._settings.notion_workout_weeks
        # Mesma janela rolante do financeiro_semanal. A regra de balde em si
        # ainda nao foi exercitada aqui: enquanto nao houver treino concluido,
        # todos os baldes sao zero dos dois lados.
        datas = await self._datas_de_treino(hoje - timedelta(weeks=semanas), hoje=hoje)

        contagem = dict.fromkeys(range(semanas), 0)
        for quando in datas:
            atras = _semanas_atras(hoje, quando)
            if 0 <= atras < semanas:
                contagem[atras] += 1

        return [
            {
                "label": "Essa sem." if atras == 0 else f"-{atras} sem.",
                "quantidade": contagem[atras],
            }
            for atras in sorted(contagem, reverse=True)
        ]

    async def workout_monthly(self, *, hoje: date | None = None) -> list[dict[str, Any]]:
        """Treinos por mes - formato de /api/stats/workout-monthly."""
        hoje = hoje or datetime.now(UTC).date()
        primeiro = _primeiro_dia(hoje, self._settings.notion_months_window - 1)
        datas = await self._datas_de_treino(primeiro, hoje=hoje)

        contagem = dict.fromkeys(_chaves_de_mes(primeiro, hoje), 0)
        for quando in datas:
            chave = quando.isoformat()[:7]
            if chave in contagem:
                contagem[chave] += 1

        return [
            {"label": _rotulo_mes(chave), "quantidade": contagem[chave]}
            for chave in sorted(contagem)
        ]

    async def _datas_de_treino(self, desde: date, *, hoje: date) -> list[date]:
        """Datas dos treinos que contam para as duas series de treino.

        MEDIDO: o Web conta apenas treino **concluido**. Um treino com data
        PASSADA e status "Planejado" continua zerado do lado dele - foi esse o
        teste que separou as duas hipoteses. Antes disso o unico treino era
        futuro E planejado, e as duas regras explicavam o mesmo zero.

        O corte por ``hoje`` continua, mas nao e mais uma regra inferida: e o
        fim da propria janela ("ultimas N semanas/meses"). Sem ele as duas
        series discordariam entre si, porque uma data futura cai fora dos
        baldes semanais e dentro do balde do mes corrente.
        """
        mapa = self._mapa.treino
        if not mapa.database_id:
            raise MapaIncompleto("treino.database_id nao configurado")

        paginas = await self._consultar(
            mapa.database_id, filtro=_entre(mapa.data, desde, hoje)
        )
        datas: list[date] = []
        nao_realizados = 0
        for pagina in paginas:
            props = propriedades(pagina)
            quando = ler_data(props.get(mapa.data))
            if quando is None:
                continue
            try:
                dia = date.fromisoformat(quando)
            except ValueError:
                continue
            if dia > hoje:
                continue
            if not _casa(ler_texto(props.get(mapa.status)) or "", mapa.valores_realizado):
                nao_realizados += 1
                continue
            datas.append(dia)

        if nao_realizados:
            # Sem este aviso, um treino recem-lancado como "Planejado" some da
            # contagem sem explicacao nenhuma.
            self._avisar(
                "treino.nao_realizado",
                "treinos na janela com status diferente de concluido foram ignorados "
                "(mesma regra do Web) - um treino so conta depois de marcado como feito",
                nao_realizados,
            )
        return datas

    # ----------------------------------------------------------- work_tasks
    async def work_tasks(self) -> list[dict[str, Any]]:
        """Tarefas de trabalho por tipo - formato de /api/work-tasks."""
        mapa = self._mapa.trabalho
        if not mapa.database_id:
            raise MapaIncompleto("trabalho.database_id nao configurado")

        paginas = await self._consultar(mapa.database_id)
        por_tipo: dict[str, dict[str, int]] = {}

        for pagina in paginas:
            props = propriedades(pagina)
            tipo = (ler_texto(props.get(mapa.tipo)) or "Sem tipo").strip()
            grupo = por_tipo.setdefault(tipo, {"total": 0, "ativo": 0, "concluido": 0})
            grupo["total"] += 1

            marcado = ler_checkbox(props.get(mapa.status))
            if marcado is None:
                situacao = (ler_texto(props.get(mapa.status)) or "").strip()
                concluido = _casa(situacao, mapa.valores_concluido)
            else:
                concluido = marcado

            grupo["concluido" if concluido else "ativo"] += 1

        return [
            {"tipo": tipo, "total": v["total"], "ativo": v["ativo"], "concluido": v["concluido"]}
            for tipo, v in sorted(por_tipo.items())
        ]

    # ------------------------------------------------------ progress_alerts
    async def progress_alerts(self, *, hoje: date | None = None) -> list[dict[str, Any]]:
        """Prazos proximos - formato de /api/progress-alerts."""
        if not any(p.database_id for p in self._mapa.prazos):
            raise MapaIncompleto("nenhuma base de prazos configurada")

        hoje = hoje or datetime.now(UTC).date()
        limite = hoje + timedelta(days=self._settings.notion_alert_window_days)
        # PRESUMIDO e NAO CONFERIDO: sem limite para tras, o Core lista prazo
        # vencido de qualquer epoca. So passou na conferencia porque existe um
        # unico item vencido. Configure quando souber a regra do Web.
        piso = (
            hoje - timedelta(days=self._settings.notion_alert_past_days)
            if self._settings.notion_alert_past_days is not None
            else None
        )
        alertas: list[dict[str, Any]] = []

        for fonte in self._mapa.prazos:
            if not fonte.database_id:
                continue
            # Assimetria proposital, medida contra o Web: a borda da frente e
            # INCLUSIVA (+30 aparece) e a de tras e EXCLUSIVA (-30 nao aparece,
            # -29 aparece). Isso reproduz uma janela de +-30 dias calculada em
            # TIMESTAMP: a meia-noite de 30 dias atras ja e anterior a
            # "agora - 30 dias", enquanto a de daqui a 30 dias ainda nao passou
            # de "agora + 30 dias". Com "on_or_after" o item de -30 voltaria.
            janela: dict[str, str] = {"on_or_before": limite.isoformat()}
            if piso is not None:
                janela["after"] = piso.isoformat()
            paginas = await self._consultar(
                fonte.database_id,
                filtro={"property": fonte.data, "date": janela},
                ordenacao=[{"property": fonte.data, "direction": "ascending"}],
            )
            for pagina in paginas:
                props = propriedades(pagina)
                quando = ler_data(props.get(fonte.data))
                if quando is None:
                    continue
                try:
                    dia = date.fromisoformat(quando)
                except ValueError:
                    continue
                # MEDIDO: o Web nao alerta sobre item concluido. Faz sentido -
                # um painel de prazos avisa do que falta, nao do que acabou.
                situacao = (ler_texto(props.get(fonte.status)) or "").strip()
                if _casa(situacao, fonte.valores_concluido):
                    continue
                alertas.append(
                    {
                        "tipo": fonte.rotulo,
                        "titulo": ler_texto(props.get(fonte.titulo)) or "(sem titulo)",
                        "detalhe": (
                            (ler_texto(props.get(fonte.detalhe)) or "") if fonte.detalhe else ""
                        ),
                        "data": quando,
                        "diasRestantes": (dia - hoje).days,
                    }
                )

        alertas.sort(key=lambda a: a["data"])
        return alertas


async def diagnosticar_alertas(
    stats: NotionStats, *, hoje: date | None = None, dias: int | None = None
) -> dict[str, Any]:
    """Mostra cada prazo da base e por que ele entra ou nao no relatorio.

    Existe porque "mova o item para +30 dias" e um pedido que convida ao erro:
    um dia de diferenca coloca a linha fora da janela e o resultado fica
    identico ao de um item muito distante - parece confirmacao, e nao e.

    Em vez de pedir aritmetica de data, este diagnostico diz a data exata que
    cai dentro e a que cai fora.
    """
    hoje = hoje or datetime.now(UTC).date()
    janela = stats._settings.notion_alert_window_days
    piso_dias = stats._settings.notion_alert_past_days
    limite = hoje + timedelta(days=janela)
    piso = hoje - timedelta(days=piso_dias) if piso_dias is not None else None

    linhas: list[dict[str, Any]] = []
    for fonte in stats._mapa.prazos:
        if not fonte.database_id:
            continue
        for pagina in await stats._client.consultar(fonte.database_id):
            props = propriedades(pagina)
            quando = ler_data(props.get(fonte.data))
            if quando is None:
                linhas.append(
                    {
                        "titulo": ler_texto(props.get(fonte.titulo)) or "(sem titulo)",
                        "data": None,
                        "dias": None,
                        "no_core": False,
                        "motivo": f"propriedade {fonte.data!r} vazia ou de outro tipo",
                    }
                )
                continue
            try:
                # NAO chamar de "dias": e o nome do parametro da funcao, e
                # reatribuir aqui destruia o valor pedido pelo chamador a cada
                # linha lida do Notion.
                dias_da_linha = (date.fromisoformat(quando) - hoje).days
            except ValueError:
                continue

            depois = quando > limite.isoformat()
            # Borda de tras exclusiva - ver comentario em progress_alerts().
            antes = piso is not None and quando <= piso.isoformat()
            linhas.append(
                {
                    "titulo": ler_texto(props.get(fonte.titulo)) or "(sem titulo)",
                    "data": quando,
                    "dias": dias_da_linha,
                    "no_core": not (depois or antes),
                    "motivo": (
                        f"{dias_da_linha - janela} dia(s) depois do limite" if depois
                        else f"anterior ao piso de {piso_dias} dias" if antes
                        else "dentro da janela"
                    ),
                }
            )

    linhas.sort(key=lambda x: (x["dias"] is None, x["dias"]))
    saida: dict[str, Any] = {
        "hoje": hoje.isoformat(),
        "janela_dias": janela,
        "piso_dias": piso_dias,
        "ultima_data_que_entra": limite.isoformat(),
        "primeira_data_que_fica_de_fora": (limite + timedelta(days=1)).isoformat(),
        "data_mais_antiga_que_entra": (
            (piso + timedelta(days=1)).isoformat() if piso else "sem limite para tras"
        ),
        "linhas": linhas,
        "como_testar": {
            "dentro_da_janela": limite.isoformat(),
            "fora_por_um_dia": (limite + timedelta(days=1)).isoformat(),
            "passado_distante": (hoje - timedelta(days=60)).isoformat(),
        },
    }

    if dias is not None:
        # Converte "quero testar N dias" na data literal para colar no Notion.
        # Enquanto o pedido for "mova para +N dias", o erro de um dia continua
        # possivel - e um item fora por 1 dia produz o mesmo relatorio de um
        # item fora por 40, o que parece confirmacao e nao e.
        alvo = hoje + timedelta(days=dias)
        saida["data_para_testar"] = {
            "dias": dias,
            "data": alvo.isoformat(),
            "entraria_no_core": _dentro(alvo, piso, limite),
        }
    return saida


def _dentro(alvo: date, piso: date | None, limite: date) -> bool:
    """Frente inclusiva, tras exclusiva - ver comentario em progress_alerts()."""
    return alvo <= limite and (piso is None or alvo > piso)


def _entre(propriedade: str, inicio: date, fim: date) -> dict[str, Any]:
    """Filtro de intervalo fechado para o Notion.

    Precisa de ``and`` com duas condicoes. Um unico objeto ``date`` com
    ``on_or_after`` E ``on_or_before`` juntos nao e intervalo: o Notion aplica
    so uma das chaves e devolve 200, sem erro nenhum. Foi assim que uma prova de
    29/07 entrou no relatorio de agosto - o limite superior valia e o inferior
    era ignorado em silencio.
    """
    return {
        "and": [
            {"property": propriedade, "date": {"on_or_after": inicio.isoformat()}},
            {"property": propriedade, "date": {"on_or_before": fim.isoformat()}},
        ]
    }


# Rotulos e cortes conferidos contra /api/stats/tasks.
_BALDES_ATRASO: Final[tuple[str, ...]] = (
    "Sem prazo",
    "No prazo",
    "1-7 dias",
    "8-30 dias",
    "30+ dias",
)


def _balde_atraso(dias: int | None) -> str:
    """Balde de atraso de uma tarefa pendente.

    NAO CONFERIDO: os cortes vieram dos rotulos, nao de medicao - as cinco
    tarefas da base estao concluidas e nenhum balde foi exercitado.
    """
    if dias is None:
        return "Sem prazo"
    if dias <= 0:
        return "No prazo"
    if dias <= 7:
        return "1-7 dias"
    if dias <= 30:
        return "8-30 dias"
    return "30+ dias"


def _ultimo_dia(quando: date) -> date:
    """Ultimo dia do mes de ``quando``.

    Escrito a mao em vez de _primeiro_dia(hoje, -1) - 1 dia: aquele caminho
    monta date(ano, 13, 1) em dezembro e levanta ValueError.
    """
    if quando.month == 12:
        return date(quando.year, 12, 31)
    return date(quando.year, quando.month + 1, 1) - timedelta(days=1)


def _situacao(obtido: float, maximo: float, minimo: float) -> str:
    """Situacao da disciplina. So "em andamento" foi observado no Web."""
    if obtido >= minimo:
        return "aprovado"
    if maximo < minimo:
        return "reprovado"
    return "em andamento"


def _dia_mes(rotulo: str, ano: int) -> date | None:
    """Converte um titulo "DD/MM" na data do ano corrente.

    Devolve None em vez de levantar: a base de sono tem dezenas de linhas em
    branco, e uma linha mal preenchida nao pode derrubar o indicador inteiro.
    """
    partes = rotulo.replace("-", "/").split("/")
    if len(partes) != 2:
        return None
    try:
        dia, mes = int(partes[0]), int(partes[1])
        return date(ano, mes, dia)
    except ValueError:
        return None


def _taxa(income: float, expense: float) -> float | None:
    """Percentual poupado. None quando nao houve receita - nao e zero."""
    if not income:
        return None
    return round((income - expense) / income * 100, 2)


def _semanas_atras(hoje: date, quando: date) -> int:
    """Indice do balde semanal - janela ROLANTE de 7 dias a partir de hoje.

    MEDIDO contra o Web, nao presumido: das tres regras testadas com os
    lancamentos reais, a rolante deu 0/8 baldes divergentes e as duas semanas
    civis (segunda e domingo) deram 3/8.

    A pista estava em dois lancamentos, 2026-07-10 e 2026-07-16: em semana civil
    caem em semanas diferentes, na janela rolante caem na mesma - era o balde de
    177 do Web se partindo em 157 + 20 do lado do Core.
    """
    return (hoje - quando).days // 7


def _casa(valor: str, aceitos: list[str]) -> bool:
    alvo = valor.strip().lower()
    return bool(alvo) and any(alvo == a.strip().lower() for a in aceitos)


def _primeiro_dia(hoje: date, meses_atras: int) -> date:
    ano, mes = hoje.year, hoje.month - meses_atras
    while mes <= 0:
        mes += 12
        ano -= 1
    return date(ano, mes, 1)


def _chaves_de_mes(inicio: date, fim: date) -> list[str]:
    chaves: list[str] = []
    ano, mes = inicio.year, inicio.month
    while (ano, mes) <= (fim.year, fim.month):
        chaves.append(f"{ano:04d}-{mes:02d}")
        mes += 1
        if mes > 12:
            mes = 1
            ano += 1
    return chaves


def _rotulo_mes(chave: str) -> str:
    ano, mes = chave.split("-")
    return f"{MESES_ABREV[int(mes) - 1]}/{ano[2:]}"
