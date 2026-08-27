"""Ferramenta de aviso por Telegram.

O canal dos avisos proativos, escolhido de propósito **separado do WhatsApp**:
se o WhatsApp quebrar ou a conta for banida - risco assumido no caminho
nao-oficial -, os avisos continuam chegando por aqui.
"""

from __future__ import annotations

from typing import Any, ClassVar

from core.config import Settings
from core.logging import get_logger
from integrations.telegram import TelegramClient, TelegramError, truncar
from security.policy import RiskLevel
from tools.registry import Tool, ToolResult

log = get_logger("tools.telegram")

# Quanto do texto entra na frase de confirmacao. A pessoa precisa ver o que
# vai ser dito antes de autorizar, mas um paragrafo inteiro numa frase falada
# em voz alta e pior que um resumo.
PREVIA_NA_CONFIRMACAO = 120


class TelegramEnviarTool(Tool):
    """Manda uma mensagem para o dono do Optmus, no Telegram."""

    name = "telegram_enviar"
    risk = RiskLevel.EXTERNO
    description = (
        "Manda uma mensagem de texto no Telegram para o proprio usuario. Use "
        "para avisar de algo que ele pediu para ser lembrado, ou quando a "
        "resposta precisa chegar mesmo com ele longe da tela. NAO use para "
        "responder a conversa em andamento - para isso basta responder. "
        "A mensagem vai sempre para o usuario; nao ha como escolher outro "
        "destinatario."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "texto": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4096,
                "description": (
                    "A mensagem, em texto puro. Sem markdown: o Telegram recebe "
                    "como esta escrito."
                ),
            }
        },
        "required": ["texto"],
        "additionalProperties": False,
    }

    def __init__(self, settings: Settings, client: TelegramClient | None = None) -> None:
        self._settings = settings
        self.client = client or TelegramClient(settings)

    async def available(self) -> bool:
        """Sem token e sem chat, a ferramenta nem entra no schema do modelo.

        Oferecer um canal que nao pode entregar e pior que nao ter: o modelo
        diz ao usuario que avisou, e o aviso nunca chega.
        """
        if not self.client.configurado:
            log.info(
                "telegram.indisponivel",
                motivo="falta OPTMUS_TELEGRAM_BOT_TOKEN ou OPTMUS_TELEGRAM_CHAT_ID",
                acao="python scripts/telegram_id.py",
            )
            return False
        return True

    def resumir(self, parametros: dict[str, Any]) -> str:
        """Frase lida em voz alta antes de mandar.

        Mostra o TEXTO, nao o nome da ferramenta: quem autoriza precisa saber o
        que vai ser dito, nao qual funcao vai rodar. Mensagem e irreversivel -
        depois de enviada, some do controle de todo mundo.
        """
        texto = str(parametros.get("texto", "")).strip()
        if len(texto) <= PREVIA_NA_CONFIRMACAO:
            return f'mandar no seu Telegram: "{texto}"'
        # Marca propria, e nao a do `truncar`: aquela avisa que a MENSAGEM foi
        # cortada. Aqui so a previa foi - dizer o contrario faria a pessoa
        # autorizar achando que o texto ia chegar mutilado.
        previa = texto[:PREVIA_NA_CONFIRMACAO].rstrip()
        return (
            f'mandar no seu Telegram: "{previa}..." '
            f"(a previa esta cortada, a mensagem vai inteira)"
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        texto = str(kwargs.get("texto", "")).strip()
        if not texto:
            return ToolResult.erro("mensagem vazia: nao ha o que enviar")

        try:
            resultado = await self.client.enviar(texto)
        except TelegramError as exc:
            # Erro de ferramenta e resposta, nao excecao: o modelo precisa poder
            # dizer ao usuario que o aviso NAO foi enviado. Achar que enviou e
            # pior do que a falha em si.
            log.warning("telegram.falhou", erro=str(exc))
            return ToolResult.erro(f"Nao consegui enviar pelo Telegram: {exc}")

        return ToolResult(
            content="Mensagem entregue no Telegram.",
            metadata={
                "message_id": resultado.get("message_id"),
                "caracteres": len(texto),
                "truncada": len(texto) > len(truncar(texto)),
            },
        )
