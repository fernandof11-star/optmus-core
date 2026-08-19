"""Autenticacao da API - o que separa "local" de "exposto na internet"."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.config import ConfigError, Settings, get_settings, reset_settings_cache
from security.api_auth import TokenAuthMiddleware, exposto_na_rede, verificar_exposicao

TOKEN = "token-de-teste-bem-comprido-0123456789"


def _app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.add_middleware(TokenAuthMiddleware, settings=settings)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "vivo"}

    @app.get("/memoria/buscar")
    async def privado() -> dict[str, str]:
        return {"segredo": "memoria do usuario"}

    return app


async def _cliente(settings: Settings) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=_app(settings)), base_url="http://core")


@pytest.fixture
def com_token(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("OPTMUS_API_TOKEN", TOKEN)
    reset_settings_cache()
    return get_settings()


# ------------------------------------------------------------------ middleware
async def test_sem_token_a_rota_privada_e_negada(com_token: Settings) -> None:
    async with await _cliente(com_token) as cliente:
        resposta = await cliente.get("/memoria/buscar")
    assert resposta.status_code == 401
    assert resposta.headers["www-authenticate"] == "Bearer"


async def test_token_certo_libera(com_token: Settings) -> None:
    async with await _cliente(com_token) as cliente:
        resposta = await cliente.get(
            "/memoria/buscar", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert resposta.status_code == 200


@pytest.mark.parametrize(
    "cabecalho",
    ["Bearer errado", f"Basic {TOKEN}", TOKEN, "Bearer ", ""],
)
async def test_cabecalho_malformado_ou_errado_e_negado(
    com_token: Settings, cabecalho: str
) -> None:
    async with await _cliente(com_token) as cliente:
        resposta = await cliente.get("/memoria/buscar", headers={"Authorization": cabecalho})
    assert resposta.status_code == 401


async def test_liveness_fica_publica(com_token: Settings) -> None:
    """O healthcheck do orquestrador nao deve carregar credencial."""
    async with await _cliente(com_token) as cliente:
        resposta = await cliente.get("/health/live")
    assert resposta.status_code == 200


async def test_sem_token_configurado_nao_ha_barreira(settings: Settings) -> None:
    """Local, em 127.0.0.1: quem esta na maquina ja tem tudo mesmo."""
    async with await _cliente(settings) as cliente:
        resposta = await cliente.get("/memoria/buscar")
    assert resposta.status_code == 200


# --------------------------------------------------------------- exposicao
def test_host_local_nao_e_exposto(settings: Settings) -> None:
    assert exposto_na_rede(settings) is False
    verificar_exposicao(settings)  # nao levanta


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5"])
def test_host_publico_sem_token_recusa_subir(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """Protecao que depende de alguem lembrar de ligar nao e protecao."""
    monkeypatch.setenv("OPTMUS_HTTP_HOST", host)
    reset_settings_cache()
    with pytest.raises(ConfigError, match="OPTMUS_API_TOKEN"):
        verificar_exposicao(get_settings())


def test_producao_sem_token_recusa_subir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTMUS_ENV", "prod")
    reset_settings_cache()
    with pytest.raises(ConfigError, match="OPTMUS_API_TOKEN"):
        verificar_exposicao(get_settings())


def test_host_publico_com_token_sobe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTMUS_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("OPTMUS_API_TOKEN", TOKEN)
    reset_settings_cache()
    verificar_exposicao(get_settings())


def test_porta_aceita_a_variavel_PORT(monkeypatch: pytest.MonkeyPatch) -> None:
    """Railway, Render e Fly injetam PORT sem prefixo."""
    monkeypatch.delenv("OPTMUS_HTTP_PORT", raising=False)
    monkeypatch.setenv("PORT", "9123")
    reset_settings_cache()
    assert get_settings().http_port == 9123


# ------------------------------------------- o buraco de 2026-08-18 (producao)
def _ambiente_de_producao(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduz o que a Railway entregava: PORT injetado e mais nada.

    Nem OPTMUS_ENV nem OPTMUS_HTTP_HOST chegavam ao processo - estavam no
    Dockerfile, e o builder em uso nao lia o Dockerfile. Sobravam os padroes:
    env=dev e http_host=127.0.0.1.
    """
    for chave in ("OPTMUS_API_TOKEN", "OPTMUS_ENV", "OPTMUS_HTTP_HOST"):
        monkeypatch.delenv(chave, raising=False)
    monkeypatch.setenv("PORT", "8080")
    reset_settings_cache()


def test_plataforma_sem_token_recusa_subir(monkeypatch: pytest.MonkeyPatch) -> None:
    """O Core ficou publico e sem autenticacao por causa deste caminho.

    Com PORT injetado, o processo esta hospedado - o uvicorn e iniciado com
    --host 0.0.0.0 pela plataforma, independentemente do que a config acredita.
    Antes, http_host=127.0.0.1 e env=dev faziam o guarda liberar a subida, e o
    middleware se desativava sozinho por nao ter token: API aberta na internet.
    """
    _ambiente_de_producao(monkeypatch)
    settings = get_settings()

    assert settings.env.value == "dev", "reproduz o estado real: ninguem setou OPTMUS_ENV"
    assert settings.http_host == "127.0.0.1", "e a config acreditava ser local"

    with pytest.raises(ConfigError, match="OPTMUS_API_TOKEN"):
        verificar_exposicao(settings)


def test_plataforma_e_considerada_exposicao(monkeypatch: pytest.MonkeyPatch) -> None:
    """PORT injetado significa hospedado, mesmo com http_host local."""
    _ambiente_de_producao(monkeypatch)
    assert exposto_na_rede(get_settings()) is True


def test_plataforma_com_token_sobe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A correcao nao pode impedir um deploy legitimo de subir."""
    _ambiente_de_producao(monkeypatch)
    monkeypatch.setenv("OPTMUS_API_TOKEN", TOKEN)
    reset_settings_cache()
    verificar_exposicao(get_settings())


async def test_token_falso_e_negado_com_a_api_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """O teste manual que expos o problema, agora automatizado.

    Foi com a string literal "SEU_OPTMUS_API_TOKEN_AQUI" - um placeholder de
    documentacao - que /chat respondeu 200 em producao.
    """
    monkeypatch.setenv("OPTMUS_API_TOKEN", TOKEN)
    reset_settings_cache()
    settings = get_settings()

    async with await _cliente(settings) as cliente:
        resposta = await cliente.get(
            "/memoria/buscar", headers={"Authorization": "Bearer SEU_OPTMUS_API_TOKEN_AQUI"}
        )
    assert resposta.status_code == 401


def test_local_de_verdade_continua_sem_exigir_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A correcao falha fechada, mas nao pode atrapalhar o uso local.

    Sem sinal de plataforma e em 127.0.0.1, o Core sobe sem token como sempre.
    """
    for chave in ("OPTMUS_API_TOKEN", "OPTMUS_ENV", "OPTMUS_HTTP_HOST"):
        monkeypatch.delenv(chave, raising=False)
    for marca in ("PORT", "RAILWAY_ENVIRONMENT", "RENDER", "DYNO"):
        monkeypatch.delenv(marca, raising=False)
    reset_settings_cache()

    assert exposto_na_rede(get_settings()) is False
    verificar_exposicao(get_settings())  # nao levanta
