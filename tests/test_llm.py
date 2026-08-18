"""Selecao de cerebro e degradacao graciosa."""

from __future__ import annotations

import pytest

from core.config import MissingConfigError, Settings, get_settings, reset_settings_cache
from core.llm import AnthropicClient, NullLLMClient, OllamaClient, escolher_cliente


async def test_sem_chave_e_sem_ollama_falha_com_nome_da_variavel(settings: Settings) -> None:
    with pytest.raises(MissingConfigError) as exc:
        await escolher_cliente(settings)
    assert "OPTMUS_ANTHROPIC_API_KEY" in str(exc.value)


async def test_com_chave_escolhe_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTMUS_ANTHROPIC_API_KEY", "sk-teste-nao-usada")
    reset_settings_cache()
    cliente = await escolher_cliente(get_settings())
    assert isinstance(cliente, AnthropicClient)
    assert cliente.name == "anthropic"


async def test_sem_chave_cai_no_ollama_quando_ha_servidor(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def disponivel(self: OllamaClient) -> bool:
        return True

    monkeypatch.setattr(OllamaClient, "available", disponivel)
    cliente = await escolher_cliente(settings)
    assert cliente.name == "ollama"


async def test_cliente_nulo_responde_texto_falavel() -> None:
    cliente = NullLLMClient()
    recebidos: list[str] = []

    async def coletar(delta: str) -> None:
        recebidos.append(delta)

    turno = await cliente.stream_turn(system="", messages=[], on_text=coletar)
    assert turno.text
    assert "".join(recebidos) == turno.text
    assert not turno.quer_ferramenta


async def test_anthropic_sem_chave_falha_com_erro_de_config(settings: Settings) -> None:
    with pytest.raises(MissingConfigError):
        await AnthropicClient(settings).stream_turn(system="x", messages=[])
