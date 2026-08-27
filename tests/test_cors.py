"""O preflight do navegador — a classe de defeito que os outros 558 nao pegam.

Em 27/08/2026 o HUD parou de conversar com producao. O sintoma era enganoso:
"LINK OK" no rodape e "Nao alcancei o Optmus" no chat, ao mesmo tempo. Os dois
estavam certos.

``/health/live`` e GET simples, sem header customizado - **nao dispara
preflight**. O ``/chat`` manda ``Authorization`` e ``X-Optmus-Dispositivo``, e
dispara. O ``allow_headers`` do CORS nao listava o segundo, o preflight voltou
400, o navegador bloqueou a chamada antes de ela sair, e o `fetch` rejeitou com
``TypeError`` - que o cliente classifica como falha de rede, porque o navegador
nao distingue CORS de DNS de offline.

**Por que a suite inteira passou com o bug dentro:** nenhum teste passava por um
navegador. ``TestClient`` e ``httpx.ASGITransport`` chamam a aplicacao direto e
nao fazem preflight. O contrato que quebrou so existe entre navegador e
servidor, entao so um teste que o encene pega.

A lista abaixo e a fonte da verdade deste arquivo: **todo header que o frontend
manda tem de estar aqui**. Quando o cliente ganhar um header novo, este teste e
o lugar que avisa.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from main import app

# Espelha o que `src/api/optmus.ts` envia hoje:
#   - Content-Type: quando ha corpo
#   - Authorization: `Bearer <token>` em toda rota autenticada
#   - X-Optmus-Dispositivo: identidade do aparelho (Achado Serio 2)
#
# Em minusculas porque e assim que o navegador manda no
# `Access-Control-Request-Headers`.
CABECALHOS_DO_NAVEGADOR = ["content-type", "authorization", "x-optmus-dispositivo"]

ORIGEM = "http://localhost:5174"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://core.local") as c:
            yield c


async def _preflight(client: AsyncClient, rota: str, cabecalhos: list[str]):
    return await client.options(
        rota,
        headers={
            "Origin": ORIGEM,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": ",".join(cabecalhos),
        },
    )


@pytest.mark.parametrize("rota", ["/chat", "/seguranca/confirmar", "/seguranca/dispositivos"])
async def test_o_preflight_do_navegador_passa(client: AsyncClient, rota: str) -> None:
    """A requisicao que o navegador REALMENTE manda antes do POST.

    Com o defeito de 27/08 este teste devolvia 400 e o chat ficava mudo.
    """
    resposta = await _preflight(client, rota, CABECALHOS_DO_NAVEGADOR)

    assert resposta.status_code == 200, (
        f"preflight recusado em {rota}: o navegador bloqueia a chamada e o "
        f"erro chega ao usuario como 'falha de rede'"
    )
    assert resposta.headers.get("access-control-allow-origin") == ORIGEM


async def test_todo_cabecalho_do_cliente_esta_liberado(client: AsyncClient) -> None:
    """Nomeia o header que falta, em vez de so dizer 'preflight falhou'.

    A mensagem importa: o modo de falha desta classe de defeito e um erro de
    rede generico do outro lado, e quem investiga precisa saber QUAL header.
    """
    resposta = await _preflight(client, "/chat", CABECALHOS_DO_NAVEGADOR)
    liberados = {
        h.strip().lower()
        for h in resposta.headers.get("access-control-allow-headers", "").split(",")
    }

    faltando = [h for h in CABECALHOS_DO_NAVEGADOR if h not in liberados]
    assert not faltando, f"o CORS nao libera: {faltando}"


async def test_controle_o_preflight_simples_ja_passava(client: AsyncClient) -> None:
    """Controle que impede o teste de passar por engano.

    Sem ele, um CORS quebrado de outro jeito (origem errada, metodo ausente)
    faria os testes acima falharem e pareceria o mesmo defeito. Este isola: a
    parte antiga do preflight sempre funcionou.
    """
    resposta = await _preflight(client, "/chat", ["content-type", "authorization"])
    assert resposta.status_code == 200


async def test_origem_desconhecida_continua_recusada(client: AsyncClient) -> None:
    """Liberar um header nao pode virar liberar qualquer site.

    Sem esta guarda, a correcao mais rapida para o bug de 27/08 seria
    `allow_headers=["*"]` com `allow_origins=["*"]` junto - e ai qualquer pagina
    aberta no seu navegador falaria com o Core usando o seu token.
    """
    resposta = await client.options(
        "/chat",
        headers={
            "Origin": "https://site-qualquer.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert resposta.headers.get("access-control-allow-origin") != "https://site-qualquer.example"


async def test_health_live_nao_precisa_de_preflight(client: AsyncClient) -> None:
    """Por que o rodape dizia LINK OK enquanto o chat falhava.

    GET sem header customizado e requisicao simples: o navegador manda direto,
    sem perguntar antes. Por isso a checagem de saude passava e so o /chat
    quebrava - dois sinais contraditorios que eram os dois verdadeiros.
    """
    resposta = await client.get("/health/live", headers={"Origin": ORIGEM})

    assert resposta.status_code == 200
    assert resposta.headers.get("access-control-allow-origin") == ORIGEM
