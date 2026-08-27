"""Ferramenta de envio no WhatsApp (caminho nao oficial, local).

Risco **EXTERNO**: passa pelo portao de confirmacao, que desde 23/08/2026 exige
prova do dispositivo que originou o pedido. Essa ordem importa - o vinculo de
dispositivo foi feito antes desta ferramenta de proposito, porque aqui a acao
autorizada e mandar mensagem para outra pessoa, e uma confirmacao que qualquer
sessao pudesse dar nao seria confirmacao.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from core.config import Settings
from core.logging import get_logger
from integrations.contatos import Contato, ContatoDesconhecido, ListaInvalida, carregar, resolver
from integrations.whatsapp import WhatsAppClient, WhatsAppError, disponivel
from security.policy import RiskLevel
from tools.registry import Tool, ToolResult

log = get_logger("tools.whatsapp")

PREVIA_NA_CONFIRMACAO = 100


class WhatsAppEnviarTool(Tool):
    """Manda mensagem para alguem da lista de contatos permitidos."""

    name = "whatsapp_enviar"
    risk = RiskLevel.EXTERNO
    description = (
        "Manda uma mensagem de WhatsApp para uma pessoa da lista de contatos "
        "do usuario. O parametro 'contato' e o APELIDO de alguem da lista "
        "(por exemplo 'mae', 'joao') - numero de telefone nao e aceito e sera "
        "recusado. Se a pessoa nao estiver na lista, diga isso ao usuario em "
        "vez de tentar outro apelido. Use somente quando ele pedir para avisar "
        "ou responder alguem; nunca por iniciativa propria."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "contato": {
                "type": "string",
                "minLength": 1,
                "maxLength": 60,
                "description": (
                    "Apelido de um contato da lista. NAO e numero de telefone."
                ),
            },
            "texto": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4000,
                "description": "A mensagem, em texto puro.",
            },
        },
        "required": ["contato", "texto"],
        "additionalProperties": False,
    }

    def __init__(self, settings: Settings, client: WhatsAppClient | None = None) -> None:
        self._settings = settings
        self.client = client or WhatsAppClient(settings)

    # ------------------------------------------------------------- lista
    def _lista(self) -> dict[str, Contato]:
        try:
            return carregar(Path(self._settings.whatsapp_contacts_path))
        except ListaInvalida as exc:
            log.error("whatsapp.lista_invalida", erro=str(exc))
            return {}

    async def available(self) -> bool:
        """Tres portas, e a primeira nao pergunta nada a configuracao.

        Sem lista de contatos a ferramenta nao entra no schema mesmo com tudo o
        mais pronto: oferecer envio sem ninguem para quem enviar faria o modelo
        prometer ao usuario uma mensagem que nao tem destino.
        """
        ok, motivo = disponivel(self._settings)
        if not ok:
            log.info("whatsapp.indisponivel", motivo=motivo)
            return False
        if not self._lista():
            log.info(
                "whatsapp.indisponivel",
                motivo=f"lista de contatos vazia: {self._settings.whatsapp_contacts_path}",
            )
            return False
        return True

    # ------------------------------------------------------- confirmacao
    def resumir(self, parametros: dict[str, Any]) -> str:
        """Frase do portao. Mostra NOME e final do numero.

        O final do numero existe porque apelido errado e o erro plausivel aqui:
        dois "joao" na lista, ou o modelo escolhendo o parecido. "Joao Silva
        (final 4321)" da para conferir de relance; so "joao" nao da.

        O numero inteiro nao entra: esta frase vai para a trilha de auditoria e
        para a tela, e numero de terceiro nao precisa ficar nos dois.
        """
        pedido = str(parametros.get("contato", ""))
        texto = str(parametros.get("texto", "")).strip()

        try:
            destino: str = str(resolver(self._lista(), pedido))
        except ContatoDesconhecido:
            # Nao resolve: o portao mostra o pedido cru para a pessoa entender
            # o que o modelo tentou. A execucao vai recusar depois.
            destino = f"'{pedido}' (fora da lista)"

        previa = texto if len(texto) <= PREVIA_NA_CONFIRMACAO else (
            texto[:PREVIA_NA_CONFIRMACAO].rstrip() + "..."
        )
        return f'mandar no WhatsApp para {destino}: "{previa}"'

    # ---------------------------------------------------------- execucao
    async def execute(self, **kwargs: Any) -> ToolResult:
        texto = str(kwargs.get("texto", "")).strip()
        if not texto:
            return ToolResult.erro("mensagem vazia: nao ha o que enviar")

        try:
            contato = resolver(self._lista(), str(kwargs.get("contato", "")))
        except ContatoDesconhecido as exc:
            # Recusa ANTES de tocar na rede. O modelo recebe o motivo para
            # poder dizer ao usuario que a pessoa nao esta na lista - e nao
            # para tentar de novo com outro palpite.
            log.warning("whatsapp.contato_recusado", motivo=str(exc))
            return ToolResult.erro(str(exc))

        try:
            resposta = await self.client.enviar(contato.numero, texto)
        except WhatsAppError as exc:
            log.warning("whatsapp.envio_falhou", erro=str(exc))
            return ToolResult.erro(f"Nao consegui enviar pelo WhatsApp: {exc}")

        return ToolResult(
            content=f"Mensagem entregue no WhatsApp para {contato.nome}.",
            metadata={
                # Apelido e final, nunca o numero: a trilha de auditoria e
                # permanente, e numero de terceiro nao precisa morar nela.
                "contato": contato.apelido,
                "final": contato.final,
                "caracteres": len(texto),
                "id": resposta.get("id"),
            },
        )
