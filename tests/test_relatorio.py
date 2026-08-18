"""Relatorio mensal: os numeros e o PDF.

A divisao dos testes espelha a divisao do modulo. ``montar_dados`` responde
pelos numeros e e testado com numeros; ``gerar_pdf`` responde pelo desenho e e
testado extraindo o texto de volta. Misturar os dois daria o pior dos mundos:
teste de conta que quebra por quebra de linha.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from reports.mensal import (
    DadosRelatorio,
    gerar_pdf,
    horas,
    moeda,
    montar_dados,
    sem_emoji,
)

HOJE = date(2026, 8, 13)


class _StatsFalso:
    """Devolve exatamente o que as agregacoes reais devolveriam hoje."""

    def __init__(self, **trocas: Any) -> None:
        self.avisos: list[Any] = []
        self._dados: dict[str, Any] = {
            "monthly": [
                {"month": "2026-06", "label": "Jun/26", "income": 0.0, "expense": 0.0,
                 "balance": 0.0},
                {"month": "2026-07", "label": "Jul/26", "income": 10.0, "expense": 5.0,
                 "balance": 5.0},
                {"month": "2026-08", "label": "Ago/26", "income": 958.75, "expense": 224.13,
                 "balance": 734.62},
            ],
            "savings_rate": {"currentRate": 76.62, "incomeSoFar": 958.75,
                             "expenseSoFar": 224.13, "trend": []},
            "category_spending": [{"categoria": "Saúde", "total": 173.23, "count": 2}],
            "tasks": {"totalPendentes": 0, "totalConcluidas": 5, "totalAtrasadas": 0},
            "work_tasks": [
                {"tipo": "Empresa", "total": 1, "ativo": 1, "concluido": 0},
                {"tipo": "pessoal", "total": 1, "ativo": 1, "concluido": 0},
            ],
            "grades": {
                "disciplinas": [
                    {
                        "disciplina": "matematica",
                        "trimestres": [
                            {"numero": 1, "nome": "1º", "total": 30, "minimo": 18, "pontos": 18},
                            {"numero": 2, "nome": "2º", "total": 30, "minimo": 18, "pontos": None},
                        ],
                        "obtido": 18, "restante": 30, "precisa": 42,
                        "situacao": "em andamento", "maximoPossivel": 48,
                    }
                ],
                "totalAno": 100.0,
                "minimoAprovacao": 60.0,
                "trimestres": [{"numero": 1, "nome": "1º", "total": 30, "minimo": 18}],
            },
            "study_month": [
                {"titulo": "ingles", "disciplina": "ingles", "tipo": "Prova",
                 "status": "Concluído", "data": "2026-08-13"}
            ],
            # Janela de 3 meses: o ultimo balde e o mes corrente.
            "workout_monthly": [
                {"label": "Jun/26", "quantidade": 7},
                {"label": "Jul/26", "quantidade": 3},
                {"label": "Ago/26", "quantidade": 0},
            ],
            "sleep": {
                "ultimasNoites": [
                    {"label": "08/08", "data": "2026-08-08", "horas": 8, "qualidade": "😊 Bom"},
                    {"label": "09/08", "data": "2026-08-09", "horas": 5, "qualidade": "😕 Ruim"},
                ],
                "mediaHoras": 6.5, "noitesRegistradas": 2, "totalLinhas": 91,
                "porQualidade": [], "noitesNaMeta": 1, "metaHoras": 8,
                "melhorNoite": {"label": "08/08", "horas": 8},
                "piorNoite": {"label": "09/08", "horas": 5},
            },
        }
        self._dados.update(trocas)

    def __getattr__(self, nome: str) -> Any:
        if nome.startswith("_") or nome not in self._dados:
            raise AttributeError(nome)

        async def _ler(**_: Any) -> Any:
            return self._dados[nome]

        return _ler


# ------------------------------------------------------------------- numeros
async def test_dados_saem_do_mes_corrente() -> None:
    dados = await montar_dados(_StatsFalso(), hoje=HOJE)

    assert dados.mes == "2026-08"
    assert dados.titulo == "Agosto de 2026"
    assert (dados.receita, dados.despesa, dados.saldo) == (958.75, 224.13, 734.62)


async def test_treino_pega_o_ultimo_balde_e_nao_o_primeiro() -> None:
    """Casar pelo rotulo pegava o mes ERRADO.

    "Ago/26" e "Jun/26" terminam ambos em "/26"; o primeiro que casava era o
    mais antigo da janela. Aqui isso daria 7 treinos em vez de 0.
    """
    dados = await montar_dados(_StatsFalso(), hoje=HOJE)
    assert dados.treinos_no_mes == 0


async def test_mes_sem_lancamento_nao_erra() -> None:
    """monthly() pode nao ter o mes corrente; zero e melhor que estourar."""
    dados = await montar_dados(_StatsFalso(monthly=[]), hoje=HOJE)
    assert (dados.receita, dados.saldo) == (0.0, 0.0)


async def test_trabalho_soma_os_tipos() -> None:
    dados = await montar_dados(_StatsFalso(), hoje=HOJE)
    assert (dados.trabalho_ativas, dados.trabalho_concluidas) == (2, 0)


async def test_noites_saem_da_mais_recente_para_a_mais_antiga() -> None:
    """sleep() devolve crescente; o PDF do Web lista ao contrario."""
    dados = await montar_dados(_StatsFalso(), hoje=HOJE)
    assert [n["label"] for n in dados.noites] == ["09/08", "08/08"]


# --------------------------------------------------------------- formatacao
@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        (734.62, "R$ 734,62"),
        (-175.0, "-R$ 175,00"),
        (0.0, "R$ 0,00"),
        (1234567.5, "R$ 1.234.567,50"),
        (85.9, "R$ 85,90"),
    ],
)
def test_moeda(valor: float, esperado: str) -> None:
    assert moeda(valor) == esperado


def test_horas_sem_casa_decimal_inutil() -> None:
    assert horas(6.5) == "6.5h"
    assert horas(8.0) == "8h"


def test_sem_emoji_mantem_a_palavra() -> None:
    """O Notion guarda "😊 Bom"; o PDF mostra "Bom" - e a fonte base nao tem
    glifo de emoji, entao deixar passar imprimiria um quadrado."""
    assert sem_emoji("😊 Bom") == "Bom"
    assert sem_emoji("🔥 Muito alta") == "Muito alta"
    assert sem_emoji("Bom") == "Bom"


# --------------------------------------------------------------------- pdf
def _texto(pdf: bytes, caminho: Any) -> str:
    """Texto do PDF com o espaco em branco normalizado.

    O extrator quebra linha onde o layout quebra e devolve espaco duplo onde o
    NBSP separa numero e rotulo. Nada disso e o que estes testes verificam - eles
    verificam conteudo -, e sem normalizar a assercao passa a depender do
    tamanho da fonte.
    """
    from pypdf import PdfReader

    caminho.write_bytes(pdf)
    bruto = "\n".join(p.extract_text() or "" for p in PdfReader(str(caminho)).pages)
    return " ".join(bruto.split())


async def test_pdf_carrega_todos_os_numeros(tmp_path: Any) -> None:
    """Um PDF valido com os numeros errados passaria num teste de tipo/tamanho."""
    dados = await montar_dados(_StatsFalso(), hoje=HOJE)
    pdf = gerar_pdf(dados)

    assert pdf.startswith(b"%PDF-")
    texto = _texto(pdf, tmp_path / "r.pdf")

    for esperado in (
        "Agosto de 2026",
        "R$ 734,62",
        "R$ 958,75",
        "R$ 224,13",
        "77%",  # 76,62 arredondado, como o Web faz
        "Concluídas",
        "matematica",
        "18/100",
        "ingles",
        "6.5h",
        "Bom",
    ):
        assert esperado in texto, f"sumiu do PDF: {esperado}"
    assert "😊" not in texto, "emoji nao chega ao papel"


async def test_pdf_arredonda_a_economia_como_o_web(tmp_path: Any) -> None:
    dados = await montar_dados(_StatsFalso(), hoje=HOJE)
    texto = _texto(gerar_pdf(dados), tmp_path / "r.pdf")
    assert "77%" in texto
    assert "76,62" not in texto and "76.62" not in texto


async def test_pdf_sem_dado_nenhum_ainda_sai(tmp_path: Any) -> None:
    """Mes vazio nao pode derrubar o relatorio - divisao por zero, lista vazia,
    melhorNoite None. E o caso de virada de mes, quando nada foi lancado."""
    vazio = _StatsFalso(
        monthly=[],
        category_spending=[],
        work_tasks=[],
        study_month=[],
        workout_monthly=[],
        grades={"disciplinas": [], "totalAno": 100.0, "minimoAprovacao": 60.0, "trimestres": []},
        savings_rate={"currentRate": None},
        sleep={
            "ultimasNoites": [], "mediaHoras": 0, "noitesRegistradas": 0, "totalLinhas": 0,
            "porQualidade": [], "noitesNaMeta": 0, "metaHoras": 8,
            "melhorNoite": None, "piorNoite": None,
        },
    )
    texto = _texto(gerar_pdf(await montar_dados(vazio, hoje=HOJE)), tmp_path / "r.pdf")
    assert "Agosto de 2026" in texto
    assert "Nada com data neste mês." in texto
    assert "—" in texto, "taxa sem receita aparece como travessao, nao como 0%"


def test_pdf_com_dados_zerados_nao_quebra_no_singular(tmp_path: Any) -> None:
    """1 noite, 1 treino, 1 lancamento: o plural e montado a mao em varios pontos."""
    dados = DadosRelatorio(
        mes="2026-08",
        titulo="Agosto de 2026",
        gerado_em="13/08/2026 às 23:00",
        categorias=[{"categoria": "Saúde", "total": 10.0, "count": 1}],
        treinos_no_mes=1,
        sono_noites=1,
        sono_media=8.0,
    )
    texto = _texto(gerar_pdf(dados), tmp_path / "r.pdf")
    assert "1 lançamento" in texto and "1 lançamentos" not in texto
    assert "1 treino concluído no mês" in texto
    assert "1 noite anotada" in texto
