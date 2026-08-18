"""Ferramentas de memoria: ``lembrar``, ``recordar`` e ``perfil_atualizar``.

O Optmus ja recupera memoria sozinho a cada turno (F2). Estas ferramentas
existem para o caso deliberado: o usuario diz "guarda isso" e o modelo grava
como fato, em vez de deixar o episodio decair junto com o resto da conversa.

``perfil_atualizar`` e a **unica** porta de escrita do ``perfil.md``. A spec e
explicita: o perfil muda por ferramenta explicita, nunca implicitamente - ele
entra em todo prompt, e um fato errado ali contamina todas as conversas
seguintes em vez de estragar uma busca.
"""

from __future__ import annotations

from typing import Any, ClassVar

from core.logging import get_logger
from memory.profile import SECOES_PADRAO
from memory.system import MemorySystem
from security.policy import RiskLevel
from tools.registry import Tool, ToolResult

log = get_logger("tools.memory")


class LembrarTool(Tool):
    name = "lembrar"
    description = (
        "Guarda um fato durador sobre o usuario ou o mundo dele na memoria de longo "
        "prazo. Use quando ele pedir para lembrar de algo, ou quando disser algo que "
        "continuara verdadeiro daqui a semanas (preferencias, pessoas, projetos, "
        "restricoes). NAO use para pedido pontual, resultado de consulta ou horario. "
        "Se isto corrige algo que voce ja sabia, passe 'corrige_id' com o id antigo: "
        "o fato antigo e versionado, nao apagado."
    )
    risk = RiskLevel.ESCRITA
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "conteudo": {
                "type": "string",
                "description": "o fato, em uma frase completa e autocontida",
            },
            "confianca": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "0.9 se o usuario afirmou; 0.6 se voce inferiu",
            },
            "corrige_id": {
                "type": "integer",
                "description": "id do fato que este substitui, se houver",
            },
        },
        "required": ["conteudo"],
        "additionalProperties": False,
    }

    def __init__(self, memory: MemorySystem) -> None:
        self._memory = memory

    def resumir(self, parametros: dict[str, Any]) -> str:
        return f"guardar na memoria: {parametros.get('conteudo', '')}"

    async def execute(self, **kwargs: Any) -> ToolResult:
        conteudo = str(kwargs.get("conteudo", "")).strip()
        if len(conteudo) < 3:
            return ToolResult.erro("conteudo vazio ou curto demais para virar memoria")

        corrige = kwargs.get("corrige_id")
        if corrige is None:
            duplicado = await self._memory.semantic.ja_sabe(conteudo)
            if duplicado is not None:
                return ToolResult(
                    content=f"Ja sabia disso (id {duplicado}). Nada gravado.",
                    metadata={"duplicado": duplicado},
                )

        fato = await self._memory.semantic.remember(
            conteudo,
            source="ferramenta",
            confidence=float(kwargs.get("confianca", 0.9)),
            supersedes=int(corrige) if corrige is not None else None,
        )
        texto = f"Guardado (id {fato.id})."
        if fato.corrigiu:
            texto += f" O fato {fato.superou} foi marcado como superado, nao apagado."
        return ToolResult(content=texto, metadata={"id": fato.id})


class RecordarTool(Tool):
    name = "recordar"
    description = (
        "Busca na memoria de longo prazo do usuario (episodios e fatos). "
        "Use quando ele perguntar 'o que eu disse sobre X', 'voce lembra de Y', ou "
        "quando precisar de contexto anterior que nao esta nesta conversa. "
        "Devolve os trechos mais relevantes com a data."
    )
    risk = RiskLevel.LEITURA
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "consulta": {"type": "string", "description": "o que procurar, em linguagem natural"},
            "limite": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["consulta"],
        "additionalProperties": False,
    }

    def __init__(self, memory: MemorySystem) -> None:
        self._memory = memory

    async def execute(self, **kwargs: Any) -> ToolResult:
        consulta = str(kwargs.get("consulta", "")).strip()
        if not consulta:
            return ToolResult.erro("consulta vazia")

        achados = await self._memory.recall(consulta, limit=int(kwargs.get("limite", 5)))
        if not achados:
            aviso = ""
            if not self._memory.embedder.semantico:
                aviso = (
                    " A busca esta em modo lexical (so casa palavras), entao pode ter "
                    "passado batido - considere perguntar ao usuario."
                )
            return ToolResult(content=f"Nada encontrado sobre isso na memoria.{aviso}")

        linhas = "\n".join(
            f"- [{h.layer}, {h.created_at[:10]}] {h.content}" for h in achados
        )
        return ToolResult(content=linhas, metadata={"encontrados": len(achados)})


class PerfilAtualizarTool(Tool):
    name = "perfil_atualizar"
    description = (
        "Reescreve uma secao do perfil permanente do usuario, que entra em TODA "
        "conversa. Use so quando ele pedir explicitamente para mudar algo duradouro "
        "sobre si (preferencias, pessoas importantes, projetos ativos, rotina). "
        "A secao inteira e substituida pelo conteudo enviado - inclua o que deve "
        "permanecer. Para um fato solto, prefira a ferramenta 'lembrar'."
    )
    risk = RiskLevel.ESCRITA
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "secao": {"type": "string", "enum": list(SECOES_PADRAO)},
            "conteudo": {
                "type": "string",
                "description": "conteudo completo da secao, em markdown (lista de itens)",
            },
        },
        "required": ["secao", "conteudo"],
        "additionalProperties": False,
    }

    def __init__(self, memory: MemorySystem) -> None:
        self._memory = memory

    def resumir(self, parametros: dict[str, Any]) -> str:
        return f"reescrever a secao '{parametros.get('secao')}' do perfil"

    async def execute(self, **kwargs: Any) -> ToolResult:
        secao = str(kwargs.get("secao", ""))
        if secao not in SECOES_PADRAO:
            return ToolResult.erro(
                f"secao invalida: {secao}. Disponiveis: {', '.join(SECOES_PADRAO)}"
            )
        await self._memory.profile.update_section(secao, str(kwargs.get("conteudo", "")))
        return ToolResult(content=f"Secao '{secao}' do perfil atualizada.")
