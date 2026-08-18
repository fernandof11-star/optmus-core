"""Fixtures compartilhadas.

Os testes sao hermeticos: ignoram o ``.env`` do desenvolvedor, limpam
qualquer ``OPTMUS_*`` herdado do ambiente e usam banco em ``tmp_path``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from core.bus import InProcessEventBus
from core.config import Settings, get_settings, reset_settings_cache
from core.llm import OllamaClient
from core.logging import configure_logging
from memory.store import Store

SECRET_DE_TESTE = "chave-de-teste-suficientemente-longa-para-passar-0123456789"


@pytest.fixture(autouse=True)
def ambiente_isolado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for chave in [k for k in os.environ if k.startswith("OPTMUS_")]:
        monkeypatch.delenv(chave, raising=False)

    # Ignora o .env real: a suite nao pode depender da maquina de quem roda.
    monkeypatch.setitem(Settings.model_config, "env_file", None)

    monkeypatch.setenv("OPTMUS_ENV", "dev")
    monkeypatch.setenv("OPTMUS_SECRET_KEY", SECRET_DE_TESTE)
    monkeypatch.setenv("OPTMUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPTMUS_DB_PATH", str(tmp_path / "data" / "teste.db"))
    monkeypatch.setenv("OPTMUS_LOG_LEVEL", "warning")

    # A suite nao toca a rede: sem isto, todo boot sonda o Ollama local e paga
    # o timeout de conexao na maquina de quem roda.
    monkeypatch.setattr(OllamaClient, "available", _sempre_indisponivel)

    reset_settings_cache()
    configure_logging(level="warning", json_output=False)
    yield
    reset_settings_cache()


async def _sempre_indisponivel(self: OllamaClient) -> bool:
    return False


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
async def store(settings: Settings) -> AsyncIterator[Store]:
    st = await Store(settings.database_path, embedding_dim=settings.embedding_dim).connect()
    await st.migrate()
    yield st
    await st.close()


@pytest.fixture
async def bus(store: Store) -> AsyncIterator[InProcessEventBus]:
    b = InProcessEventBus(store, queue_maxsize=64)
    await b.start()
    yield b
    await b.stop()
