"""Mapa entre as bases do Notion e o que as agregacoes esperam.

Este arquivo existe porque **eu nao posso adivinhar o schema do seu Notion**.
Se a coluna de valor se chama "Valor", "Preco" ou "Amount", se o tipo e um
select "Tipo" com valores "Receita"/"Despesa" ou um numero negativo - nada disso
da para inferir sem olhar. E numa conferencia de numeros, chutar produz o pior
resultado possivel: um total plausivel e errado.

O mapa e um JSON simples, versionavel e editavel a mao:

    {
      "financeiro": {
        "database_id": "abc123...",
        "data": "Data",
        "valor": "Valor",
        "tipo": "Tipo",
        "valores_receita": ["Receita", "Entrada"],
        "valores_despesa": ["Despesa", "Saida"]
      },
      ...
    }

``POST /notion/descobrir`` gera um rascunho a partir das bases reais, com os
nomes de propriedade que existem - ai voce so corrige o que estiver errado.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

from core.logging import get_logger
from integrations.notion import DatabaseInfo

log = get_logger("integrations.notion_map")

# Palpites para o rascunho da descoberta. Sao SUGESTOES ordenadas por
# probabilidade, nunca decisao final - o rascunho e para o humano revisar.
PISTAS: Final[dict[str, tuple[str, ...]]] = {
    "data": ("data", "date", "quando", "vencimento", "dia"),
    "valor": ("valor", "value", "amount", "preco", "total", "quantia"),
    "tipo": ("tipo", "type", "categoria", "category", "natureza"),
    "status": ("status", "situacao", "estado", "concluido", "done"),
    "titulo": ("nome", "name", "titulo", "title", "descricao"),
    "detalhe": ("materia", "detalhe", "assunto", "disciplina", "obs"),
}


@dataclass(slots=True)
class MapaFinanceiro:
    database_id: str = ""
    data: str = "Data"
    valor: str = "Valor"
    tipo: str = "Tipo"
    categoria: str = "Categoria"
    valores_receita: list[str] = field(default_factory=lambda: ["Receita"])
    valores_despesa: list[str] = field(default_factory=lambda: ["Despesa"])


@dataclass(slots=True)
class MapaTrabalho:
    database_id: str = ""
    tipo: str = "Tipo"
    status: str = "Status"
    valores_concluido: list[str] = field(default_factory=lambda: ["Concluido", "Done"])


@dataclass(slots=True)
class MapaEstudos:
    """Base de Estudos: alimenta study() e grades()."""

    database_id: str = ""
    titulo: str = "Título"
    data: str = "Data"
    disciplina: str = "Disciplina"
    tipo: str = "Tipo"
    status: str = "Status"
    nota: str = "Nota"


@dataclass(slots=True)
class MapaTreino:
    database_id: str = ""
    data: str = "Data"
    status: str = "Status"
    duracao: str = "Duração (min)"
    # MEDIDO: o Status so tem 'Planejado' e 'Concluído', e o Web conta apenas o
    # concluido - um treino com data PASSADA e status 'Planejado' segue em zero
    # do lado dele.
    valores_realizado: list[str] = field(
        default_factory=lambda: ["Concluído", "Concluido", "Done"]
    )


@dataclass(slots=True)
class MapaTarefas:
    """Base "Gestao de tarefas" - alimenta o bloco TAREFAS do relatorio."""

    database_id: str = ""
    titulo: str = "Tarefa"
    status: str = "Status da tarefa"
    prioridade: str = "Prioridade"
    prazo: str = "Data de conclusão"
    valores_concluido: list[str] = field(
        default_factory=lambda: ["Concluído", "Concluido", "Done"]
    )


@dataclass(slots=True)
class MapaNotasTrimestre:
    """Base propria de notas: Disciplina x Trimestre x Pontos."""

    database_id: str = ""
    disciplina: str = "Disciplina"
    trimestre: str = "Trimestre"
    pontos: str = "Pontos"


@dataclass(slots=True)
class MapaSono:
    """Registro de sono. ``noite`` e um titulo no formato DD/MM, nao uma data."""

    database_id: str = ""
    noite: str = "Noite"
    horas: str = "Horas"
    qualidade: str = "Qualidade"
    # Constante do Web, nao vem do Notion.
    meta_horas: float = 8.0


@dataclass(slots=True)
class MapaNotas:
    """Constantes de avaliacao escolar.

    NAO estao no Notion: sao regra de negocio embutida no Optmus Web. Os valores
    padrao sao os que o Web devolve hoje - se a sua escola mudar, mude aqui.
    """

    total_ano: float = 100.0
    minimo_aprovacao: float = 60.0
    trimestres: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"numero": 1, "nome": "1º trimestre", "total": 30, "minimo": 18},
            {"numero": 2, "nome": "2º trimestre", "total": 30, "minimo": 18},
            {"numero": 3, "nome": "3º trimestre", "total": 40, "minimo": 24},
        ]
    )


@dataclass(slots=True)
class MapaPrazos:
    """Fonte de alertas de prazo (estudos, provas, entregas)."""

    database_id: str = ""
    titulo: str = "Nome"
    data: str = "Data"
    detalhe: str = ""
    rotulo: str = "estudo"
    # MEDIDO: o Web nao alerta sobre o que ja foi concluido. As duas grafias
    # entram porque a comparacao e exata e "Concluído" != "Concluido".
    status: str = "Status"
    valores_concluido: list[str] = field(
        default_factory=lambda: ["Concluído", "Concluido", "Done"]
    )


@dataclass(slots=True)
class NotionMap:
    financeiro: MapaFinanceiro = field(default_factory=MapaFinanceiro)
    trabalho: MapaTrabalho = field(default_factory=MapaTrabalho)
    estudos: MapaEstudos = field(default_factory=MapaEstudos)
    treino: MapaTreino = field(default_factory=MapaTreino)
    notas: MapaNotas = field(default_factory=MapaNotas)
    notas_trimestre: MapaNotasTrimestre = field(default_factory=MapaNotasTrimestre)
    tarefas: MapaTarefas = field(default_factory=MapaTarefas)
    sono: MapaSono = field(default_factory=MapaSono)
    prazos: list[MapaPrazos] = field(default_factory=list)

    def pendencias(self) -> list[str]:
        """O que ainda falta preencher para as agregacoes rodarem."""
        faltando: list[str] = []
        if not self.financeiro.database_id:
            faltando.append("financeiro.database_id")
        if not self.trabalho.database_id:
            faltando.append("trabalho.database_id")
        if not self.estudos.database_id:
            faltando.append("estudos.database_id")
        if not self.treino.database_id:
            faltando.append("treino.database_id")
        if not self.notas_trimestre.database_id:
            faltando.append("notas_trimestre.database_id")
        if not self.sono.database_id:
            faltando.append("sono.database_id")
        if not self.tarefas.database_id:
            faltando.append("tarefas.database_id")
        if not any(p.database_id for p in self.prazos):
            faltando.append("prazos[].database_id")
        return faltando

    @property
    def completo(self) -> bool:
        return not self.pendencias()

    def to_dict(self) -> dict[str, Any]:
        return {
            "financeiro": asdict(self.financeiro),
            "trabalho": asdict(self.trabalho),
            "estudos": asdict(self.estudos),
            "treino": asdict(self.treino),
            "notas": asdict(self.notas),
            "notas_trimestre": asdict(self.notas_trimestre),
            "tarefas": asdict(self.tarefas),
            "sono": asdict(self.sono),
            "prazos": [asdict(p) for p in self.prazos],
        }


def carregar(caminho: Path) -> NotionMap:
    if not caminho.exists():
        return NotionMap()
    try:
        bruto = json.loads(caminho.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"mapa do Notion invalido ({caminho}): {exc}") from exc
    return de_dict(bruto)


def de_texto(texto: str) -> NotionMap:
    """Monta o mapa a partir de um JSON em texto.

    O caminho de producao: o mapa chega por variavel de ambiente em vez de
    arquivo, porque ele identifica bases pessoais e nao pode viajar no
    repositorio nem na imagem.

    JSON invalido levanta com o motivo. Devolver um mapa vazio aqui seria pior:
    todo /notion/* passaria a dizer "mapa incompleto", e ninguem ligaria isso a
    uma virgula sobrando na variavel.
    """
    try:
        bruto = json.loads(texto)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"OPTMUS_NOTION_MAP_JSON nao e JSON valido: {exc}"
        ) from exc
    if not isinstance(bruto, dict):
        raise ValueError("OPTMUS_NOTION_MAP_JSON precisa ser um objeto JSON")
    return de_dict(bruto)


def de_dict(bruto: dict[str, Any]) -> NotionMap:
    return NotionMap(
        financeiro=MapaFinanceiro(**(bruto.get("financeiro") or {})),
        trabalho=MapaTrabalho(**(bruto.get("trabalho") or {})),
        estudos=MapaEstudos(**(bruto.get("estudos") or {})),
        treino=MapaTreino(**(bruto.get("treino") or {})),
        notas=MapaNotas(**(bruto.get("notas") or {})),
        notas_trimestre=MapaNotasTrimestre(**(bruto.get("notas_trimestre") or {})),
        tarefas=MapaTarefas(**(bruto.get("tarefas") or {})),
        sono=MapaSono(**(bruto.get("sono") or {})),
        prazos=[MapaPrazos(**p) for p in (bruto.get("prazos") or [])],
    )


def salvar(mapa: NotionMap, caminho: Path) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(
        json.dumps(mapa.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log.info("notion.mapa_salvo", caminho=str(caminho))


def _melhor(base: DatabaseInfo, chave: str, tipos: tuple[str, ...]) -> str:
    """Propriedade mais provavel para um papel, ou string vazia."""
    candidatas = [nome for nome, tipo in base.propriedades.items() if tipo in tipos]
    for pista in PISTAS.get(chave, ()):
        for nome in candidatas:
            if pista in nome.lower():
                return nome
    return candidatas[0] if candidatas else ""


def rascunho(bases: list[DatabaseInfo]) -> dict[str, Any]:
    """Monta um rascunho do mapa a partir das bases reais.

    **E um rascunho.** Os nomes de propriedade sao reais (vieram do Notion), mas
    a associacao entre base e papel e heuristica pela palavra no titulo. Reveja
    antes de confiar - especialmente qual coluna e o valor no financeiro.
    """
    saida: dict[str, Any] = {"financeiro": {}, "trabalho": {}, "prazos": []}
    for base in bases:
        titulo = base.titulo.lower()
        if any(p in titulo for p in ("financ", "gasto", "despesa", "dinheiro", "conta")):
            saida["financeiro"] = {
                "database_id": base.id,
                "data": _melhor(base, "data", ("date", "created_time", "formula")),
                "valor": _melhor(base, "valor", ("number", "formula", "rollup")),
                "tipo": _melhor(base, "tipo", ("select", "status", "multi_select")),
                "valores_receita": ["Receita"],
                "valores_despesa": ["Despesa"],
                "_propriedades_disponiveis": base.propriedades,
            }
        elif any(p in titulo for p in ("trabalho", "work", "empresa", "job")):
            saida["trabalho"] = {
                "database_id": base.id,
                "tipo": _melhor(base, "tipo", ("select", "status", "multi_select")),
                "status": _melhor(base, "status", ("status", "select", "checkbox")),
                "valores_concluido": ["Concluido", "Done"],
                "_propriedades_disponiveis": base.propriedades,
            }
        elif "nota" in titulo:
            saida["notas_trimestre"] = {
                "database_id": base.id,
                "disciplina": _melhor(base, "detalhe", ("title",)),
                "trimestre": _melhor(base, "tipo", ("select", "status")),
                "pontos": _melhor(base, "valor", ("number", "formula", "rollup")),
                "_propriedades_disponiveis": base.propriedades,
            }
        elif any(p in titulo for p in ("sono", "sleep", "dormi")):
            saida["sono"] = {
                "database_id": base.id,
                "noite": _melhor(base, "titulo", ("title",)),
                "horas": _melhor(base, "valor", ("number", "formula")),
                "qualidade": _melhor(base, "tipo", ("select", "status")),
                "meta_horas": 8.0,
                "_propriedades_disponiveis": base.propriedades,
            }
        elif any(p in titulo for p in ("treino", "workout", "academia", "exerc")):
            saida["treino"] = {
                "database_id": base.id,
                "data": _melhor(base, "data", ("date", "formula")),
                "status": _melhor(base, "status", ("select", "status", "checkbox")),
                "duracao": _melhor(base, "valor", ("number", "formula")),
                # Sugestao a partir das opcoes REAIS do select, nao de um chute:
                # so entram os rotulos que existem mesmo na base.
                "valores_realizado": [
                    o
                    for o in base.opcoes.get(
                        _melhor(base, "status", ("select", "status")), []
                    )
                    if any(p in o.lower() for p in ("conclu", "feito", "realiz", "done"))
                ]
                or ["Concluído"],
                "_propriedades_disponiveis": base.propriedades,
            }
        elif any(p in titulo for p in ("estudo", "prova", "materia", "escola", "faculdade")):
            saida["prazos"].append(
                {
                    "database_id": base.id,
                    "titulo": _melhor(base, "titulo", ("title",)),
                    "data": _melhor(base, "data", ("date", "formula")),
                    "detalhe": _melhor(base, "detalhe", ("select", "rich_text", "multi_select")),
                    "rotulo": "estudo",
                    "_propriedades_disponiveis": base.propriedades,
                }
            )
    return saida
