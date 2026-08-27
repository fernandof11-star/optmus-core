"""Instagram: dado ausente que continua ausente, e o token de 60 dias.

Os dois riscos desta integracao nao sao de entrega, sao de honestidade e de
prazo: a Meta devolve conjunto vazio onde se espera um numero, e o token morre
em 60 dias sem poder ser ressuscitado.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from core.config import Settings, get_settings, reset_settings_cache
from integrations.instagram import (
    CHAVE_EXPIRA,
    CHAVE_TOKEN,
    InstagramClient,
    InstagramError,
)
from memory.store import Store
from security.policy import RiskLevel
from tools.impl.instagram import InstagramComentariosTool, InstagramResumoTool


@pytest.fixture
def ig_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("OPTMUS_INSTAGRAM_TOKEN", "IGQ-token-do-env")
    monkeypatch.setenv("OPTMUS_INSTAGRAM_ACCOUNT_ID", "17841400000000000")
    reset_settings_cache()
    return get_settings()


def _mock(monkeypatch: pytest.MonkeyPatch, rotas: dict[str, Any]) -> list[httpx.Request]:
    """Roteia por trecho do caminho. Valor pode ser dict ou callable."""
    vistas: list[httpx.Request] = []
    original = httpx.AsyncClient.__init__

    def _init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        def _captura(request: httpx.Request) -> httpx.Response:
            vistas.append(request)
            for trecho, resposta in rotas.items():
                if trecho in request.url.path:
                    corpo = resposta(request) if callable(resposta) else resposta
                    if isinstance(corpo, httpx.Response):
                        return corpo
                    return httpx.Response(200, json=corpo)
            return httpx.Response(404, json={"error": {"message": f"sem rota: {request.url.path}"}})

        kwargs["transport"] = httpx.MockTransport(_captura)
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)
    return vistas


PERFIL = {"username": "eu", "followers_count": 100, "follows_count": 50, "media_count": 7}


def _insights(**valores: int | None) -> dict[str, Any]:
    return {
        "data": [
            {"name": nome, "total_value": {"value": v}}
            for nome, v in valores.items()
            if v is not None
        ]
    }


# ------------------------------------------------------ dado que nao existe
async def test_metrica_ausente_vira_travessao_e_nunca_zero(
    monkeypatch: pytest.MonkeyPatch, ig_settings: Settings, store: Store
) -> None:
    """A Meta devolve conjunto VAZIO quando a metrica nao tem dado, nao 0.

    Traduzir ausencia para zero seria afirmar "seu alcance hoje foi zero" -
    uma medicao que ninguem fez. O usuario tomaria decisao sobre um numero
    inventado, que e exatamente o que este projeto nao faz.
    """
    _mock(monkeypatch, {
        "/me/insights": _insights(reach=42),  # so reach; as outras somem
        "/me": PERFIL,
    })
    resultado = await InstagramResumoTool(ig_settings, store).execute()

    assert "alcance: 42" in resultado.content
    assert "visualizacoes: —" in resultado.content
    assert "visualizacoes: 0" not in resultado.content
    assert resultado.metadata["metricas"]["views"] is None, "None no metadado, nao 0"


async def test_primeira_leitura_nao_afirma_que_ninguem_seguiu(
    monkeypatch: pytest.MonkeyPatch, ig_settings: Settings, store: Store
) -> None:
    """Sem leitura anterior nao ha variacao - e "+0" seria uma afirmacao."""
    _mock(monkeypatch, {"/me/insights": _insights(reach=1), "/me": PERFIL})
    resultado = await InstagramResumoTool(ig_settings, store).execute()

    assert "sem base de comparacao" in resultado.content
    assert "+0" not in resultado.content
    assert resultado.metadata["delta"] is None


async def test_segunda_leitura_compara_com_a_primeira(
    monkeypatch: pytest.MonkeyPatch, ig_settings: Settings, store: Store
) -> None:
    perfis = [PERFIL, {**PERFIL, "followers_count": 103}]
    _mock(monkeypatch, {
        "/me/insights": _insights(reach=1),
        "/me": lambda _: perfis.pop(0) if len(perfis) > 1 else perfis[0],
    })
    ferramenta = InstagramResumoTool(ig_settings, store)

    await ferramenta.execute()
    segunda = await ferramenta.execute()

    assert segunda.metadata["delta"] == 3
    assert "+3" in segunda.content


async def test_diz_que_nao_sabe_quem_seguiu(
    monkeypatch: pytest.MonkeyPatch, ig_settings: Settings, store: Store
) -> None:
    """O limite da API oficial vai no texto que o modelo le.

    Sem isso o modelo preenche a lacuna: perguntado "quem me seguiu?", ele tem
    um numero na mao e nenhuma indicacao de que a lista nao existe - e inventa
    um nome. O aviso e a unica coisa que impede isso.
    """
    _mock(monkeypatch, {"/me/insights": _insights(reach=1), "/me": PERFIL})
    resultado = await InstagramResumoTool(ig_settings, store).execute()

    assert "nao informa quem seguiu" in resultado.content
    assert "nem permite seguir de volta" in resultado.content


async def test_metrica_presente_mas_sem_valor_tambem_e_ausencia(
    monkeypatch: pytest.MonkeyPatch, ig_settings: Settings, store: Store
) -> None:
    """O segundo caminho para "sem dado", e ele NAO passa pelo mesmo codigo.

    A Meta tem duas formas de nao responder uma metrica: omiti-la do `data`,
    ou devolve-la com `total_value.value` nulo. A primeira e resolvida pelo
    default do dicionario; esta aqui passa pelo laco. Um teste que so cobrisse
    a primeira deixaria esta linha livre para virar `else 0` sem ninguem notar
    - foi exatamente o que a injecao de defeito revelou.
    """
    _mock(monkeypatch, {
        "/me/insights": {
            "data": [
                {"name": "reach", "total_value": {"value": 5}},
                {"name": "views", "total_value": {"value": None}},
                {"name": "total_interactions", "total_value": {}},
            ]
        },
        "/me": PERFIL,
    })
    resultado = await InstagramResumoTool(ig_settings, store).execute()

    assert resultado.metadata["metricas"]["views"] is None
    assert resultado.metadata["metricas"]["total_interactions"] is None
    assert "visualizacoes: —" in resultado.content
    assert "interacoes: —" in resultado.content


# ----------------------------------------------------------------- token
async def test_token_guardado_tem_precedencia_sobre_o_env(
    monkeypatch: pytest.MonkeyPatch, ig_settings: Settings, store: Store
) -> None:
    """Depois da primeira renovacao, o token do `.env` esta velho.

    Continuar lendo dele seria usar credencial vencida tendo uma valida
    guardada ao lado - e o sintoma apareceria so 60 dias depois.
    """
    await store.meta_set(CHAVE_TOKEN, "IGQ-token-renovado")
    await store.meta_set(CHAVE_EXPIRA, (datetime.now(UTC) + timedelta(days=59)).isoformat())

    vistas = _mock(monkeypatch, {"/me": PERFIL})
    await InstagramClient(ig_settings, store).perfil()

    assert "IGQ-token-renovado" in str(vistas[0].url)
    assert "IGQ-token-do-env" not in str(vistas[0].url)


async def test_nao_renova_token_novo(
    monkeypatch: pytest.MonkeyPatch, ig_settings: Settings, store: Store
) -> None:
    """Renovar todo dia gastaria chamada e resetaria o prazo sem necessidade."""
    await store.meta_set(CHAVE_EXPIRA, (datetime.now(UTC) + timedelta(days=59)).isoformat())
    vistas = _mock(monkeypatch, {"/refresh_access_token": {"access_token": "x"}})

    r = await InstagramClient(ig_settings, store).renovar_se_preciso()

    assert r["renovado"] is False
    assert vistas == [], "nem bateu na Meta"


async def test_renova_quando_o_prazo_aperta(
    monkeypatch: pytest.MonkeyPatch, ig_settings: Settings, store: Store
) -> None:
    """Com folga de 10 dias: esperar o ultimo dia perderia a janela se o Core
    ficasse desligado na semana errada, e token vencido NAO se renova."""
    await store.meta_set(CHAVE_EXPIRA, (datetime.now(UTC) + timedelta(days=3)).isoformat())
    _mock(monkeypatch, {
        "/refresh_access_token": {"access_token": "IGQ-novo", "expires_in": 60 * 86400}
    })

    r = await InstagramClient(ig_settings, store).renovar_se_preciso()

    assert r["renovado"] is True
    assert await store.meta_get(CHAVE_TOKEN) == "IGQ-novo"
    assert r["dias_restantes"] >= 59


async def test_prazo_desconhecido_nao_vira_sessenta(
    ig_settings: Settings, store: Store
) -> None:
    """A Meta nao diz a validade de um token que ela nao acabou de emitir.

    Mostrar 60 dias para o token do `.env` seria inventar uma data - e o
    usuario confiaria nela justamente para decidir quando agir.
    """
    assert await InstagramClient(ig_settings, store).dias_restantes() is None


async def test_renovacao_falhada_nao_derruba_a_leitura(
    monkeypatch: pytest.MonkeyPatch, ig_settings: Settings, store: Store
) -> None:
    """"Token com menos de 24 h" e o caso NORMAL logo depois de configurar.

    Se a manutencao derrubasse a leitura, a integracao nasceria quebrada e
    voltaria a funcionar sozinha no dia seguinte - o sintoma mais confuso
    possivel.
    """
    _mock(monkeypatch, {
        "/refresh_access_token": httpx.Response(
            400, json={"error": {"message": "token is not old enough", "code": 190}}
        ),
        "/me/insights": _insights(reach=7),
        "/me": PERFIL,
    })
    resultado = await InstagramResumoTool(ig_settings, store).execute()

    assert not resultado.is_error
    assert "alcance: 7" in resultado.content


async def test_avisa_quando_o_token_esta_para_vencer(
    monkeypatch: pytest.MonkeyPatch, ig_settings: Settings, store: Store
) -> None:
    """Token vencido nao ressuscita: o aviso tem que chegar ANTES.

    E o prazo e arredondado para BAIXO (3,96 dias vira "3"), de proposito:
    para um vencimento sem volta, subestimar faz agir cedo e superestimar faz
    agir tarde demais. So uma das duas falhas tem conserto.

    As 23 h no prazo existem para fugir da fronteira exata. Com `days=4` cravado
    o teste falhava 1 vez em 5: o relogio do Windows tem granularidade de ~15 ms,
    as duas leituras de `now()` caem no mesmo tique e a diferenca da 4 dias
    redondos em vez de 3,9999.
    """
    prazo = datetime.now(UTC) + timedelta(days=3, hours=23)
    await store.meta_set(CHAVE_EXPIRA, prazo.isoformat())
    _mock(monkeypatch, {
        "/refresh_access_token": httpx.Response(
            400, json={"error": {"message": "nope", "code": 190}}
        ),
        "/me/insights": _insights(reach=1),
        "/me": PERFIL,
    })
    resultado = await InstagramResumoTool(ig_settings, store).execute()

    assert "vence em 3 dias" in resultado.content
    assert "vence em 4 dias" not in resultado.content, "nunca para cima"


# ---------------------------------------------------------------- falhas
async def test_erro_da_meta_chega_literal(
    monkeypatch: pytest.MonkeyPatch, ig_settings: Settings, store: Store
) -> None:
    """"HTTP 400" nao diz o que consertar; a frase da Meta diz."""
    _mock(monkeypatch, {
        "/me": httpx.Response(
            400,
            json={"error": {"message": "Invalid OAuth access token", "code": 190}},
        )
    })
    resultado = await InstagramResumoTool(ig_settings, store).execute()

    assert resultado.is_error
    assert "Invalid OAuth access token" in resultado.content
    assert "190" in resultado.content


async def test_publicacao_ilegivel_nao_derruba_as_outras(
    monkeypatch: pytest.MonkeyPatch, ig_settings: Settings, store: Store
) -> None:
    """Meia leitura com o buraco marcado vale mais que erro total."""
    def _comentarios(request: httpx.Request) -> httpx.Response:
        if "/media-ruim/" in request.url.path:
            return httpx.Response(500, json={"error": {"message": "oops"}})
        return httpx.Response(200, json={"data": [{"username": "ana", "text": "top"}]})

    _mock(monkeypatch, {
        "/me/media": {"data": [{"id": "media-ruim"}, {"id": "media-boa", "caption": "praia"}]},
        "/comments": _comentarios,
    })
    resultado = await InstagramComentariosTool(ig_settings, store).execute()

    assert not resultado.is_error
    assert "@ana: top" in resultado.content, "a publicacao boa apareceu"
    assert "nao consegui ler" in resultado.content, "e o buraco esta marcado"


async def test_sem_comentarios_nao_inventa_lista(
    monkeypatch: pytest.MonkeyPatch, ig_settings: Settings, store: Store
) -> None:
    _mock(monkeypatch, {
        "/me/media": {"data": [{"id": "m1", "caption": "oi"}]},
        "/comments": {"data": []},
    })
    resultado = await InstagramComentariosTool(ig_settings, store).execute()

    assert "Nenhum comentario" in resultado.content
    assert resultado.metadata["comentarios"] == 0


# --------------------------------------------------- risco e disponibilidade
def test_leitura_nao_passa_pelo_portao(ig_settings: Settings, store: Store) -> None:
    """Nada sai da conta: nao publica, nao segue, nao responde.

    Exigir confirmacao para ler o proprio perfil ensinaria a confirmar por
    reflexo - e o reflexo e o que quebra o portao quando ele importar.
    """
    for tipo in (InstagramResumoTool, InstagramComentariosTool):
        ferramenta = tipo(ig_settings, store)
        assert ferramenta.risk is RiskLevel.LEITURA
        assert ferramenta.risk.ordem < RiskLevel.EXTERNO.ordem


async def test_sem_configuracao_some_do_schema(settings: Settings, store: Store) -> None:
    for tipo in (InstagramResumoTool, InstagramComentariosTool):
        assert await tipo(settings, store).available() is False


async def test_configurado_entra_no_schema(ig_settings: Settings, store: Store) -> None:
    for tipo in (InstagramResumoTool, InstagramComentariosTool):
        assert await tipo(ig_settings, store).available() is True


async def test_so_o_token_nao_basta(monkeypatch: pytest.MonkeyPatch, store: Store) -> None:
    monkeypatch.setenv("OPTMUS_INSTAGRAM_TOKEN", "IGQ-x")
    reset_settings_cache()
    ferramenta = InstagramResumoTool(get_settings(), store)

    assert await ferramenta.available() is False
    with pytest.raises(InstagramError, match="INSTAGRAM_ACCOUNT_ID"):
        await ferramenta.client.perfil()
