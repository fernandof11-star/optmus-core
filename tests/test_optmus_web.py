"""Ponte com o Optmus Web: contrato real, resiliencia e degradacao.

O contrato foi conferido contra a API em producao: token estatico
HMAC-SHA256(chave='jarvis-auth-v1', mensagem=senha), enviado como
``Authorization: Bearer``, com GETs de estatistica e ``POST /api/chat``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest

from core.config import Settings, get_settings, reset_settings_cache
from tools.impl.optmus_web import (
    INDICADORES,
    CircuitBreaker,
    OptmusWebChatTool,
    OptmusWebClient,
    OptmusWebTool,
    WebIndisponivel,
)

SENHA = "senha-de-teste"
TOKEN = hmac.new(b"jarvis-auth-v1", SENHA.encode(), hashlib.sha256).hexdigest()


@pytest.fixture
def web_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("OPTMUS_WEB_BASE_URL", "https://exemplo.vercel.app")
    monkeypatch.setenv("OPTMUS_WEB_PASSWORD", SENHA)
    reset_settings_cache()
    return get_settings()


def _mock_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    vistas: list[httpx.Request] = []
    original = httpx.AsyncClient.__init__

    def _init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        def _captura(request: httpx.Request) -> httpx.Response:
            vistas.append(request)
            return handler(request)

        kwargs["transport"] = httpx.MockTransport(_captura)
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)
    return vistas


# ------------------------------------------------------------------- token
def test_token_segue_o_contrato_do_web() -> None:
    """Chave fixa 'jarvis-auth-v1', mensagem = senha. Nao o contrario.

    Este teste pega a inversao chave/mensagem - o erro mais facil de cometer
    aqui, e que produz um token plausivel e completamente errado.
    """
    from tools.impl.optmus_web import CHAVE_HMAC

    assert CHAVE_HMAC == b"jarvis-auth-v1"
    assert TOKEN != hmac.new(SENHA.encode(), b"jarvis-auth-v1", hashlib.sha256).hexdigest()


def test_token_calculado_uma_vez_so(web_settings: Settings) -> None:
    cliente = OptmusWebClient(web_settings)
    assert cliente.token == TOKEN
    assert cliente.token is cliente.token


async def test_authorization_bearer_em_toda_chamada(
    web_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    vistas = _mock_transport(monkeypatch, lambda r: httpx.Response(200, json=[]))
    cliente = OptmusWebClient(web_settings)
    await cliente.indicador("financeiro_mensal")
    await cliente.indicador("tarefas")

    assert vistas[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert vistas[0].headers["authorization"] == vistas[1].headers["authorization"]
    assert "x-optmus-timestamp" not in vistas[0].headers


async def test_indicador_bate_na_rota_certa(
    web_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    vistas = _mock_transport(
        monkeypatch, lambda r: httpx.Response(200, json=[{"month": "2026-08"}])
    )
    await OptmusWebClient(web_settings).indicador("gastos_por_categoria")

    assert vistas[0].method == "GET"
    assert str(vistas[0].url).endswith("/api/stats/category-spending")


async def test_chat_manda_messages_e_extrai_o_texto(
    web_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    resposta = {
        "messages": [
            {"role": "user", "content": "quanto gastei"},
            {"role": "assistant", "content": [{"type": "text", "text": "Tres mil e duzentos."}]},
        ]
    }
    vistas = _mock_transport(monkeypatch, lambda r: httpx.Response(200, json=resposta))
    texto = await OptmusWebClient(web_settings).chat("quanto gastei")

    assert texto == "Tres mil e duzentos."
    assert str(vistas[0].url).endswith("/api/chat")
    assert json.loads(vistas[0].content)["messages"][0]["content"] == "quanto gastei"


async def test_chat_aceita_conteudo_em_texto_puro(
    web_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    resposta = {"messages": [{"role": "assistant", "content": "resposta simples"}]}
    _mock_transport(monkeypatch, lambda r: httpx.Response(200, json=resposta))
    assert await OptmusWebClient(web_settings).chat("oi") == "resposta simples"


# -------------------------------------------------------------------- risco
def test_consulta_e_leitura_chat_e_escrita(web_settings: Settings) -> None:
    """/api/chat pode gravar no Notion - rotular de LEITURA seria mentira."""
    assert OptmusWebTool(web_settings).risk.value == "LEITURA"
    assert OptmusWebChatTool(web_settings).risk.value == "ESCRITA"


async def test_indicador_invalido_e_recusado_antes_da_rede(web_settings: Settings) -> None:
    resultado = await OptmusWebTool(web_settings).execute(indicador="criptomoedas")
    assert resultado.is_error and "indicador invalido" in resultado.content


def test_todo_indicador_aponta_para_uma_rota() -> None:
    assert all(rota.startswith("/api/") for rota in INDICADORES.values())
    assert "financeiro_mensal" in INDICADORES


# --------------------------------------------------------------- resiliencia
async def test_erro_5xx_e_retentado(
    web_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cold start de serverless e normal, nao erro."""
    tentativas = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        tentativas["n"] += 1
        if tentativas["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=[{"total": 10}])

    _mock_transport(monkeypatch, handler)
    dados = await OptmusWebClient(web_settings).indicador("financeiro_mensal")
    assert dados == [{"total": 10}]
    assert tentativas["n"] == 2


async def test_401_tenta_login_uma_vez(
    web_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Se o esquema do token mudar, o login oficial e a rede de seguranca."""
    chamadas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        chamadas.append(caminho)
        if caminho == "/api/login":
            return httpx.Response(200, json={"token": "token-novo-do-servidor"})
        if chamadas.count("/api/stats/monthly") == 1:
            return httpx.Response(401)
        return httpx.Response(200, json=[{"ok": True}])

    _mock_transport(monkeypatch, handler)
    cliente = OptmusWebClient(web_settings)
    dados = await cliente.indicador("financeiro_mensal")

    assert dados == [{"ok": True}]
    assert "/api/login" in chamadas
    assert cliente.token == "token-novo-do-servidor"


async def test_401_persistente_desiste_com_mensagem_util(
    web_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return httpx.Response(200, json={"token": "outro"})
        return httpx.Response(401)

    _mock_transport(monkeypatch, handler)
    with pytest.raises(WebIndisponivel, match="autenticacao"):
        await OptmusWebClient(web_settings).indicador("financeiro_mensal")


async def test_senha_errada_no_login_e_dita_claramente(
    web_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_transport(monkeypatch, lambda r: httpx.Response(401, json={"error": "Senha incorreta."}))
    with pytest.raises(WebIndisponivel, match="OPTMUS_WEB_PASSWORD"):
        await OptmusWebClient(web_settings).login()


async def test_web_fora_nao_derruba_o_optmus(
    web_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sem rota para o host")

    _mock_transport(monkeypatch, handler)
    resultado = await OptmusWebTool(web_settings).execute(indicador="financeiro_mensal")

    assert resultado.is_error
    assert "Nao consigo alcancar meus dados" in resultado.content
    assert "sem inventar numeros" in resultado.content


async def test_circuito_abre_e_falha_rapido(
    web_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("caiu")

    vistas = _mock_transport(monkeypatch, handler)
    cliente = OptmusWebClient(web_settings)

    for _ in range(3):
        with pytest.raises(WebIndisponivel):
            await cliente.indicador("financeiro_mensal")

    antes = len(vistas)
    with pytest.raises(WebIndisponivel, match="circuito aberto"):
        await cliente.indicador("financeiro_mensal")
    assert len(vistas) == antes, "circuito aberto nao pode bater na rede"


def test_circuito_fecha_apos_sucesso() -> None:
    breaker = CircuitBreaker(limite_falhas=2)
    breaker.registrar_falha("x")
    breaker.registrar_falha("y")
    assert breaker.aberto

    breaker.registrar_sucesso()
    assert not breaker.aberto and breaker.falhas == 0


# ------------------------------------------------------------- configuracao
async def test_sem_configuracao_a_ferramenta_nao_e_oferecida(settings: Settings) -> None:
    assert await OptmusWebTool(settings).available() is False


async def test_diagnostico_nao_vaza_o_token(
    web_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_transport(monkeypatch, lambda r: httpx.Response(200, json=[]))
    info = await OptmusWebClient(web_settings).diagnostico()

    assert info["ok"] is True
    assert TOKEN not in json.dumps(info)
    assert SENHA not in json.dumps(info)
