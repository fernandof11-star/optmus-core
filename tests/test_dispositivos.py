"""Vinculo entre quem pede e quem confirma.

O achado que isto fecha: ``POST /seguranca/confirmar`` aceitava o token de
qualquer um que tivesse o ``OPTMUS_API_TOKEN``. A tela dizia "um humano
autorizou"; o Core sabia apenas "alguem com o token da API autorizou".

O teste que carrega o peso e
:func:`test_token_da_api_sozinho_nao_confirma_nada` - ele encena exatamente o
atacante que motivou a mudanca.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from main import app
from security.dispositivos import (
    ORIGEM_DESCONHECIDA,
    ORIGEM_VOZ,
    DispositivoDesconhecido,
    IdJaRegistrado,
    ProvaInvalida,
    RegistroDeDispositivos,
    origem,
    origem_atual,
    prova,
)
from security.policy import Pendente, PolicyEngine, RiskLevel


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """App com o lifespan real - o registro de dispositivos vive nele."""
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://core.local") as c:
            yield c


HUD = "hud-7f3a"
CELULAR = "celular-91bb"
SEGREDO = "a" * 44
OUTRO = "b" * 44


# ------------------------------------------------------------- o registro
async def test_id_registrado_nao_pode_ser_tomado(store) -> None:
    """A recusa que sustenta o mecanismo inteiro.

    Sem ela, quem tivesse o token da API sequestraria o id do HUD apenas
    registrando-o de novo com um segredo proprio - e a prova HMAC passaria a
    ser calculada com a chave do atacante. Todo o resto viraria enfeite.
    """
    registro = RegistroDeDispositivos(store)
    await registro.registrar(HUD, SEGREDO)

    with pytest.raises(IdJaRegistrado):
        await registro.registrar(HUD, OUTRO)

    # E o segredo original continua valendo.
    await registro.verificar(HUD, "confirmar", "tok", prova(SEGREDO, "confirmar", "tok"))


async def test_registrar_de_novo_com_o_mesmo_segredo_e_idempotente(store) -> None:
    """O HUD registra a cada carga da pagina - nao pode virar erro."""
    registro = RegistroDeDispositivos(store)
    primeira = await registro.registrar(HUD, SEGREDO)
    segunda = await registro.registrar(HUD, SEGREDO)

    assert primeira["registrado"] is True
    assert segunda["registrado"] is False and segunda["ja_existia"] is True


async def test_prova_de_outro_dispositivo_nao_serve(store) -> None:
    registro = RegistroDeDispositivos(store)
    await registro.registrar(HUD, SEGREDO)
    await registro.registrar(CELULAR, OUTRO)

    with pytest.raises(ProvaInvalida):
        await registro.verificar(HUD, "confirmar", "tok", prova(OUTRO, "confirmar", "tok"))


async def test_prova_de_recusar_nao_confirma(store) -> None:
    """A acao entra no HMAC de proposito.

    Sem ela, uma prova capturada para recusar serviria para confirmar - e sao
    decisoes opostas, que nao podem compartilhar credencial.
    """
    registro = RegistroDeDispositivos(store)
    await registro.registrar(HUD, SEGREDO)

    with pytest.raises(ProvaInvalida):
        await registro.verificar(HUD, "confirmar", "tok", prova(SEGREDO, "recusar", "tok"))


async def test_prova_de_outra_pendencia_nao_serve(store) -> None:
    """O token entra no HMAC: a prova vale para UMA acao, nao para o dispositivo."""
    registro = RegistroDeDispositivos(store)
    await registro.registrar(HUD, SEGREDO)

    with pytest.raises(ProvaInvalida):
        await registro.verificar(HUD, "confirmar", "tok-B", prova(SEGREDO, "confirmar", "tok-A"))


async def test_dispositivo_nunca_visto_e_recusado(store) -> None:
    with pytest.raises(DispositivoDesconhecido):
        await RegistroDeDispositivos(store).verificar(HUD, "confirmar", "t", "x" * 64)


async def test_esquecer_revoga(store) -> None:
    """Navegador perdido: revogar precisa cortar de verdade."""
    registro = RegistroDeDispositivos(store)
    await registro.registrar(HUD, SEGREDO)
    assert await registro.esquecer(HUD) is True

    with pytest.raises(DispositivoDesconhecido):
        await registro.verificar(HUD, "confirmar", "t", prova(SEGREDO, "confirmar", "t"))


async def test_segredo_nunca_aparece_na_listagem(store) -> None:
    """`listar` alimenta uma resposta HTTP."""
    registro = RegistroDeDispositivos(store)
    await registro.registrar(HUD, SEGREDO)

    assert SEGREDO not in str(await registro.listar())


# -------------------------------------------------------------- a origem
def test_origem_nao_vaza_de_um_turno_para_o_outro() -> None:
    """O laco de voz e uma tarefa longa e viva.

    Um ``set`` sem ``reset`` deixaria a origem do turno anterior colada no
    proximo, e a pendencia sairia carimbada com o dispositivo errado - o pior
    resultado possivel num mecanismo cujo unico proposito e dizer quem pediu.
    """
    assert origem_atual() == ORIGEM_DESCONHECIDA
    with origem(HUD):
        assert origem_atual() == HUD
    assert origem_atual() == ORIGEM_DESCONHECIDA


def test_origem_aninhada_volta_para_a_anterior() -> None:
    with origem(HUD):
        with origem(CELULAR):
            assert origem_atual() == CELULAR
        assert origem_atual() == HUD


async def test_pendencia_nasce_carimbada(settings, store) -> None:
    """Carimbada no nascimento: depois de criada nao ha como saber a origem."""
    motor = PolicyEngine(settings, store)
    with origem(HUD):
        decisao = await motor.avaliar(
            ferramenta="olhar", risco=RiskLevel.EXTERNO, parametros={}, resumo="olhar"
        )

    assert motor.pendentes(para=HUD)[0]["origem"] == HUD
    assert decisao.token is not None


# ------------------------------------------------- quem pode confirmar o que
def _pendente(origem_: str) -> Pendente:
    return Pendente(
        token="tok", ferramenta="olhar", parametros={}, risco=RiskLevel.EXTERNO,
        resumo="olhar", origem=origem_,
    )


def test_so_quem_pediu_confirma() -> None:
    assert _pendente(HUD).confirmavel_por(HUD) is True
    assert _pendente(HUD).confirmavel_por(CELULAR) is False


def test_pedido_por_voz_pode_ser_confirmado_na_tela() -> None:
    """Abertura declarada, e nao esquecimento.

    O microfone nao produz HMAC, e hoje NAO existe confirmacao falada - o
    registro devolve "a confirmacao chega por fora", e "por fora" e a tela.
    Fechar isto tornaria toda acao externa pedida por voz impossivel de
    autorizar, que e pior que a abertura.
    """
    assert _pendente(ORIGEM_VOZ).confirmavel_por(HUD) is True
    assert _pendente(ORIGEM_DESCONHECIDA).confirmavel_por(HUD) is True


async def test_pendencia_de_outro_dispositivo_nao_aparece_na_lista(settings, store) -> None:
    """Cartao que o aparelho nao consegue autorizar so gera clique com erro -
    e mostra a um aparelho o que o outro esta fazendo."""
    motor = PolicyEngine(settings, store)
    with origem(CELULAR):
        await motor.avaliar(
            ferramenta="olhar", risco=RiskLevel.EXTERNO, parametros={}, resumo="x"
        )

    assert motor.pendentes(para=CELULAR) != []
    assert motor.pendentes(para=HUD) == []
    assert motor.pendentes() != [], "sem filtro, a visao de diagnostico ve tudo"


async def test_confirmar_de_outro_dispositivo_e_recusado(settings, store) -> None:
    motor = PolicyEngine(settings, store)
    with origem(CELULAR):
        decisao = await motor.avaliar(
            ferramenta="olhar", risco=RiskLevel.EXTERNO, parametros={}, resumo="x"
        )
    assert decisao.token is not None

    with pytest.raises(PermissionError, match="outro dispositivo"):
        motor.confirmar(decisao.token, dispositivo=HUD)


# -------------------------------------------------------- ponta a ponta HTTP
async def _pendencia_via_http(client: AsyncClient, de: str) -> str:
    registro = app.state.ferramentas
    with origem(de):
        decisao = await registro.policy.avaliar(
            ferramenta="olhar",
            risco=RiskLevel.EXTERNO,
            parametros={"modo": "descrever"},
            resumo="ligar a webcam",
        )
    assert decisao.token is not None
    return decisao.token


async def test_token_da_api_sozinho_nao_confirma_nada(client: AsyncClient) -> None:
    """O atacante que motivou toda esta mudanca.

    Ele tem o ``OPTMUS_API_TOKEN`` - o `client` da suite fala com a API
    normalmente. Antes, isso bastava para confirmar qualquer acao pendente.
    Agora ele nao tem o segredo do dispositivo, e sem ele nao ha prova.
    """
    await client.post(
        "/seguranca/dispositivos", json={"dispositivo": HUD, "segredo": SEGREDO}
    )
    token = await _pendencia_via_http(client, HUD)

    # Sem prova nenhuma: o corpo nem e aceito.
    assert (await client.post("/seguranca/confirmar", json={"token": token})).status_code == 422

    # Com prova chutada:
    resposta = await client.post(
        "/seguranca/confirmar",
        json={"token": token, "dispositivo": HUD, "prova": "0" * 64},
    )
    assert resposta.status_code == 403

    # Inventando um dispositivo que nunca registrou:
    resposta = await client.post(
        "/seguranca/confirmar",
        json={
            "token": token,
            "dispositivo": "dispositivo-do-atacante",
            "prova": prova(OUTRO, "confirmar", token),
        },
    )
    assert resposta.status_code == 403

    # E a pendencia continua de pe, esperando o dono.
    assert any(
        p["token"] == token for p in (await client.get("/seguranca/pendentes")).json()
    )


async def test_dono_do_segredo_confirma(client: AsyncClient) -> None:
    await client.post(
        "/seguranca/dispositivos", json={"dispositivo": HUD, "segredo": SEGREDO}
    )
    token = await _pendencia_via_http(client, HUD)

    resposta = await client.post(
        "/seguranca/confirmar",
        json={
            "token": token,
            "dispositivo": HUD,
            "prova": prova(SEGREDO, "confirmar", token),
        },
    )
    assert resposta.status_code == 200
    # A camera nao existe no ambiente de teste; o que importa e ter PASSADO
    # pelo portao - erro de execucao vem depois da autorizacao.
    assert "resultado" in resposta.json()


async def test_dispositivo_certo_pendencia_do_outro(client: AsyncClient) -> None:
    """Prova valida nao basta: ela responde "quem e voce", nao "voce pode isto"."""
    for nome, seg in ((HUD, SEGREDO), (CELULAR, OUTRO)):
        await client.post(
            "/seguranca/dispositivos", json={"dispositivo": nome, "segredo": seg}
        )
    token = await _pendencia_via_http(client, CELULAR)

    resposta = await client.post(
        "/seguranca/confirmar",
        json={
            "token": token,
            "dispositivo": HUD,
            "prova": prova(SEGREDO, "confirmar", token),
        },
    )
    assert resposta.status_code == 400 or resposta.json().get("executado") is False
    assert "outro dispositivo" in str(resposta.json())


async def test_id_tomado_devolve_409(client: AsyncClient) -> None:
    await client.post(
        "/seguranca/dispositivos", json={"dispositivo": HUD, "segredo": SEGREDO}
    )
    resposta = await client.post(
        "/seguranca/dispositivos", json={"dispositivo": HUD, "segredo": OUTRO}
    )
    assert resposta.status_code == 409


async def test_chat_carimba_a_pendencia_com_o_header(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O caminho principal do HUD, e o que quase passou batido.

    Sem o carimbo no ``/chat``, toda pendencia nascida do HUD sai como
    ``desconhecida`` - e ``desconhecida`` e confirmavel por QUALQUER
    dispositivo. A garantia continuaria de pe no papel e valeria zero na
    pratica.

    O que importa e a origem valendo DURANTE o turno, porque e la dentro que a
    pendencia nasce. Verificar so antes e depois nao prova nada: com o carimbo
    removido, os dois extremos continuam limpos.
    """
    from core.voice_loop import VoiceLoop
    from security.dispositivos import _origem

    vista: list[str] = []
    original = VoiceLoop.handle_text

    async def espiao(self, texto, **kw):
        vista.append(origem_atual())
        return await original(self, texto, **kw)

    monkeypatch.setattr(VoiceLoop, "handle_text", espiao)

    assert _origem.get() == ORIGEM_DESCONHECIDA
    await client.post(
        "/chat", json={"mensagem": "oi"}, headers={"X-Optmus-Dispositivo": HUD}
    )

    assert vista == [HUD], "a origem precisa valer enquanto o turno roda"
    assert _origem.get() == ORIGEM_DESCONHECIDA, "e nao sobrar depois"


async def test_chat_sem_header_nasce_sem_dono(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cliente que nao se identifica cria pendencia sem dono.

    E permitido - script e curl precisam funcionar -, mas e o ponto fraco
    declarado: pendencia sem dono aceita confirmacao de qualquer dispositivo
    registrado. Por isso o HUD sempre manda o header.
    """
    from core.voice_loop import VoiceLoop

    vista: list[str] = []
    original = VoiceLoop.handle_text

    async def espiao(self, texto, **kw):
        vista.append(origem_atual())
        return await original(self, texto, **kw)

    monkeypatch.setattr(VoiceLoop, "handle_text", espiao)
    await client.post("/chat", json={"mensagem": "oi"})

    assert vista == [ORIGEM_DESCONHECIDA]
