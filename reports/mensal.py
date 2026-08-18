"""Relatorio mensal em PDF - o que hoje sai de /api/reports/monthly.

O modulo e partido em dois de proposito:

``montar_dados()``  junta os numeros a partir das agregacoes ja conferidas.
``gerar_pdf()``     desenha. So layout, nenhuma conta.

A separacao e o que torna o relatorio testavel. Conferir numero dentro de PDF
exige extrair texto de volta, e ai um erro de arredondamento vira um teste que
falha por causa de quebra de linha. Com ``DadosRelatorio`` no meio, os numeros
sao comparados como numeros, e o PDF so responde por desenho.

**Nada aqui recalcula nada.** Toda conta vem de ``NotionStats``, que ja foi
conferida campo a campo contra o Web. Se o relatorio fizesse a propria soma,
teriamos duas versoes da mesma regra para manter em sincronia - e a segunda
nunca aparece na conferencia.

Formatacao conferida contra o PDF do Web (14/08/2026):

- moeda ``R$ 1.234,56``, negativo como ``-R$ 175,00`` (sinal antes do R$);
- taxa de economia **arredondada para inteiro**: 76,62% sai como ``77%``;
- qualidade do sono **sem emoji**: ``😊 Bom`` sai como ``Bom``;
- estudo sem ``Tipo`` aparece como ``Outro``;
- noites listadas da **mais recente para a mais antiga**, ao contrario da
  ordem que /api/stats/sleep devolve.
"""

from __future__ import annotations

import asyncio
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from io import BytesIO
from typing import Any

from core.logging import get_logger

log = get_logger("reports.mensal")

MESES: tuple[str, ...] = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)


class ReportlabAusente(RuntimeError):
    """O extra [relatorios] nao esta instalado."""

    def __init__(self) -> None:
        super().__init__(
            "geracao de PDF precisa do reportlab: pip install -e .[relatorios]"
        )


@dataclass(slots=True)
class DadosRelatorio:
    """Tudo que o PDF mostra, ja formatado como numero - nunca como texto.

    A formatacao fica em ``gerar_pdf``. Guardar "R$ 734,62" aqui impediria o
    teste de comparar contra 734.62 sem desfazer a formatacao.
    """

    mes: str = ""
    titulo: str = ""
    gerado_em: str = ""

    saldo: float = 0.0
    receita: float = 0.0
    despesa: float = 0.0
    taxa_economia: float | None = None
    categorias: list[dict[str, Any]] = field(default_factory=list)

    tarefas_concluidas: int = 0
    tarefas_pendentes: int = 0
    tarefas_atrasadas: int = 0

    trabalho_ativas: int = 0
    trabalho_concluidas: int = 0
    trabalho_por_tipo: list[dict[str, Any]] = field(default_factory=list)

    total_ano: float = 100.0
    minimo_aprovacao: float = 60.0
    trimestres: list[dict[str, Any]] = field(default_factory=list)
    disciplinas: list[dict[str, Any]] = field(default_factory=list)

    estudos_do_mes: list[dict[str, Any]] = field(default_factory=list)
    treinos_no_mes: int = 0

    sono_media: float = 0.0
    sono_noites: int = 0
    sono_meta: float = 8.0
    sono_na_meta: int = 0
    melhor_noite: dict[str, Any] | None = None
    pior_noite: dict[str, Any] | None = None
    noites: list[dict[str, Any]] = field(default_factory=list)

    avisos: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            campo: getattr(self, campo) for campo in self.__dataclass_fields__
        }


async def montar_dados(stats: Any, *, hoje: date | None = None) -> DadosRelatorio:
    """Junta os numeros do mes a partir das agregacoes ja conferidas."""
    hoje = hoje or datetime.now().date()
    chave = hoje.isoformat()[:7]

    # Em paralelo, e nao em sequencia: sao nove consultas independentes ao
    # Notion, de cerca de um segundo cada. Encadeadas davam ~11s de resposta -
    # acima do limite de execucao de funcao serverless, ou seja, o relatorio
    # simplesmente nao existiria no deploy.
    (
        mensal,
        poupanca,
        categorias,
        tarefas,
        trabalho,
        notas,
        estudos_mes,
        treino,
        sono,
    ) = await asyncio.gather(
        stats.monthly(hoje=hoje),
        stats.savings_rate(hoje=hoje),
        stats.category_spending(hoje=hoje),
        stats.tasks(),
        stats.work_tasks(),
        stats.grades(),
        stats.study_month(hoje=hoje),
        stats.workout_monthly(hoje=hoje),
        stats.sleep(),
    )
    do_mes = next((m for m in mensal if m.get("month") == chave), None) or {}

    # workout_monthly devolve a janela em ordem crescente terminando hoje, entao
    # o ultimo balde e o mes corrente. Casar pelo rotulo ("Ago/26") daria o mes
    # ERRADO: o primeiro que termina em "/26" e o mais antigo da janela.
    treinos = int(treino[-1].get("quantidade", 0)) if treino else 0

    # As noites saem de sleep() em ordem crescente; o PDF mostra ao contrario.
    noites = list(reversed(sono.get("ultimasNoites") or []))

    return DadosRelatorio(
        mes=chave,
        titulo=f"{MESES[hoje.month - 1].capitalize()} de {hoje.year}",
        gerado_em=datetime.now().strftime("%d/%m/%Y às %H:%M"),
        saldo=float(do_mes.get("balance", 0.0)),
        receita=float(do_mes.get("income", 0.0)),
        despesa=float(do_mes.get("expense", 0.0)),
        taxa_economia=poupanca.get("currentRate"),
        categorias=list(categorias),
        tarefas_concluidas=int(tarefas.get("totalConcluidas", 0)),
        tarefas_pendentes=int(tarefas.get("totalPendentes", 0)),
        tarefas_atrasadas=int(tarefas.get("totalAtrasadas", 0)),
        trabalho_ativas=sum(int(t.get("ativo", 0)) for t in trabalho),
        trabalho_concluidas=sum(int(t.get("concluido", 0)) for t in trabalho),
        trabalho_por_tipo=list(trabalho),
        total_ano=float(notas.get("totalAno", 100.0)),
        minimo_aprovacao=float(notas.get("minimoAprovacao", 60.0)),
        trimestres=list(notas.get("trimestres") or []),
        disciplinas=list(notas.get("disciplinas") or []),
        estudos_do_mes=list(estudos_mes),
        treinos_no_mes=treinos,
        sono_media=float(sono.get("mediaHoras", 0.0)),
        sono_noites=int(sono.get("noitesRegistradas", 0)),
        sono_meta=float(sono.get("metaHoras", 8.0)),
        sono_na_meta=int(sono.get("noitesNaMeta", 0)),
        melhor_noite=sono.get("melhorNoite"),
        pior_noite=sono.get("piorNoite"),
        noites=noites,
        avisos=[a.to_dict() for a in getattr(stats, "avisos", [])],
    )


# ----------------------------------------------------------------- formatacao
def moeda(valor: float) -> str:
    """``R$ 1.234,56``, com o sinal ANTES do R$ - como no PDF do Web.

    Espaco comum, nao NBSP: o ``Paragraph`` do reportlab normaliza espaco em
    branco e o NBSP nao chega ao papel. Como estes valores ficam em celula
    propria e nunca quebram linha, nao ha o que proteger.
    """
    sinal = "-" if valor < 0 else ""
    inteiro, centavos = divmod(round(abs(valor) * 100), 100)
    return f"{sinal}R$ {inteiro:,}".replace(",", ".") + f",{centavos:02d}"


def horas(valor: float) -> str:
    """``6.5h`` e ``8h`` - sem casa decimal quando e inteiro."""
    return f"{valor:g}h"


def sem_emoji(texto: str) -> str:
    """Tira emoji e sobras de espaco.

    O Notion guarda ``😊 Bom`` e ``🔥 Muito alta``; o PDF do Web mostra so a
    palavra. Alem de bater com ele, as fontes base do PDF nao tem glifo de
    emoji - deixar passar imprimiria um quadrado preto.
    """
    limpo = "".join(
        c for c in texto if unicodedata.category(c) not in ("So", "Sk", "Cf")
    )
    return " ".join(limpo.split())


def _pontos(valor: Any) -> str:
    return "—" if valor is None else f"{valor:g}"


# --------------------------------------------------------------------- layout
def gerar_pdf(dados: DadosRelatorio) -> bytes:
    """Desenha o relatorio. Nenhuma conta acontece aqui."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_RIGHT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            KeepTogether,
            PageBreak,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as exc:  # pragma: no cover - depende do ambiente
        raise ReportlabAusente() from exc

    tinta = colors.HexColor("#111111")
    fraco = colors.HexColor("#767676")
    linha = colors.HexColor("#E3E3E3")

    def estilo(nome: str, **kw: Any) -> ParagraphStyle:
        """Entrelinha acompanha o corpo, salvo quando informada.

        Sem isto, a entrelinha 13 da base valeria tambem para os estilos de 17 e
        22 pontos, e as linhas se sobrepunham - foi o que juntou "OPTMUS" com o
        subtitulo no cabecalho.
        """
        base: dict[str, Any] = {"fontName": "Helvetica", "fontSize": 9.5, "textColor": tinta}
        base["leading"] = round(float(kw.get("fontSize", base["fontSize"])) * 1.35, 1)
        return ParagraphStyle(nome, **{**base, **kw})

    st_marca = estilo("marca", fontName="Helvetica-Bold", fontSize=17)
    st_sub = estilo("sub", fontSize=8.5, textColor=fraco)
    st_dir = estilo("dir", fontSize=8, textColor=fraco, alignment=TA_RIGHT)
    st_titulo = estilo("titulo", fontName="Helvetica-Bold", fontSize=25, leading=30)
    st_secao = estilo("secao", fontName="Helvetica-Bold", fontSize=8, textColor=fraco)
    st_rot = estilo("rot", fontSize=7, textColor=fraco)
    st_num = estilo("num", fontName="Helvetica-Bold", fontSize=13)
    st_forte = estilo("forte", fontName="Helvetica-Bold")
    st_val = estilo("val", fontName="Helvetica-Bold", alignment=TA_RIGHT)
    st_nota = estilo("nota", fontName="Helvetica-Oblique", fontSize=8, textColor=fraco)

    buffer = BytesIO()
    largura = A4[0] - 36 * mm
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=f"Optmus - relatorio {dados.mes}",
        author="Optmus Core",
    )

    def secao(nome: str) -> list[Any]:
        """Cabecalho de secao: rotulo espacado sobre um filete.

        As palavras precisam de NBSP entre si. So espacar as letras juntaria
        "PROVAS E TRABALHOS" num bloco unico, porque o espaco entre palavras
        fica igual ao espaco entre letras.
        """
        espacado = "&nbsp;&nbsp;&nbsp;".join(
            " ".join(palavra) for palavra in nome.upper().split()
        )
        tabela = Table([[Paragraph(espacado, st_secao)]], colWidths=[largura])
        tabela.setStyle(
            TableStyle(
                [
                    ("LINEBELOW", (0, 0), (-1, -1), 0.6, linha),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        return [Spacer(1, 7 * mm), tabela, Spacer(1, 3 * mm)]

    def linhas(pares: list[tuple[str, str]]) -> Table:
        """Rotulo a esquerda, valor em negrito a direita."""
        tabela = Table(
            [[Paragraph(r, estilo("r")), Paragraph(v, st_val)] for r, v in pares],
            colWidths=[largura * 0.62, largura * 0.38],
        )
        tabela.setStyle(
            TableStyle(
                [
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        return tabela

    def item(titulo: str, detalhe: str, valor: str) -> Table:
        """Titulo em negrito com uma linha fraca embaixo, valor a direita."""
        tabela = Table(
            [
                [Paragraph(titulo, st_forte), Paragraph(valor, st_val)],
                [Paragraph(detalhe, st_rot), ""],
            ],
            colWidths=[largura * 0.62, largura * 0.38],
        )
        tabela.setStyle(
            TableStyle(
                [
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 1),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                    ("SPAN", (1, 0), (1, 1)),
                    ("VALIGN", (1, 0), (1, 1), "TOP"),
                ]
            )
        )
        return tabela

    def destaque(numero: str, texto: str) -> Paragraph:
        """Numero grande com o rotulo colado, na mesma linha de base.

        Duas celulas de tabela nao servem: a largura da coluna afasta o rotulo
        do numero, e ele muda de lugar conforme o numero cresce.
        """
        return Paragraph(
            f'<font size="22">{numero}</font>&nbsp;&nbsp;<font size="9.5">{texto}</font>',
            estilo("dst", fontName="Helvetica-Bold", fontSize=22, leading=26),
        )

    fluxo: list[Any] = []

    def abrir(nome: str, conteudo: list[Any]) -> None:
        """Emite uma secao com o titulo grudado no primeiro item.

        Titulo de secao sozinho no pe da pagina e o defeito classico desse tipo
        de relatorio - "FACULDADE" ficava na pagina 1 e a disciplina na 2.
        """
        cabecalho = secao(nome)
        if conteudo:
            fluxo.append(KeepTogether([*cabecalho, conteudo[0]]))
            fluxo.extend(conteudo[1:])
        else:
            fluxo.extend(cabecalho)

    # ------------------------------------------------------------- cabecalho
    marca = Table(
        [[Paragraph("OPTMUS", st_marca)], [Paragraph("Relatório mensal", st_sub)]],
        colWidths=[largura * 0.5],
    )
    marca.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]
        )
    )
    topo = Table(
        [[marca, Paragraph(f"Gerado em {dados.gerado_em}", st_dir)]],
        colWidths=[largura * 0.5, largura * 0.5],
    )
    topo.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    fluxo += [topo, Spacer(1, 9 * mm), Paragraph(dados.titulo, st_titulo), Spacer(1, 6 * mm)]

    # ----------------------------------------------------------------- cards
    cards = [
        ("SALDO DO MÊS", moeda(dados.saldo)),
        ("ECONOMIA", _taxa_texto(dados.taxa_economia)),
        ("TAREFAS PENDENTES", str(dados.tarefas_pendentes)),
        ("SONO (MÉDIA)", horas(dados.sono_media)),
    ]
    faixa = Table(
        [
            [Paragraph(r, st_rot) for r, _ in cards],
            [Paragraph(v, st_num) for _, v in cards],
        ],
        colWidths=[largura / 4] * 4,
    )
    faixa.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.6, linha),
                ("LINEAFTER", (0, 0), (-2, -1), 0.6, linha),
                ("TOPPADDING", (0, 0), (-1, 0), 7),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
            ]
        )
    )
    fluxo.append(faixa)

    # ------------------------------------------------------------ financeiro
    bloco_fin: list[Any] = [
        destaque(moeda(dados.saldo), "de saldo no mês"),
        Spacer(1, 4 * mm),
        linhas(
            [
                ("Receita", moeda(dados.receita)),
                ("Despesa", moeda(dados.despesa)),
                ("Taxa de economia", _taxa_texto(dados.taxa_economia)),
            ]
        ),
    ]
    if dados.categorias:
        bloco_fin += [
            Spacer(1, 4 * mm),
            Paragraph("Gasto por categoria", st_forte),
            Spacer(1, 2 * mm),
        ]
        for cat in dados.categorias:
            quantos = int(cat.get("count", 0))
            bloco_fin.append(
                item(
                    str(cat.get("categoria", "")),
                    f"{quantos} lançamento" + ("s" if quantos != 1 else ""),
                    moeda(float(cat.get("total", 0.0))),
                )
            )
    abrir("financeiro", bloco_fin)

    # --------------------------------------------------------------- tarefas
    abrir(
        "tarefas",
        [
            linhas(
                [
                    ("Concluídas", str(dados.tarefas_concluidas)),
                    ("Pendentes", str(dados.tarefas_pendentes)),
                    ("Atrasadas", str(dados.tarefas_atrasadas)),
                ]
            ),
            Spacer(1, 2 * mm),
            Paragraph(
                "Situação atual — o Optmus não restringe esse número ao mês; "
                "é o total acumulado na base de tarefas.",
                st_nota,
            ),
        ],
    )

    # -------------------------------------------------------------- trabalho
    bloco_trab: list[Any] = [
        linhas(
            [
                ("Ativas", str(dados.trabalho_ativas)),
                ("Concluídas", str(dados.trabalho_concluidas)),
            ]
        ),
        Spacer(1, 2 * mm),
    ]
    for tipo in dados.trabalho_por_tipo:
        ativo, feito = int(tipo.get("ativo", 0)), int(tipo.get("concluido", 0))
        bloco_trab.append(
            item(
                str(tipo.get("tipo", "")),
                f"{ativo} ativa{'s' if ativo != 1 else ''} · "
                f"{feito} concluída{'s' if feito != 1 else ''}",
                str(int(tipo.get("total", 0))),
            )
        )
    abrir("trabalho", bloco_trab)

    # ------------------------------------------------------------- faculdade
    resumo = ", ".join(f"{t.get('numero')}º: {t.get('total')}" for t in dados.trimestres)
    bloco_fac: list[Any] = [
        Paragraph(
            f"O ano vale {dados.total_ano:g} pontos ({resumo}) e a aprovação "
            f"exige {dados.minimo_aprovacao:g}.",
            st_nota,
        ),
        Spacer(1, 3 * mm),
    ]
    for disciplina in dados.disciplinas:
        detalhe = "  ".join(
            f"{t.get('numero')}º {_pontos(t.get('pontos'))}/{t.get('total')}"
            for t in disciplina.get("trimestres") or []
        )
        faltam = float(disciplina.get("precisa", 0))
        if faltam > 0:
            detalhe += (
                f"  ·  faltam {faltam:g} ({float(disciplina.get('restante', 0)):g} em disputa)"
            )
        else:
            detalhe += f"  ·  {disciplina.get('situacao', '')}"
        bloco_fac.append(
            item(
                str(disciplina.get("disciplina", "")),
                detalhe,
                f"{float(disciplina.get('obtido', 0)):g}/{dados.total_ano:g}",
            )
        )
    abrir("faculdade", bloco_fac)

    # ----------------------------------------------- provas e trabalhos do mes
    abrir(
        "provas e trabalhos do mês",
        [
            item(
                str(estudo.get("titulo", "")),
                f"{estudo.get('disciplina', '')} · {estudo.get('tipo', '')} · "
                f"{_data_br(str(estudo.get('data', '')))}",
                str(estudo.get("status", "")),
            )
            for estudo in dados.estudos_do_mes
        ]
        or [Paragraph("Nada com data neste mês.", st_nota)],
    )

    # ---------------------------------------------------------------- treino
    plural = "s" if dados.treinos_no_mes != 1 else ""
    abrir(
        "treino",
        [destaque(str(dados.treinos_no_mes), f"treino{plural} concluído{plural} no mês")],
    )

    # ------------------------------------------------------------------ sono
    s = "s" if dados.sono_noites != 1 else ""
    bloco: list[Any] = [
        destaque(
            horas(dados.sono_media),
            f"de média nas últimas {dados.sono_noites} noite{s} anotada{s}",
        ),
        Spacer(1, 4 * mm),
    ]
    bloco.append(
        linhas(
            [
                (
                    f"Noites na meta ({horas(dados.sono_meta)})",
                    f"{dados.sono_na_meta} de {dados.sono_noites}",
                ),
                ("Melhor noite", _noite_texto(dados.melhor_noite)),
                ("Pior noite", _noite_texto(dados.pior_noite)),
            ]
        )
    )
    if dados.noites:
        bloco += [Spacer(1, 4 * mm), Paragraph("Noites registradas", st_forte), Spacer(1, 2 * mm)]
        for noite in dados.noites:
            bloco.append(
                item(
                    str(noite.get("label", "")),
                    sem_emoji(str(noite.get("qualidade") or "")),
                    horas(float(noite.get("horas", 0))),
                )
            )
    # Sem KeepTogether no bloco inteiro: ele nao cabia no que sobrava da pagina
    # e pulava inteiro para a seguinte, deixando meia pagina em branco. Prender
    # o titulo ao primeiro item ja resolve o que precisava ser resolvido.
    abrir("sono", bloco)

    if dados.avisos:
        fluxo.append(PageBreak())
        abrir(
            "avisos da apuração",
            [
                Paragraph(
                    "Linhas do Notion que ficaram de fora dos totais. Não são erros "
                    "do relatório — são dados incompletos na origem.",
                    st_nota,
                ),
                Spacer(1, 3 * mm),
                *(
                    item(
                        str(aviso.get("campo", "")),
                        str(aviso.get("detalhe", "")),
                        str(aviso.get("linhas", 0) or ""),
                    )
                    for aviso in dados.avisos
                ),
            ],
        )

    doc.build(fluxo)
    log.info("relatorio.gerado", mes=dados.mes, bytes=buffer.tell())
    return buffer.getvalue()


def _taxa_texto(taxa: float | None) -> str:
    """MEDIDO: o Web arredonda para inteiro - 76,62% sai como 77%."""
    return "—" if taxa is None else f"{round(taxa):g}%"


def _noite_texto(noite: dict[str, Any] | None) -> str:
    if not noite:
        return "—"
    return f"{horas(float(noite.get('horas', 0)))} · {noite.get('label', '')}"


def _data_br(iso: str) -> str:
    try:
        return date.fromisoformat(iso[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return iso
