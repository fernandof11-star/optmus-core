"""Loop de agente - porte do ``src/core/engine.ts`` do Optmus Web.

Nucleo burro, ferramentas inteligentes (secao 3.1): este arquivo so decide
*quando* parar de conversar com o modelo. Nenhuma logica de dominio mora aqui.

O loop e o mesmo que ja funciona no Web: manda a conversa, se o modelo pedir
ferramenta, executa, devolve o resultado e roda de novo - ate ``max_rounds``.
O teto existe porque um modelo em loop de ferramenta e um assistente que fala
sozinho a noite inteira.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from core.config import Settings
from core.llm import Imagem, LLMClient, LLMError, LLMTurn, TextSink, ToolCall
from core.logging import get_logger

log = get_logger("core.agent")

# Substitui uma imagem ja vista pelo modelo dentro do mesmo turno.
MARCADOR_IMAGEM = "[imagem ja analisada e descartada para economizar contexto]"

PROMPT_SISTEMA = """Voce e o Optmus, assistente ambiente do {nome}.

Como voce fala:
- Conciso. Nunca duas frases quando uma resolve.
- Sua resposta vira audio: sem markdown, sem listas, sem emoji, sem URL falada.
- Confirma execucao; nao pede permissao para trivialidade.
- Reporta falha direto, sem desculpa longa.
- Trata por "{tratamento}".
- Ironia seca ocasional, so em contexto de baixo risco. Nunca em erro, falha
  ou alerta de seguranca.

O que voce nao faz:
- Nao inventa dado que nao veio de ferramenta. Se nao sabe, diz que nao sabe.
- Nao executa acao irreversivel sem confirmacao explicita."""


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    content: str
    is_error: bool = False
    imagens: tuple[Imagem, ...] = ()


@runtime_checkable
class ToolProvider(Protocol):
    """Contrato do registro de ferramentas (implementado na F3)."""

    def schemas(self) -> list[dict[str, Any]]: ...

    async def execute(
        self, name: str, arguments: dict[str, Any], *, correlation_id: str | None = None
    ) -> ToolOutcome: ...


@dataclass(slots=True)
class AgentResult:
    text: str
    rounds: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None
    truncado: bool = False
    erro: str | None = None

    @property
    def ok(self) -> bool:
        return self.erro is None


class Agent:
    """Orquestrador de uma resposta: modelo -> ferramentas -> modelo -> ..."""

    def __init__(
        self,
        client: LLMClient,
        settings: Settings,
        *,
        tools: ToolProvider | None = None,
        on_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._tools = tools
        self._on_event = on_event

    @property
    def system_prompt(self) -> str:
        return PROMPT_SISTEMA.format(
            nome=self._settings.user_name,
            tratamento=self._settings.user_honorific,
        )

    def build_system(self, context: str | None = None) -> str:
        """Prompt de sistema + perfil e memoria recuperada, quando houver.

        O contexto entra DEPOIS da identidade e antes de tudo mais: e material
        de consulta, nao instrucao de comportamento.
        """
        if not context or not context.strip():
            return self.system_prompt
        return f"{self.system_prompt}\n\n{context.strip()}"

    async def run(
        self,
        user_text: str,
        *,
        history: list[dict[str, Any]] | None = None,
        context: str | None = None,
        on_text: TextSink | None = None,
        correlation_id: str | None = None,
    ) -> AgentResult:
        """Roda o turno completo. ``on_text`` recebe os deltas para o TTS."""
        sistema = self.build_system(context)
        mensagens: list[dict[str, Any]] = [*(history or []), {"role": "user", "content": user_text}]
        esquemas = self._montar_ferramentas()
        chamadas: list[ToolCall] = []
        turno: LLMTurn | None = None

        for rodada in range(1, self._settings.llm_max_rounds + 1):
            try:
                turno = await self._client.stream_turn(
                    system=sistema,
                    messages=mensagens,
                    tools=esquemas,
                    on_text=on_text,
                )
            except LLMError as exc:
                log.error("agente.falha_no_modelo", rodada=rodada, erro=str(exc))
                return AgentResult(
                    text="Nao consigo alcancar meu modelo agora.",
                    rounds=rodada,
                    tool_calls=chamadas,
                    erro=str(exc),
                )

            if turno.stop_reason == "pause_turn":
                # Ferramenta do servidor (busca web) atingiu o limite de rodadas
                # do lado de la. Reenviar continua de onde parou - nao e erro.
                log.debug("agente.pause_turn", rodada=rodada)
                mensagens.append({"role": "assistant", "content": turno.assistant_content})
                continue

            if turno.recusou:
                log.warning("agente.recusa", rodada=rodada)
                return AgentResult(
                    text=turno.text or "Nao posso ajudar com isso.",
                    rounds=rodada,
                    tool_calls=chamadas,
                    stop_reason=turno.stop_reason,
                )

            if not turno.quer_ferramenta:
                return AgentResult(
                    text=turno.text,
                    rounds=rodada,
                    tool_calls=chamadas,
                    stop_reason=turno.stop_reason,
                )

            chamadas.extend(turno.tool_calls)
            mensagens.append({"role": "assistant", "content": turno.assistant_content})
            resultados = await self._executar(turno.tool_calls, correlation_id)
            # Descarta ANTES de anexar: limpa as imagens das rodadas anteriores,
            # que o modelo ja viu, e preserva a que acabou de chegar. Assim no
            # maximo uma imagem trafega por rodada, em vez de todas de novo.
            descartadas = _descartar_imagens(mensagens)
            if descartadas:
                log.debug("agente.imagens_descartadas", quantidade=descartadas, rodada=rodada)
            mensagens.append({"role": "user", "content": resultados})

        log.warning("agente.rodadas_esgotadas", limite=self._settings.llm_max_rounds)
        return AgentResult(
            text=(turno.text if turno else "") or "Travei tentando resolver isso.",
            rounds=self._settings.llm_max_rounds,
            tool_calls=chamadas,
            stop_reason=turno.stop_reason if turno else None,
            truncado=True,
        )

    def _montar_ferramentas(self) -> list[dict[str, Any]] | None:
        """Ferramentas do registro + as que o provedor executa no servidor."""
        cliente = self._tools.schemas() if self._tools is not None else []
        servidor = self._client.server_tools()
        return [*cliente, *servidor] or None

    async def _executar(
        self, chamadas: list[ToolCall], correlation_id: str | None
    ) -> list[dict[str, Any]]:
        """Executa as ferramentas em paralelo e devolve todos os resultados juntos.

        Um unico bloco com TODOS os ``tool_result`` - dividir em mensagens
        separadas ensina o modelo a parar de pedir chamadas paralelas.
        """
        resultados = await asyncio.gather(
            *(self._executar_uma(c, correlation_id) for c in chamadas)
        )
        return list(resultados)

    async def _executar_uma(
        self, chamada: ToolCall, correlation_id: str | None
    ) -> dict[str, Any]:
        if self._tools is None:
            saida = ToolOutcome("Nenhuma ferramenta registrada neste build.", is_error=True)
        else:
            try:
                saida = await self._tools.execute(
                    chamada.name, chamada.input, correlation_id=correlation_id
                )
            except Exception as exc:  # noqa: BLE001 - ferramenta ruim nao mata o turno
                log.error("agente.ferramenta_explodiu", ferramenta=chamada.name, erro=str(exc))
                saida = ToolOutcome(f"Erro: {type(exc).__name__}: {exc}", is_error=True)

        await self._emitir(
            "ferramenta.executada",
            {
                "ferramenta": chamada.name,
                "erro": saida.is_error,
                "correlation_id": correlation_id,
            },
        )
        imagens = tuple(getattr(saida, "imagens", ()) or ())
        if imagens:
            # Texto primeiro, imagem depois: o texto diz o que a imagem e, e o
            # modelo le na ordem. Um bloco de imagem solto chega sem contexto.
            conteudo: Any = [
                {"type": "text", "text": saida.content},
                *(imagem.bloco() for imagem in imagens),
            ]
            log.info(
                "agente.ferramenta_devolveu_imagem",
                ferramenta=chamada.name,
                quantidade=len(imagens),
                tokens_estimados=sum(i.tokens_estimados for i in imagens),
            )
        else:
            conteudo = saida.content

        return {
            "type": "tool_result",
            "tool_use_id": chamada.id,
            "content": conteudo,
            "is_error": saida.is_error,
        }

    async def _emitir(self, tipo: str, payload: dict[str, Any]) -> None:
        if self._on_event is not None:
            await self._on_event(tipo, payload)


def _descartar_imagens(mensagens: list[dict[str, Any]]) -> int:
    """Troca imagens ja analisadas por um marcador de texto. Devolve quantas.

    Uma imagem so precisa chegar ao modelo uma vez: ele olha, conclui, e a
    conclusao fica no texto dele. Reenviar o mesmo quadro a cada rodada paga
    de novo por uma informacao que ja foi extraida - com ``llm_max_rounds=6``,
    ate cinco vezes.

    O marcador nao e enfeite. Sem ele o modelo afirma "vejo uma caneca azul" e
    na rodada seguinte nao ha nada que justifique a afirmacao; com ele, a
    conversa continua coerente. O detalhe do que foi capturado nao se perde
    porque continua no texto da propria ferramenta, que fica intacto.

    Nao mexe em ``history``: aquilo ja e so texto (``WorkingMemory`` guarda
    string), e imagem nenhuma sobrevive ao fim do turno.
    """
    trocadas = 0
    for mensagem in mensagens:
        blocos = mensagem.get("content")
        if not isinstance(blocos, list):
            continue
        for bloco in blocos:
            if not isinstance(bloco, dict) or bloco.get("type") != "tool_result":
                continue
            conteudo = bloco.get("content")
            if not isinstance(conteudo, list):
                continue
            novo: list[Any] = []
            for parte in conteudo:
                if isinstance(parte, dict) and parte.get("type") == "image":
                    novo.append({"type": "text", "text": MARCADOR_IMAGEM})
                    trocadas += 1
                else:
                    novo.append(parte)
            bloco["content"] = novo
    return trocadas
