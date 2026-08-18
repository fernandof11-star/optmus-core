"""API HTTP da F0: healthcheck e injecao de evento ponta a ponta."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from main import app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """Sobe o app com o lifespan real (store + bus de verdade)."""
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://core.local") as c:
            yield c


async def test_health_reporta_estado_real(client: AsyncClient) -> None:
    resposta = await client.get("/health")
    assert resposta.status_code == 200
    corpo = resposta.json()

    assert corpo["status"] in {"ok", "degradado"}
    assert corpo["barramento"]["ativo"] is True
    assert "recorder:*" in corpo["barramento"]["assinantes"]
    assert corpo["barramento"]["publicados"] >= 1
    assert corpo["persistencia"]["path"].endswith("teste.db")
    assert 1 in corpo["persistencia"]["migrations_aplicadas"]
    # o evento sistema.iniciado do lifespan ja esta gravado
    assert corpo["persistencia"]["eventos"] >= 1


async def test_health_aponta_config_pendente_das_fases_futuras(client: AsyncClient) -> None:
    corpo = (await client.get("/health")).json()
    assert "OPTMUS_WEB_BASE_URL" in corpo["config_pendente"]["F3_optmus_web"]


async def test_publicar_evento_persiste_e_aparece_no_recent(client: AsyncClient) -> None:
    resposta = await client.post(
        "/events",
        json={
            "type": "sistema.teste",
            "payload": {"origem": "suite"},
            "source": "pytest",
            "correlation_id": "corr-api",
        },
    )
    assert resposta.status_code == 202
    evento = resposta.json()
    assert evento["type"] == "sistema.teste"

    recentes = (await client.get("/events/recent", params={"limit": 20})).json()
    encontrado = next(linha for linha in recentes if linha["id"] == evento["id"])
    assert encontrado["payload"] == {"origem": "suite"}
    assert encontrado["correlation_id"] == "corr-api"


async def test_recent_filtra_por_tipo(client: AsyncClient) -> None:
    await client.post("/events", json={"type": "voz.wake", "payload": {}})
    await client.post("/events", json={"type": "dispositivo.online", "payload": {}})

    somente_voz = (await client.get("/events/recent", params={"type": "voz.wake"})).json()
    assert somente_voz
    assert {linha["type"] for linha in somente_voz} == {"voz.wake"}


async def test_evento_invalido_e_recusado(client: AsyncClient) -> None:
    assert (await client.post("/events", json={"type": "", "payload": {}})).status_code == 422
    assert (await client.get("/events/recent", params={"limit": 0})).status_code == 422


async def test_voz_texto_responde_pela_camada_1(client: AsyncClient) -> None:
    """Sem chave de API e sem microfone, "que horas sao" ainda responde."""
    corpo = (await client.post("/voz/texto", json={"texto": "que horas sao"})).json()
    assert corpo["camada"] == "deterministica"
    assert corpo["regra"] == "hora"
    assert corpo["resposta"]
    assert corpo["latencia"]["total_ms"] > 0


async def test_voz_texto_sem_cerebro_avisa_em_vez_de_quebrar(client: AsyncClient) -> None:
    corpo = (await client.post("/voz/texto", json={"texto": "quanto gastei esse mes"})).json()
    assert corpo["camada"] == "llm"
    assert "cerebro" in corpo["resposta"].lower()


async def test_health_expoe_estado_da_voz(client: AsyncClient) -> None:
    corpo = (await client.get("/health")).json()
    assert corpo["voz"]["cerebro"] == "nenhum"
    assert corpo["voz"]["escutando"] is False
    assert "sem cerebro: so a camada 1 do roteador responde" in corpo["degradacoes"]


async def test_metrics_acumula_turnos(client: AsyncClient) -> None:
    await client.post("/voz/texto", json={"texto": "que horas sao"})
    resumo = (await client.get("/metrics")).json()
    assert resumo["turnos"] >= 1
    assert "etapa.router" in resumo["series"]


async def test_kill_switch_pela_api(client: AsyncClient) -> None:
    assert (await client.post("/sistema/parar")).json() == {"status": "parado"}
    eventos = (await client.get("/events/recent", params={"type": "sistema.parar"})).json()
    assert eventos


async def test_texto_vazio_e_recusado(client: AsyncClient) -> None:
    assert (await client.post("/voz/texto", json={"texto": ""})).status_code == 422


async def test_gatilho_sem_escuta_recusa_em_vez_de_aceitar_calado(
    client: AsyncClient,
) -> None:
    """202 sem escuta montada seria mentira: nenhum turno rodaria, nenhum log sairia."""
    resposta = await client.post("/voz/gatilho")
    assert resposta.status_code == 409
    assert "voz/texto" in resposta.json()["detail"]
