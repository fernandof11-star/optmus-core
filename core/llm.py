"""Clientes de LLM com streaming.

Contrato unico (:class:`LLMClient`) para dois backends:

- :class:`AnthropicClient` - Claude via SDK oficial, tool use nativo. Primario.
- :class:`OllamaClient` - modelo local, sem ferramentas. Modo offline.

Streaming nao e enfeite: o TTS comeca a falar na primeira frase pronta, muito
antes de a resposta terminar de ser gerada. Sem isso, a latencia percebida vira
a latencia da resposta inteira e o sistema deixa de parecer vivo (secao 3.6).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from core.config import MissingConfigError, Settings
from core.logging import get_logger

log = get_logger("core.llm")

TextSink = Callable[[str], Awaitable[None]]

# Modelos Claude atuais recusam parametros de sampling e budget de thinking.
# Thinking fica ADAPTATIVO com effort baixo - ver comentario em config.py.
_THINKING_ADAPTATIVO: dict[str, str] = {"type": "adaptive"}


class LLMError(RuntimeError):
    """Falha ao falar com o modelo."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Imagem:
    """Uma imagem que uma ferramenta devolve para o modelo ver.

    Mora aqui, e nao em ``tools/``, porque e formato de fio: e o desenho do
    bloco que a API aceita dentro de um ``tool_result``, do mesmo jeito que
    ``ToolCall`` e o desenho do bloco que chega de volta.

    ``dados_b64`` NAO e logado nem persistido em lugar nenhum. Um quadro de
    webcam num SQLite sem prazo de validade e um vazamento esperando data.
    """

    dados_b64: str
    media_type: str = "image/jpeg"
    largura: int = 0
    altura: int = 0
    origem: str = "ferramenta"

    @property
    def tokens_estimados(self) -> int:
        """Aproximacao da conta da Anthropic: (largura x altura) / 750."""
        if not self.largura or not self.altura:
            return 0
        return round(self.largura * self.altura / 750)

    def bloco(self) -> dict[str, Any]:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": self.media_type,
                "data": self.dados_b64,
            },
        }



@dataclass(slots=True)
class LLMTurn:
    """Resultado de uma rodada do modelo."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None
    assistant_content: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None

    @property
    def quer_ferramenta(self) -> bool:
        return bool(self.tool_calls)

    @property
    def recusou(self) -> bool:
        return self.stop_reason == "refusal"


class LLMClient(ABC):
    """Contrato do cerebro. Trocar de backend nao muda o loop de agente."""

    name: str

    @abstractmethod
    async def available(self) -> bool: ...

    def server_tools(self) -> list[dict[str, Any]]:
        """Ferramentas que o proprio provedor executa. Vazio por padrao."""
        return []

    @abstractmethod
    async def stream_turn(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: TextSink | None = None,
    ) -> LLMTurn: ...


class AnthropicClient(LLMClient):
    """Claude via SDK oficial, com tool use nativo e streaming."""

    name = "anthropic"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any = None

    async def available(self) -> bool:
        return self._settings.anthropic_api_key is not None

    def server_tools(self) -> list[dict[str, Any]]:
        """Ferramentas executadas no servidor da Anthropic.

        A busca web entra por aqui em vez de virar uma ferramenta cliente: nao
        exige chave de outro fornecedor, ja volta com citacao, e o resultado nao
        passa por este processo. O custo e que ela so existe com o cerebro na
        nuvem - offline nao ha busca web, e isso e honesto.
        """
        if not self._settings.web_search_enabled:
            return []
        return [
            {
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": self._settings.web_search_max_uses,
            }
        ]

    def _ensure(self) -> Any:
        if self._client is not None:
            return self._client
        self._settings.require("anthropic_api_key", subsystem="cerebro (anthropic)")
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - depende do extra "llm"
            raise LLMError('SDK ausente: pip install -e ".[llm]"') from exc

        assert self._settings.anthropic_api_key is not None
        self._client = AsyncAnthropic(
            api_key=self._settings.anthropic_api_key.get_secret_value()
        )
        return self._client

    async def stream_turn(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: TextSink | None = None,
    ) -> LLMTurn:
        client = self._ensure()
        params: dict[str, Any] = {
            "model": self._settings.anthropic_model,
            "max_tokens": self._settings.llm_max_tokens,
            "system": system,
            "messages": messages,
            "thinking": _THINKING_ADAPTATIVO,
            "output_config": {"effort": self._settings.llm_effort},
        }
        if tools:
            params["tools"] = tools

        try:
            async with client.messages.stream(**params) as stream:
                async for evento in stream:
                    if (
                        on_text is not None
                        and evento.type == "content_block_delta"
                        and evento.delta.type == "text_delta"
                    ):
                        await on_text(evento.delta.text)
                final = await stream.get_final_message()
        except Exception as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc

        return _turno_do_anthropic(final)


def _turno_do_anthropic(final: Any) -> LLMTurn:
    texto: list[str] = []
    chamadas: list[ToolCall] = []
    conteudo: list[dict[str, Any]] = []

    for bloco in final.content:
        conteudo.append(bloco.model_dump(exclude_none=True))
        if bloco.type == "text":
            texto.append(bloco.text)
        elif bloco.type == "tool_use":
            chamadas.append(ToolCall(id=bloco.id, name=bloco.name, input=dict(bloco.input)))

    return LLMTurn(
        text="".join(texto),
        tool_calls=chamadas,
        stop_reason=final.stop_reason,
        assistant_content=conteudo,
        model=final.model,
    )


class OllamaClient(LLMClient):
    """Fallback local. Sem ferramentas - responde texto e so."""

    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def available(self) -> bool:
        import httpx

        # Timeout curto: isto roda no boot e nao pode segurar a subida do Core.
        try:
            async with httpx.AsyncClient(timeout=0.8) as http:
                resposta = await http.get(f"{self._settings.ollama_base_url}/api/tags")
                return resposta.status_code == 200
        except Exception:  # noqa: BLE001 - indisponivel e resposta valida aqui
            return False

    async def stream_turn(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: TextSink | None = None,
    ) -> LLMTurn:
        import httpx

        if tools:
            log.warning("ollama.ferramentas_ignoradas", quantidade=len(tools))

        corpo = {
            "model": self._settings.ollama_model,
            "stream": True,
            "messages": [{"role": "system", "content": system}, *_texto_puro(messages)],
        }
        partes: list[str] = []
        try:
            async with httpx.AsyncClient(timeout=60.0) as http:
                async with http.stream(
                    "POST", f"{self._settings.ollama_base_url}/api/chat", json=corpo
                ) as resposta:
                    resposta.raise_for_status()
                    async for linha in resposta.aiter_lines():
                        if not linha.strip():
                            continue
                        dado = json.loads(linha)
                        pedaco = dado.get("message", {}).get("content", "")
                        if pedaco:
                            partes.append(pedaco)
                            if on_text is not None:
                                await on_text(pedaco)
        except Exception as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc

        return LLMTurn(
            text="".join(partes),
            stop_reason="end_turn",
            model=self._settings.ollama_model,
        )


def _texto_puro(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Achata blocos de conteudo para o formato simples do Ollama."""
    saida: list[dict[str, str]] = []
    for msg in messages:
        conteudo = msg.get("content", "")
        if isinstance(conteudo, list):
            conteudo = " ".join(
                b.get("text", "") for b in conteudo if isinstance(b, dict) and b.get("text")
            )
        if conteudo:
            saida.append({"role": str(msg.get("role", "user")), "content": str(conteudo)})
    return saida


class NullLLMClient(LLMClient):
    """Sem cerebro configurado. Responde e avisa, em vez de derrubar o processo."""

    name = "nenhum"
    RESPOSTA = "Estou sem cerebro configurado. Falta a chave da API no ambiente."

    async def available(self) -> bool:
        return False

    async def stream_turn(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: TextSink | None = None,
    ) -> LLMTurn:
        if on_text is not None:
            await on_text(self.RESPOSTA)
        return LLMTurn(text=self.RESPOSTA, stop_reason="end_turn", model=self.name)


async def escolher_cliente(settings: Settings) -> LLMClient:
    """Anthropic quando ha chave; Ollama quando ha servidor local; senao falha.

    Degradacao graciosa (secao 3.5): sem internet, modo local.
    """
    anthropic = AnthropicClient(settings)
    if await anthropic.available():
        return anthropic

    ollama = OllamaClient(settings)
    if await ollama.available():
        log.warning("llm.fallback_local", motivo="OPTMUS_ANTHROPIC_API_KEY ausente")
        return ollama

    raise MissingConfigError("cerebro", ["anthropic_api_key"])
