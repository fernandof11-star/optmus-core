"""Cliente do Notion, agregacoes locais e conferencia contra o Optmus Web."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, ClassVar

import httpx
import pytest

from core.config import Settings, get_settings, reset_settings_cache
from integrations.notion import (
    NotionClient,
    NotionError,
    ler_data,
    ler_numero,
    ler_texto,
)
from integrations.notion_map import (
    MapaEstudos,
    MapaFinanceiro,
    MapaNotasTrimestre,
    MapaPrazos,
    MapaSono,
    MapaTarefas,
    MapaTrabalho,
    MapaTreino,
    NotionMap,
    de_dict,
)
from integrations.notion_stats import MapaIncompleto, NotionStats, diagnosticar_alertas
from integrations.reconciliacao import comparar_listas, comparar_objetos, conferir
from main import METODOS_NOTION

HOJE = date(2026, 8, 12)


@pytest.fixture
def notion_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("OPTMUS_NOTION_TOKEN", "ntn_teste")
    monkeypatch.setenv("OPTMUS_NOTION_MONTHS_WINDOW", "3")
    reset_settings_cache()
    return get_settings()


def _mock(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    vistas: list[httpx.Request] = []
    original = httpx.AsyncClient.__init__

    def _init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        def _captura(request: httpx.Request) -> httpx.Response:
            vistas.append(request)
            return handler(request)

        kwargs["transport"] = httpx.MockTransport(_captura)
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)
    return vistas


def _pagina(**props: Any) -> dict[str, Any]:
    return {"properties": props}


def _data(valor: str | None) -> dict[str, Any]:
    return {"type": "date", "date": {"start": valor} if valor else None}


def _numero(valor: float | None) -> dict[str, Any]:
    return {"type": "number", "number": valor}


def _select(nome: str | None) -> dict[str, Any]:
    return {"type": "select", "select": {"name": nome} if nome else None}


def _titulo(texto: str) -> dict[str, Any]:
    return {"type": "title", "title": [{"plain_text": texto}]}


# ------------------------------------------------------------------ leitores
def test_leitores_desembrulham_o_envelope_do_notion() -> None:
    assert ler_numero(_numero(32.5)) == 32.5
    assert ler_numero(_numero(None)) is None
    assert ler_data(_data("2026-08-12T10:00:00")) == "2026-08-12"
    assert ler_data(_data(None)) is None
    assert ler_texto(_select("Despesa")) == "Despesa"
    assert ler_texto(_titulo("Prova CALCULO 2")) == "Prova CALCULO 2"


def test_campo_vazio_e_none_nao_zero() -> None:
    """Vazio e zero sao coisas diferentes numa soma financeira."""
    assert ler_numero(_numero(None)) is None
    assert ler_numero(_numero(0)) == 0.0


# -------------------------------------------------------------------- cliente
async def test_paginacao_segue_o_cursor(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ignorar next_cursor produz total truncado que parece certo."""
    chamadas = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas["n"] += 1
        if chamadas["n"] == 1:
            return httpx.Response(
                200, json={"results": [_pagina()], "has_more": True, "next_cursor": "c2"}
            )
        return httpx.Response(200, json={"results": [_pagina(), _pagina()], "has_more": False})

    vistas = _mock(monkeypatch, handler)
    paginas = await NotionClient(notion_settings).consultar("db1")

    assert len(paginas) == 3
    assert chamadas["n"] == 2
    import json as _json

    assert _json.loads(vistas[1].content)["start_cursor"] == "c2"


async def test_429_respeita_retry_after(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    chamadas = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas["n"] += 1
        if chamadas["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"results": [], "has_more": False})

    _mock(monkeypatch, handler)
    await NotionClient(notion_settings).consultar("db1")
    assert chamadas["n"] == 2


async def test_404_explica_o_compartilhamento(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O erro mais comum do Notion: a base existe, mas nao foi compartilhada."""
    _mock(monkeypatch, lambda r: httpx.Response(404, json={}))
    with pytest.raises(NotionError, match="Conexoes"):
        await NotionClient(notion_settings).consultar("db1")


async def test_sem_token_falha_com_instrucao(settings: Settings) -> None:
    cliente = NotionClient(settings)
    assert cliente.configurado is False
    with pytest.raises(NotionError, match="my-integrations"):
        await cliente.consultar("db1")


# ----------------------------------------------------------------- monthly
def _mapa_completo() -> NotionMap:
    return NotionMap(
        financeiro=MapaFinanceiro(
            database_id="fin",
            data="Data",
            valor="Valor",
            tipo="Tipo",
            valores_receita=["Receita"],
            valores_despesa=["Despesa"],
        ),
        trabalho=MapaTrabalho(
            database_id="trab", tipo="Tipo", status="Status", valores_concluido=["Concluido"]
        ),
        estudos=MapaEstudos(database_id="est", titulo="Nome", data="Data", disciplina="Materia"),
        treino=MapaTreino(database_id="trein", data="Data"),
        notas_trimestre=MapaNotasTrimestre(database_id="notas"),
        tarefas=MapaTarefas(database_id="taref", titulo="Tarefa", status="Status"),
        sono=MapaSono(database_id="sono"),
        prazos=[
            MapaPrazos(database_id="est", titulo="Nome", data="Data", detalhe="Materia")
        ],
    )


async def test_monthly_soma_receita_e_despesa(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    linhas = [
        _pagina(Data=_data("2026-08-01"), Valor=_numero(1000), Tipo=_select("Receita")),
        _pagina(Data=_data("2026-08-05"), Valor=_numero(250), Tipo=_select("Despesa")),
        _pagina(Data=_data("2026-07-10"), Valor=_numero(80), Tipo=_select("Despesa")),
    ]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    saida = await NotionStats(notion_settings, _mapa_completo()).monthly(hoje=HOJE)
    por_mes = {m["month"]: m for m in saida}

    assert por_mes["2026-08"] == {
        "month": "2026-08",
        "label": "Ago/26",
        "income": 1000.0,
        "expense": 250.0,
        "balance": 750.0,
    }
    assert por_mes["2026-07"]["expense"] == 80.0
    assert por_mes["2026-06"]["balance"] == 0.0, "mes sem lancamento aparece zerado"


async def test_monthly_respeita_a_janela_de_meses(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": [], "has_more": False}))
    saida = await NotionStats(notion_settings, _mapa_completo()).monthly(hoje=HOJE)
    assert [m["month"] for m in saida] == ["2026-06", "2026-07", "2026-08"]


async def test_monthly_avisa_sobre_linha_ignorada(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linha descartada em silencio e a origem classica de divergencia."""
    linhas = [
        _pagina(Data=_data(None), Valor=_numero(10), Tipo=_select("Despesa")),
        _pagina(Data=_data("2026-08-01"), Valor=_numero(None), Tipo=_select("Despesa")),
        _pagina(Data=_data("2026-08-01"), Valor=_numero(5), Tipo=_select("Outro")),
    ]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    stats = NotionStats(notion_settings, _mapa_completo())
    await stats.monthly(hoje=HOJE)

    campos = {a.campo for a in stats.avisos}
    assert campos == {"financeiro.data", "financeiro.valor", "financeiro.tipo"}


async def test_monthly_ignora_linha_sem_tipo_mas_avisa(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regra do Web, conferida em dados reais: sem Tipo, a linha nao conta.

    Mas nao pode sumir calada de um total financeiro - por isso vira aviso.
    """
    linhas = [
        _pagina(Data=_data("2026-08-01"), Valor=_numero(100), Tipo=_select("Receita")),
        _pagina(Data=_data("2026-08-02"), Valor=_numero(64), Tipo=_select(None)),
    ]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    stats = NotionStats(notion_settings, _mapa_completo())
    saida = await stats.monthly(hoje=HOJE)
    agosto = next(m for m in saida if m["month"] == "2026-08")

    assert (agosto["income"], agosto["expense"]) == (100.0, 0.0)
    assert any(a.campo == "financeiro.tipo_vazio" and a.linhas == 1 for a in stats.avisos)


async def test_valor_negativo_e_estorno_nao_gasto_a_mais(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Um "Despesa" de -200 e dinheiro que voltou: tem que REDUZIR a despesa.

    Aplicar abs() aqui somava 200 em vez de subtrair - erro de 400 no total,
    que foi exatamente a divergencia encontrada contra o Web.
    """
    linhas = [
        _pagina(Data=_data("2026-08-01"), Valor=_numero(500), Tipo=_select("Despesa")),
        _pagina(Data=_data("2026-08-12"), Valor=_numero(-200), Tipo=_select("Despesa")),
    ]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    saida = await NotionStats(notion_settings, _mapa_completo()).monthly(hoje=HOJE)
    agosto = next(m for m in saida if m["month"] == "2026-08")
    assert agosto["expense"] == 300.0
    assert agosto["balance"] == -300.0


async def test_reproduz_agosto_de_2026_como_o_web(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regressao com os dados reais que expuseram as duas regras erradas."""
    reais = [
        (658.75, "Receita"), (110.00, "Despesa"), (30.00, "Despesa"),
        (85.90, "Despesa"), (118.23, "Despesa"), (25.00, "Despesa"),
        (55.00, "Despesa"), (-200.00, "Despesa"), (300.00, "Receita"),
        (64.00, None),
    ]
    linhas = [
        _pagina(Data=_data("2026-08-05"), Valor=_numero(v), Tipo=_select(t))
        for v, t in reais
    ]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    saida = await NotionStats(notion_settings, _mapa_completo()).monthly(hoje=HOJE)
    agosto = next(m for m in saida if m["month"] == "2026-08")

    assert agosto["income"] == 958.75
    assert agosto["expense"] == 224.13
    assert agosto["balance"] == 734.62


async def test_mapa_incompleto_falha_claro(notion_settings: Settings) -> None:
    with pytest.raises(MapaIncompleto, match=r"financeiro\.database_id"):
        await NotionStats(notion_settings, NotionMap()).monthly(hoje=HOJE)


# ------------------------------------------------------ financeiro_semanal
async def test_semanal_usa_janela_rolante_nao_semana_civil(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regressao com os dois lancamentos que separaram as regras.

    2026-07-10 e 2026-07-16 caem em SEMANAS CIVIS diferentes e na MESMA janela
    rolante de 7 dias. O Web soma os dois no mesmo balde; foi assim que a regra
    civil se denunciou, partindo 177 em 157 + 20.
    """
    linhas = [
        _pagina(Data=_data("2026-07-10"), Valor=_numero(157), Tipo=_select("Despesa")),
        _pagina(Data=_data("2026-07-16"), Valor=_numero(20), Tipo=_select("Despesa")),
    ]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    saida = await NotionStats(notion_settings, _mapa_completo()).finance_weekly(hoje=HOJE)
    por_balde = {b["month"]: b for b in saida}

    # hoje=2026-08-12: 10/07 fica 33 dias atras e 16/07, 27 -> baldes 4 e 3.
    assert por_balde["w-4"]["expense"] == 157.0
    assert por_balde["w-3"]["expense"] == 20.0


async def test_semanal_rotula_o_balde_atual(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": [], "has_more": False}))
    saida = await NotionStats(notion_settings, _mapa_completo()).finance_weekly(hoje=HOJE)

    assert [b["month"] for b in saida] == [f"w-{i}" for i in range(7, -1, -1)]
    assert saida[-1]["label"] == "Essa sem."
    assert saida[0]["label"] == "-7 sem."


async def test_taxa_de_poupanca_e_nula_sem_receita(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sem receita, poupanca nao e 0% - e indefinida. O Web devolve null."""
    linhas = [_pagina(Data=_data("2026-08-01"), Valor=_numero(50), Tipo=_select("Despesa"))]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    saida = await NotionStats(notion_settings, _mapa_completo()).savings_rate(hoje=HOJE)
    assert saida["currentRate"] is None
    assert all(m["rate"] is None for m in saida["trend"])


async def test_previsao_usa_tres_meses_anteriores(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEDIDO: o Web divide por 3, contando meses zerados no divisor."""
    monkeypatch.setenv("OPTMUS_NOTION_MONTHS_WINDOW", "6")
    reset_settings_cache()
    linhas = [_pagina(Data=_data("2026-07-15"), Valor=_numero(62.58), Tipo=_select("Receita"))]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    saida = await NotionStats(get_settings(), _mapa_completo()).forecast(hoje=HOJE)
    assert saida["historicalAverageNet"] == 20.86  # 62.58 / 3, nao / 5
    assert saida["monthLabel"] == "Ago/2026"  # ano com 4 digitos, ao contrario do mensal


async def test_alertas_ignoram_item_concluido(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEDIDO: o Web nao alerta sobre o que ja foi concluido."""
    linhas = [
        _pagina(
            Nome=_titulo("ingles"),
            Data=_data("2026-08-12"),
            Materia=_select("ingles"),
            Status=_select("Concluído"),
        ),
        _pagina(
            Nome=_titulo("matematiica"),
            Data=_data("2026-08-11"),
            Materia=_select("matematica"),
            Status=_select("Pendente"),
        ),
    ]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    saida = await NotionStats(notion_settings, _mapa_completo()).progress_alerts(hoje=HOJE)
    assert [a["titulo"] for a in saida] == ["matematiica"]


async def test_treino_futuro_nao_conta_em_nenhuma_das_duas_series(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEDIDO: treino planejado para o futuro nao entra.

    E as duas series precisam concordar entre si: o mensal contava e o semanal
    descartava por acidente (indice negativo caindo fora do range).
    """
    futuro = (HOJE + timedelta(days=2)).isoformat()
    linhas = [_pagina(Data=_data(futuro), Status=_select("Planejado"))]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    stats = NotionStats(notion_settings, _mapa_completo())
    assert sum(b["quantidade"] for b in await stats.workout_monthly(hoje=HOJE)) == 0
    assert sum(b["quantidade"] for b in await stats.workout_frequency(hoje=HOJE)) == 0


async def test_treino_passado_conta_nas_duas_series(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    ontem = (HOJE - timedelta(days=1)).isoformat()
    linhas = [_pagina(Data=_data(ontem), Status=_select("Concluído"))]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    stats = NotionStats(notion_settings, _mapa_completo())
    assert sum(b["quantidade"] for b in await stats.workout_monthly(hoje=HOJE)) == 1
    assert sum(b["quantidade"] for b in await stats.workout_frequency(hoje=HOJE)) == 1


async def test_treino_passado_mas_planejado_nao_conta(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O teste que separou as duas hipoteses.

    Enquanto o unico treino era futuro E planejado, "so conta data passada" e
    "so conta concluido" explicavam o mesmo zero. Com data passada e status
    "Planejado" o Web continuou em zero: e o status que manda.
    """
    ontem = (HOJE - timedelta(days=1)).isoformat()
    linhas = [_pagina(Data=_data(ontem), Status=_select("Planejado"))]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    stats = NotionStats(notion_settings, _mapa_completo())
    assert sum(b["quantidade"] for b in await stats.workout_monthly(hoje=HOJE)) == 0
    assert sum(b["quantidade"] for b in await stats.workout_frequency(hoje=HOJE)) == 0
    assert any(a.campo == "treino.nao_realizado" for a in stats.avisos), (
        "um treino que some da contagem precisa dizer por que"
    )


async def test_intervalo_de_data_vai_como_and_de_duas_condicoes(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Um objeto ``date`` com duas chaves NAO e intervalo para o Notion.

    Ele aceita, responde 200 e aplica so uma delas. Foi assim que uma prova de
    29/07 entrou no relatorio de agosto. O mock devolve tudo independentemente
    do filtro, entao o unico jeito de travar isso e conferir o que foi enviado.
    """
    import json as _json

    vistas = _mock(
        monkeypatch, lambda r: httpx.Response(200, json={"results": [], "has_more": False})
    )
    await NotionStats(notion_settings, _mapa_completo()).study_month(hoje=HOJE)

    filtro = _json.loads(vistas[0].content)["filter"]
    assert "and" in filtro, "intervalo precisa de duas condicoes, nao de duas chaves"
    limites = dict(item for c in filtro["and"] for item in c["date"].items())
    assert limites == {"on_or_after": "2026-08-01", "on_or_before": "2026-08-31"}


async def test_study_month_recorta_por_mes_e_mantem_concluido(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nao e o alerta com outra roupa.

    O alerta esconde concluido e usa janela de +-30 dias; aqui o concluido
    aparece e o corte e o mes civil. Sem ``Tipo``, o PDF do Web escreve "Outro".
    """
    linhas = [
        _pagina(
            Nome=_titulo("ingles"),
            Data=_data("2026-08-13"),
            Materia=_select("ingles"),
            Tipo=_select("Prova"),
            Status=_select("Concluído"),
        ),
        _pagina(
            Nome=_titulo("Prova CALCULO 2"),
            Data=_data("2026-08-10"),
            Materia=_select("calculo"),
            Tipo=_select(None),
            Status=_select("Em andamento"),
        ),
    ]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    saida = await NotionStats(notion_settings, _mapa_completo()).study_month(hoje=HOJE)

    assert [i["titulo"] for i in saida] == ["Prova CALCULO 2", "ingles"], "ordem por data"
    assert saida[0]["tipo"] == "Outro", "linha sem Tipo sai como Outro, nao em branco"
    assert saida[1]["status"] == "Concluído", "concluido aparece aqui, ao contrario do alerta"


async def test_tasks_conta_o_historico_inteiro(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEDIDO: o Web nao recorta as concluidas por mes - sao 5 no total.

    E ``porPrioridade`` veio vazio com quatro tarefas priorizadas, o que so se
    explica se apenas pendente entrar nesse bloco.
    """
    linhas = [
        _pagina(
            Tarefa=_titulo(f"t{i}"),
            Status=_select("Concluído"),
            Prioridade=_select("🔥 Muito alta"),
        )
        for i in range(5)
    ]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    saida = await NotionStats(notion_settings, _mapa_completo()).tasks()

    assert saida["totalConcluidas"] == 5
    assert saida["totalPendentes"] == 0
    assert saida["totalAtrasadas"] == 0
    assert saida["porPrioridade"] == [], "tarefa concluida nao entra em porPrioridade"
    assert [b["label"] for b in saida["atraso"]] == [
        "Sem prazo", "No prazo", "1-7 dias", "8-30 dias", "30+ dias",
    ]


def test_ultimo_dia_de_dezembro_nao_estoura(
    ) -> None:
    """date(ano, 13, 1) levanta ValueError - o caminho ingenuo quebrava em dezembro."""
    from integrations.notion_stats import _ultimo_dia

    assert _ultimo_dia(date(2026, 12, 3)) == date(2026, 12, 31)
    assert _ultimo_dia(date(2026, 2, 3)) == date(2026, 2, 28)
    assert _ultimo_dia(date(2028, 2, 3)) == date(2028, 2, 29), "ano bissexto"


async def test_grades_nao_le_a_base_estudos(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A base Estudos tem coluna Nota, mas NAO e a fonte do Web.

    Medido antes de a base de notas ser compartilhada: com a nota preenchida em
    Estudos, o Web seguia devolvendo ``disciplinas: []``. Sem
    ``notas_trimestre`` configurado o indicador se cala, em vez de inventar
    disciplina a partir da base errada.
    """
    linhas = [
        _pagina(
            Nome=_titulo("ingles"),
            Data=_data("2026-08-12"),
            Materia=_select("ingles"),
            Nota=_numero(5.0),
        )
    ]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    mapa = _mapa_completo()
    mapa.notas_trimestre = MapaNotasTrimestre()  # sem database_id
    stats = NotionStats(notion_settings, mapa)
    saida = await stats.grades()
    assert saida["disciplinas"] == []
    assert any(a.campo == "notas_escolares.fonte" for a in stats.avisos)


async def test_grades_deriva_os_numeros_da_base_de_notas(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Numeros conferidos contra o Web para 18 pontos no 1o trimestre."""
    linhas = [
        _pagina(
            Disciplina=_titulo("matematica"),
            Trimestre=_select("1º trimestre"),
            Pontos=_numero(18),
        )
    ]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    saida = await NotionStats(notion_settings, _mapa_completo()).grades()
    (disciplina,) = saida["disciplinas"]

    assert disciplina["disciplina"] == "matematica"
    assert disciplina["obtido"] == 18
    assert disciplina["restante"] == 70, "soma o total dos trimestres SEM nota"
    assert disciplina["maximoPossivel"] == 88
    assert disciplina["precisa"] == 42, "minimo de aprovacao menos o obtido"
    assert disciplina["situacao"] == "em andamento"
    assert [t["pontos"] for t in disciplina["trimestres"]] == [18, None, None]


async def test_grades_reprova_quando_o_maximo_nao_alcanca(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRESUMIDO, nao medido: nenhuma disciplina reprovada foi vista no Web.

    O teste trava o comportamento que eu escolhi para nao mudar sem querer -
    nao prova que o Web faz igual.
    """
    linhas = [
        _pagina(
            Disciplina=_titulo("fisica"), Trimestre=_select("1º trimestre"), Pontos=_numero(0)
        ),
        _pagina(
            Disciplina=_titulo("fisica"), Trimestre=_select("2º trimestre"), Pontos=_numero(0)
        ),
    ]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    saida = await NotionStats(notion_settings, _mapa_completo()).grades()
    # Sobra so o 3o trimestre (40 pontos), abaixo do minimo de 60.
    assert saida["disciplinas"][0]["situacao"] == "reprovado"


async def test_sono_deriva_data_do_titulo(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A base nao tem coluna de data: "Noite" e o titulo, no formato DD/MM.

    Uma das linhas reais vem com espaco na frente (" 09/08") - o Web devolve o
    label ja aparado, entao aparar nao e cosmetica.
    """
    linhas = [
        _pagina(Noite=_titulo("08/08"), Horas=_numero(8), Qualidade=_select("😊 Bom")),
        _pagina(Noite=_titulo(" 09/08"), Horas=_numero(5), Qualidade=_select("😕 Ruim")),
        _pagina(Noite=_titulo(" "), Horas=None, Qualidade=_select(None)),
    ]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    saida = await NotionStats(notion_settings, _mapa_completo()).sleep()

    assert saida["totalLinhas"] == 3, "conta TODAS as linhas, inclusive as vazias"
    assert saida["noitesRegistradas"] == 2, "so as que tem horas"
    assert saida["mediaHoras"] == 6.5
    assert saida["noitesNaMeta"] == 1, "so a de 8h alcanca a meta de 8h"
    assert saida["ultimasNoites"][1]["label"] == "09/08"
    assert saida["ultimasNoites"][1]["data"].endswith("-08-09")
    assert saida["melhorNoite"]["horas"] == 8
    assert saida["piorNoite"]["horas"] == 5


async def test_sono_separa_noite_vazia_de_formato_estranho(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sao problemas diferentes, e so um deles esta confirmado contra o Web.

    Noite vazia: o Web tambem descarta (4 linhas reais assim, e os totais
    bateram). Noite preenchida com outro formato: nunca vi acontecer, entao o
    aviso precisa dizer que essa parte nao foi conferida.
    """
    linhas = [
        _pagina(Noite=_titulo("  "), Horas=_numero(8), Qualidade=_select("😐 Razoável")),
        _pagina(Noite=_titulo("ontem"), Horas=_numero(7), Qualidade=_select("😊 Bom")),
        _pagina(Noite=_titulo("32/13"), Horas=_numero(7), Qualidade=_select("😊 Bom")),
    ]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    stats = NotionStats(notion_settings, _mapa_completo())
    saida = await stats.sleep()
    assert saida["noitesRegistradas"] == 0
    assert saida["mediaHoras"] == 0, "sem noite valida nao ha media - e nao ha divisao por zero"
    assert saida["melhorNoite"] is None
    por_campo = {a.campo: a.linhas for a in stats.avisos}
    assert por_campo["sono.sem_noite"] == 1
    assert por_campo["sono.formato"] == 2


async def test_aviso_repetido_aparece_uma_vez_so(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """monthly() roda tres vezes por relatorio; um achado nao vira tres."""
    linhas = [_pagina(Data=_data("2026-08-01"), Valor=_numero(64), Tipo=_select(None))]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    stats = NotionStats(notion_settings, _mapa_completo())
    await stats.monthly(hoje=HOJE)
    await stats.savings_rate(hoje=HOJE)
    await stats.forecast(hoje=HOJE)

    vazios = [a for a in stats.avisos if a.campo == "financeiro.tipo_vazio"]
    assert len(vazios) == 1
    assert vazios[0].linhas == 1


# -------------------------------------------------------------- work_tasks
async def test_work_tasks_agrupa_por_tipo(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    linhas = [
        _pagina(Tipo=_select("Empresa"), Status=_select("Ativo")),
        _pagina(Tipo=_select("Empresa"), Status=_select("Concluido")),
        _pagina(Tipo=_select("Freela"), Status=_select("Ativo")),
    ]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    saida = await NotionStats(notion_settings, _mapa_completo()).work_tasks()
    assert saida == [
        {"tipo": "Empresa", "total": 2, "ativo": 1, "concluido": 1},
        {"tipo": "Freela", "total": 1, "ativo": 1, "concluido": 0},
    ]


async def test_work_tasks_aceita_status_por_checkbox(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    linhas = [
        _pagina(Tipo=_select("Empresa"), Status={"type": "checkbox", "checkbox": True}),
        _pagina(Tipo=_select("Empresa"), Status={"type": "checkbox", "checkbox": False}),
    ]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    saida = await NotionStats(notion_settings, _mapa_completo()).work_tasks()
    assert saida == [{"tipo": "Empresa", "total": 2, "ativo": 1, "concluido": 1}]


# ---------------------------------------------------------- progress_alerts
async def test_alertas_calculam_dias_restantes(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    linhas = [
        _pagina(
            Nome=_titulo("Prova CALCULO 2"),
            Data=_data("2026-08-10"),
            Materia=_select("calculo"),
        ),
        _pagina(Nome=_titulo("Entrega TCC"), Data=_data("2026-08-20"), Materia=_select("tcc")),
    ]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    saida = await NotionStats(notion_settings, _mapa_completo()).progress_alerts(hoje=HOJE)
    assert saida[0] == {
        "tipo": "estudo",
        "titulo": "Prova CALCULO 2",
        "detalhe": "calculo",
        "data": "2026-08-10",
        "diasRestantes": -2,
    }
    assert saida[1]["diasRestantes"] == 8


async def test_diagnostico_diz_por_que_cada_prazo_entra_ou_nao(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O caso real: item um dia fora da janela parece confirmacao e nao e."""
    linhas = [
        _pagina(Nome=_titulo("dentro"), Data=_data("2026-09-11"), Materia=_select("x")),
        _pagina(Nome=_titulo("fora por um dia"), Data=_data("2026-09-12"), Materia=_select("x")),
    ]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    info = await diagnosticar_alertas(
        NotionStats(notion_settings, _mapa_completo()), hoje=HOJE
    )

    assert info["ultima_data_que_entra"] == "2026-09-11"
    assert info["primeira_data_que_fica_de_fora"] == "2026-09-12"
    por_titulo = {linha["titulo"]: linha for linha in info["linhas"]}
    assert por_titulo["dentro"]["no_core"] is True
    assert por_titulo["fora por um dia"]["no_core"] is False
    assert "1 dia(s) depois do limite" in por_titulo["fora por um dia"]["motivo"]


@pytest.mark.parametrize(
    ("dias", "data", "entra"),
    [
        (-30, "2026-07-13", True),   # sem piso configurado, passado entra
        (30, "2026-09-11", True),    # exatamente o limite
        (31, "2026-09-12", False),   # um dia depois - o caso que enganou
    ],
)
async def test_diagnostico_converte_dias_em_data_literal(
    notion_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    dias: int,
    data: str,
    entra: bool,
) -> None:
    """Elimina a aritmetica de data do laco de teste.

    A base PRECISA ter linhas: a versao anterior deste teste usava base vazia,
    e por isso passou por cima de um bug real - o loop que percorre as linhas
    reatribuia a variavel 'dias', destruindo o valor pedido. Com base vazia o
    loop nao roda e o bug fica invisivel.
    """
    linhas = [
        _pagina(Nome=_titulo("prova antiga"), Data=_data("2026-08-10"), Materia=_select("x")),
        _pagina(Nome=_titulo("prova futura"), Data=_data("2026-09-30"), Materia=_select("x")),
    ]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    info = await diagnosticar_alertas(
        NotionStats(notion_settings, _mapa_completo()), hoje=HOJE, dias=dias
    )
    assert info["data_para_testar"] == {
        "dias": dias,
        "data": data,
        "entraria_no_core": entra,
    }
    # As linhas seguem com o proprio calculo de dias, cada uma com o seu.
    assert {linha["dias"] for linha in info["linhas"]} == {-2, 49}


async def test_diagnostico_aponta_data_ilegivel(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    linhas = [_pagina(Nome=_titulo("sem data"), Data=_data(None), Materia=_select("x"))]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    info = await diagnosticar_alertas(
        NotionStats(notion_settings, _mapa_completo()), hoje=HOJE
    )
    assert info["linhas"][0]["no_core"] is False
    assert "vazia ou de outro tipo" in info["linhas"][0]["motivo"]


async def test_alertas_sem_piso_pedem_tudo_que_ja_passou(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Padrao atual: sem limite para tras. Nao conferido contra o Web."""
    vistas = _mock(
        monkeypatch, lambda r: httpx.Response(200, json={"results": [], "has_more": False})
    )
    await NotionStats(notion_settings, _mapa_completo()).progress_alerts(hoje=HOJE)

    import json as _json

    filtro = _json.loads(vistas[0].content)["filter"]["date"]
    assert filtro == {"on_or_before": "2026-09-11"}


async def test_piso_configurado_limita_o_passado(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPTMUS_NOTION_ALERT_PAST_DAYS", "7")
    reset_settings_cache()
    vistas = _mock(
        monkeypatch, lambda r: httpx.Response(200, json={"results": [], "has_more": False})
    )
    await NotionStats(get_settings(), _mapa_completo()).progress_alerts(hoje=HOJE)

    import json as _json

    filtro = _json.loads(vistas[0].content)["filter"]["date"]
    # 'after' e nao 'on_or_after': a borda de tras e exclusiva.
    assert filtro == {"on_or_before": "2026-09-11", "after": "2026-08-05"}


@pytest.mark.parametrize(
    ("dias", "entra"),
    [
        (30, True),    # medido: +30 aparece no Web
        (31, False),   # medido: +41 nao aparece; +31 ja e fora
        (-29, True),   # medido: -27, -28 e -29 batem nos dois lados
        (-30, False),  # medido: -30 diverge (so_no_notion) -> Web exclui
    ],
)
async def test_bordas_da_janela_reproduzem_o_web(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch, dias: int, entra: bool
) -> None:
    """As quatro bordas medidas contra o Optmus Web, com PAST_DAYS=30.

    A janela do Web e +-30 dias calculada em TIMESTAMP. Comparando por data,
    isso vira frente inclusiva e tras exclusiva - por isso -30 fica de fora e
    +30 fica dentro, com o mesmo numero 30 nos dois lados da configuracao.
    """
    monkeypatch.setenv("OPTMUS_NOTION_ALERT_PAST_DAYS", "30")
    reset_settings_cache()
    settings = get_settings()

    alvo = (HOJE + timedelta(days=dias)).isoformat()
    linhas = [_pagina(Nome=_titulo("alvo"), Data=_data(alvo), Materia=_select("x"))]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": linhas, "has_more": False}))

    info = await diagnosticar_alertas(
        NotionStats(settings, _mapa_completo()), hoje=HOJE, dias=dias
    )
    assert info["data_para_testar"]["entraria_no_core"] is entra
    assert info["linhas"][0]["no_core"] is entra


async def test_filtro_pedido_ao_notion_com_piso_de_30(
    notion_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPTMUS_NOTION_ALERT_PAST_DAYS", "30")
    reset_settings_cache()
    vistas = _mock(
        monkeypatch, lambda r: httpx.Response(200, json={"results": [], "has_more": False})
    )
    await NotionStats(get_settings(), _mapa_completo()).progress_alerts(hoje=HOJE)

    import json as _json

    filtro = _json.loads(vistas[0].content)["filter"]["date"]
    assert filtro == {"on_or_before": "2026-09-11", "after": "2026-07-13"}


# ------------------------------------------------------------- reconciliacao
def test_comparacao_detecta_divergencia_de_valor() -> None:
    web = [{"month": "2026-08", "income": 1000.0, "expense": 250.0}]
    notion = [{"month": "2026-08", "income": 1000.0, "expense": 300.0}]

    r = comparar_listas(
        "financeiro_mensal", web, notion, chave="month", campos=("income", "expense")
    )
    assert not r.equivalente
    assert len(r.divergencias) == 1
    assert (r.divergencias[0].web, r.divergencias[0].notion) == (250.0, 300.0)


def test_comparacao_tolera_ruido_de_ponto_flutuante() -> None:
    web = [{"month": "2026-08", "income": 858.75}]
    notion = [{"month": "2026-08", "income": 858.7500000001}]
    assert comparar_listas("x", web, notion, chave="month", campos=("income",)).equivalente


def test_diferenca_de_um_centavo_e_reportada() -> None:
    """Tolerancia de um centavo engoliria justamente o erro que se procura."""
    web = [{"month": "2026-08", "income": 100.00}]
    notion = [{"month": "2026-08", "income": 100.01}]
    assert not comparar_listas("x", web, notion, chave="month", campos=("income",)).equivalente


def test_comparacao_aponta_registro_so_de_um_lado() -> None:
    web = [{"tipo": "Empresa", "total": 1}, {"tipo": "Freela", "total": 2}]
    notion = [{"tipo": "Empresa", "total": 1}]

    r = comparar_listas("trabalho", web, notion, chave="tipo", campos=("total",))
    assert r.so_no_web == ["Freela"]
    assert not r.equivalente


def test_dois_lados_vazios_nao_contam_como_equivalencia() -> None:
    """Ausencia de evidencia nao e evidencia.

    Duas listas vazias nao divergem - e nao provam nada. Contar isso como
    "bateu" e o jeito mais silencioso de aprovar o desligamento da outra fonte
    sem ter testado coisa alguma.
    """
    r = comparar_listas("alertas", [], [], chave="titulo", campos=("data",))
    assert r.comparados == 0
    assert r.sem_dados is True
    assert r.equivalente is False


def test_uma_divergencia_ja_reprova() -> None:
    """Nao existe "quase igual" quando o passo seguinte e apagar a outra fonte."""
    web = [{"month": "2026-08", "income": 100.0}, {"month": "2026-07", "income": 50.0}]
    notion = [{"month": "2026-08", "income": 100.0}, {"month": "2026-07", "income": 50.01}]
    r = comparar_listas("x", web, notion, chave="month", campos=("income",))
    assert r.comparados == 2
    assert r.equivalente is False


class _WebFalso:
    def __init__(self, dados: dict[str, Any]) -> None:
        self._dados = dados

    async def indicador(self, nome: str) -> Any:
        return self._dados[nome]


class _StatsFalso:
    """Dubles das 12 agregacoes, cada uma lendo a chave correspondente."""

    METODOS: ClassVar[dict[str, str]] = {
        "monthly": "financeiro_mensal",
        "work_tasks": "trabalho",
        "progress_alerts": "alertas",
        "category_spending": "gastos_por_categoria",
        "finance_weekly": "financeiro_semanal",
        "workout_frequency": "treino_frequencia",
        "workout_monthly": "treino_mensal",
        "savings_rate": "taxa_de_poupanca",
        "forecast": "previsao_financeira",
        "study": "estudos",
        "grades": "notas_escolares",
        "sleep": "sono",
        "tasks": "tarefas",
    }

    def __init__(self, dados: dict[str, Any]) -> None:
        self._dados = dados
        self.avisos: list[Any] = []

    def __getattr__(self, nome: str) -> Any:
        if nome not in self.METODOS:
            raise AttributeError(nome)

        async def _ler() -> Any:
            return self._dados[self.METODOS[nome]]

        return _ler


IGUAIS: dict[str, Any] = {
    "financeiro_mensal": [
        {
            "month": "2026-08",
            "label": "Ago/26",
            "income": 1000.0,
            "expense": 250.0,
            "balance": 750.0,
        }
    ],
    "trabalho": [{"tipo": "Empresa", "total": 1, "ativo": 1, "concluido": 0}],
    "alertas": [
        {
            "tipo": "estudo",
            "titulo": "Prova",
            "detalhe": "calculo",
            "data": "2026-08-10",
            "diasRestantes": -2,
        }
    ],
    "gastos_por_categoria": [{"categoria": "Saude", "total": 173.23, "count": 2}],
    "financeiro_semanal": [
        {"month": "w-0", "label": "Essa sem.", "income": 300.0, "expense": -200.0, "balance": 500.0}
    ],
    "treino_frequencia": [{"label": "Essa sem.", "quantidade": 0}],
    "treino_mensal": [{"label": "Ago/26", "quantidade": 0}],
    "taxa_de_poupanca": {
        "currentRate": 75.0,
        "incomeSoFar": 1000.0,
        "expenseSoFar": 250.0,
        "trend": [{"month": "2026-08", "label": "Ago/26", "rate": 75.0}],
    },
    "previsao_financeira": {
        "monthLabel": "Ago/2026",
        "daysElapsed": 13,
        "daysInMonth": 31,
        "incomeSoFar": 1000.0,
        "expenseSoFar": 250.0,
        "netSoFar": 750.0,
        "projectedNet": 1788.46,
        "historicalAverageNet": 20.86,
    },
    "estudos": {
        "porStatus": [{"status": "Pendente", "quantidade": 1}],
        "proximoPrazo": {
            "titulo": "Prova",
            "disciplina": "calculo",
            "tipo": "",
            "data": "2026-08-10",
        },
    },
    "notas_escolares": {
        # Uma disciplina de verdade: com a lista vazia, a comparacao aninhada
        # nao era exercitada e passava por ausencia.
        "disciplinas": [
            {
                "disciplina": "matematica",
                "trimestres": [
                    {"numero": 1, "nome": "1º trimestre", "total": 30, "minimo": 18, "pontos": 18}
                ],
                "obtido": 18,
                "restante": 70,
                "precisa": 42,
                "situacao": "em andamento",
                "maximoPossivel": 88,
            }
        ],
        "totalAno": 100.0,
        "minimoAprovacao": 60.0,
        "trimestres": [{"numero": 1, "nome": "1º trimestre", "total": 30, "minimo": 18}],
    },
    "tarefas": {
        "totalPendentes": 0,
        "totalConcluidas": 5,
        "totalAtrasadas": 0,
        "porPrioridade": [],
        "atraso": [
            {"label": "Sem prazo", "quantidade": 0},
            {"label": "No prazo", "quantidade": 0},
            {"label": "1-7 dias", "quantidade": 0},
            {"label": "8-30 dias", "quantidade": 0},
            {"label": "30+ dias", "quantidade": 0},
        ],
        "atrasadas": [],
    },
    "sono": {
        "ultimasNoites": [
            {"label": "08/08", "data": "2026-08-08", "horas": 8, "qualidade": "😊 Bom"}
        ],
        "mediaHoras": 8,
        "noitesRegistradas": 1,
        "totalLinhas": 91,
        "porQualidade": [{"qualidade": "😊 Bom", "quantidade": 1}],
        "melhorNoite": {"label": "08/08", "data": "2026-08-08", "horas": 8, "qualidade": "😊 Bom"},
        "piorNoite": {"label": "08/08", "data": "2026-08-08", "horas": 8, "qualidade": "😊 Bom"},
        "noitesNaMeta": 1,
        "metaHoras": 8,
    },
}


async def test_relatorio_aprova_quando_tudo_bate() -> None:
    relatorio = await conferir(web_client=_WebFalso(IGUAIS), stats=_StatsFalso(IGUAIS))
    assert relatorio.equivalente
    assert "batem" in relatorio.to_dict()["veredito"]


async def test_relatorio_reprova_e_mostra_os_dois_valores() -> None:
    divergente = {
        **IGUAIS,
        "trabalho": [{"tipo": "Empresa", "total": 5, "ativo": 1, "concluido": 0}],
    }
    relatorio = await conferir(web_client=_WebFalso(IGUAIS), stats=_StatsFalso(divergente))

    assert not relatorio.equivalente
    veredito = relatorio.to_dict()["veredito"]
    assert "1 divergencia(s) de valor" in veredito
    assert "nao pode ser desligado" in veredito
    divergencia = next(
        d for i in relatorio.indicadores for d in i.divergencias if i.indicador == "trabalho"
    )
    assert (divergencia.web, divergencia.notion) == (1, 5)


async def test_relatorio_reprova_quando_um_indicador_veio_vazio() -> None:
    vazio = {**IGUAIS, "alertas": []}
    relatorio = await conferir(web_client=_WebFalso(vazio), stats=_StatsFalso(vazio))

    assert not relatorio.equivalente
    assert "sem dado nenhum para comparar: alertas" in relatorio.veredito()


async def test_falha_de_um_indicador_nao_aborta_os_outros() -> None:
    class _MeioQuebrado(_StatsFalso):
        async def work_tasks(self) -> Any:
            raise RuntimeError("mapa do trabalho incompleto")

    relatorio = await conferir(web_client=_WebFalso(IGUAIS), stats=_MeioQuebrado(IGUAIS))
    por_nome = {i.indicador: i for i in relatorio.indicadores}

    assert por_nome["financeiro_mensal"].equivalente
    assert por_nome["trabalho"].erro is not None
    assert not relatorio.equivalente
    # Veredito precisa dizer que NAO comparou, nao "0 divergencias".
    assert "nao consegui comparar: trabalho" in relatorio.veredito()


# ---------------------------------------------------------------- mapa
def test_mapa_lista_o_que_falta() -> None:
    pendentes = NotionMap().pendencias()
    assert "financeiro.database_id" in pendentes
    assert "estudos.database_id" in pendentes
    assert "treino.database_id" in pendentes
    assert _mapa_completo().completo


async def test_relatorio_cobre_todos_os_indicadores() -> None:
    """Se um indicador novo nao entrar na conferencia, ele nunca e comparado."""
    relatorio = await conferir(web_client=_WebFalso(IGUAIS), stats=_StatsFalso(IGUAIS))
    nomes = {i.indicador for i in relatorio.indicadores}
    assert nomes == set(METODOS_NOTION), (
        "a conferencia e o endpoint /notion/stats precisam cobrir o mesmo conjunto"
    )
    assert relatorio.equivalente


async def test_divergencia_dentro_de_objeto_aninhado_aparece() -> None:
    """melhorNoite e os trimestres de uma disciplina sao objetos aninhados.

    Comparados como texto, a diferenca podia sumir ou virar falso positivo por
    ordem de chave - por isso a comparacao desce nos dois.
    """
    divergente = {
        **IGUAIS,
        "sono": {
            **IGUAIS["sono"],
            "melhorNoite": {**IGUAIS["sono"]["melhorNoite"], "horas": 9},
        },
    }
    relatorio = await conferir(web_client=_WebFalso(IGUAIS), stats=_StatsFalso(divergente))
    sono = next(i for i in relatorio.indicadores if i.indicador == "sono")
    assert [d.campo for d in sono.divergencias] == ["melhorNoite"]


def test_ordem_das_chaves_nao_vira_divergencia() -> None:
    """O JSON do Web nao garante a mesma ordem de chave que a minha montagem."""
    resultado = comparar_objetos(
        "x",
        {"n": {"a": 1, "b": 2}},
        {"n": {"b": 2, "a": 1}},
        escalares=("n",),
    )
    assert resultado.equivalente


def test_mapa_carrega_de_dict() -> None:
    mapa = de_dict(
        {
            "financeiro": {"database_id": "abc", "valor": "Preco"},
            "prazos": [{"database_id": "xyz", "rotulo": "prova"}],
        }
    )
    assert mapa.financeiro.database_id == "abc"
    assert mapa.financeiro.valor == "Preco"
    assert mapa.prazos[0].rotulo == "prova"
