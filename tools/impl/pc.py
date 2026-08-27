"""Abrir coisas reais no PC — pelo portao, e so o que foi registrado.

Duas ferramentas com riscos deliberadamente diferentes:

- ``pc_listar`` (**LEITURA**) diz o que existe. Nao abre nada, entao nao pede
  confirmacao: exigir autorizacao para ler a propria lista ensinaria a confirmar
  por reflexo, e o reflexo e o que quebra o portao quando ele importar.
- ``pc_abrir`` (**EXTERNO**) executa. Passa pelo portao, que desde 23/08/2026
  exige prova do dispositivo que originou o pedido.

O contrato entre as duas e o que sustenta a seguranca: ``pc_listar`` devolve
``id``, ``pc_abrir`` aceita ``id``. **Caminho nao cruza essa fronteira em
nenhuma direcao.**
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, ClassVar

from core.config import Settings
from core.logging import get_logger
from integrations.alvos import AlvoDesconhecido, ListaInvalida, carregar, listar, resolver
from security.api_auth import hospedado
from security.policy import RiskLevel
from tools.registry import Tool, ToolResult

log = get_logger("tools.pc")


def disponivel(settings: Settings) -> tuple[bool, str]:
    """Pode abrir coisas aqui? Tambem devolve o motivo, para log e /health.

    A checagem de plataforma vem primeiro e nao pergunta nada a configuracao -
    mesmo raciocinio do WhatsApp e do ``verificar_exposicao``: uma flag dizendo
    "estou local" e crenca, e ``hospedado()`` e observacao. Abrir um arquivo num
    container de servidor nao e util para ninguem, e o pedido so poderia ter
    vindo de lugar errado.
    """
    if hospedado():
        return False, "plataforma hospedada: abrir arquivo e app so faz sentido local"
    if sys.platform != "win32":
        return False, f"implementado so para Windows (rodando em {sys.platform})"
    if not settings.pc_enabled:
        return False, "OPTMUS_PC_ENABLED=false"
    return True, "ok"


class _FerramentaPC(Tool):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _registro(self) -> dict[str, list[Any]]:
        try:
            return carregar(Path(self._settings.pc_targets_path))
        except ListaInvalida as exc:
            log.error("pc.registro_invalido", erro=str(exc))
            return {"apps": [], "pastas": []}

    async def available(self) -> bool:
        ok, motivo = disponivel(self._settings)
        if not ok:
            log.info("pc.indisponivel", motivo=motivo)
            return False
        registro = self._registro()
        if not registro["apps"] and not registro["pastas"]:
            log.info("pc.indisponivel", motivo=f"nenhum alvo em {self._settings.pc_targets_path}")
            return False
        return True


class PcListarTool(_FerramentaPC):
    """O que da para abrir, com os ids que ``pc_abrir`` aceita."""

    name = "pc_listar"
    risk = RiskLevel.LEITURA
    description = (
        "Lista os aplicativos e pastas que o usuario registrou como abriveis, e "
        "os arquivos dentro de uma pasta registrada. Devolve um 'id' por item - "
        "esse id e a UNICA forma de abrir algo depois. Nao abre nada."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "pasta_id": {
                "type": "string",
                "description": (
                    "Id de uma pasta registrada, para ver o que ha dentro dela. "
                    "Omita para ver o primeiro nivel (apps e pastas)."
                ),
            }
        },
        "required": [],
        "additionalProperties": False,
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        pedido = kwargs.get("pasta_id")
        try:
            itens = listar(self._registro(), pasta_id=str(pedido) if pedido else None)
        except AlvoDesconhecido as exc:
            return ToolResult.erro(str(exc))

        if not itens:
            return ToolResult(content="Nada registrado para abrir aqui.")

        linhas = [f"- [{a.id}] {a.nome} ({a.tipo})" for a in itens]
        return ToolResult(
            content="\n".join(linhas),
            dados={"itens": [a.visivel() for a in itens]},
            metadata={"quantidade": len(itens)},
        )


class PcAbrirTool(_FerramentaPC):
    """Abre um alvo registrado. Executa de verdade, entao passa pelo portao."""

    name = "pc_abrir"
    risk = RiskLevel.EXTERNO
    description = (
        "Abre no computador do usuario um aplicativo, pasta ou arquivo que ele "
        "registrou. O parametro 'alvo_id' e um id vindo de pc_listar - caminho "
        "de arquivo NAO e aceito e sera recusado. Se o que o usuario quer nao "
        "esta na lista, diga isso a ele em vez de tentar adivinhar um caminho."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "alvo_id": {
                "type": "string",
                "minLength": 4,
                "maxLength": 64,
                "description": "Id vindo de pc_listar. NAO e um caminho.",
            }
        },
        "required": ["alvo_id"],
        "additionalProperties": False,
    }

    def resumir(self, parametros: dict[str, Any]) -> str:
        """Frase do portao: diz o NOME e o que vai acontecer.

        Nao mostra o id - um hash de doze caracteres nao ajuda ninguem a
        decidir. E diz "abrir o aplicativo" / "abrir o arquivo" porque a
        diferenca importa: aplicativo executa, arquivo abre no programa padrao.
        """
        pedido = str(parametros.get("alvo_id", ""))
        try:
            alvo = resolver(self._registro(), pedido)
        except AlvoDesconhecido:
            return f"abrir algo que nao esta na lista de alvos ('{pedido[:16]}')"

        artigo = {"app": "o aplicativo", "pasta": "a pasta", "arquivo": "o arquivo"}
        return f"abrir {artigo.get(alvo.tipo, 'o item')} {alvo.nome} no seu computador"

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            alvo = resolver(self._registro(), str(kwargs.get("alvo_id", "")))
        except AlvoDesconhecido as exc:
            # Recusa antes de tocar no sistema de arquivos. O modelo recebe o
            # motivo para poder dizer ao usuario que aquilo nao esta registrado
            # - nao para tentar de novo com outro palpite.
            log.warning("pc.alvo_recusado", motivo=str(exc))
            return ToolResult.erro(str(exc))

        if not alvo.caminho.exists():
            return ToolResult.erro(
                f"{alvo.nome} esta registrado mas nao existe mais no disco."
            )

        try:
            # os.startfile e sincrono no shell mas pode bloquear; fora da thread
            # do laco para nao segurar o Core enquanto o Windows resolve a
            # associacao do arquivo.
            await asyncio.to_thread(os.startfile, str(alvo.caminho))
        except OSError as exc:
            log.warning("pc.abrir_falhou", alvo=alvo.nome, erro=str(exc))
            return ToolResult.erro(f"Nao consegui abrir {alvo.nome}: {exc}")

        log.info("pc.aberto", alvo=alvo.nome, tipo=alvo.tipo)
        return ToolResult(
            content=f"Abri {alvo.nome} no seu computador.",
            metadata={
                # Nome e tipo, nunca o caminho: a trilha de auditoria e
                # permanente e nao precisa guardar a topologia do seu disco.
                "alvo": alvo.nome,
                "tipo": alvo.tipo,
            },
        )
