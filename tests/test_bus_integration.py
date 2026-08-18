"""Criterio de aceite da F0: evento publicado chega ao consumidor e e persistido."""

from __future__ import annotations

import asyncio

import pytest

from core.bus import Event, InProcessEventBus, Recorder
from memory.store import Store


async def _esperar(condicao, timeout: float = 2.0) -> None:
    """Espera ativa curta - handlers rodam em task propria."""
    limite = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < limite:
        if condicao():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condicao nao satisfeita dentro do timeout")


async def test_evento_chega_ao_consumidor_e_e_persistido(
    bus: InProcessEventBus, store: Store
) -> None:
    recebidos: list[Event] = []

    async def consumidor(event: Event) -> None:
        recebidos.append(event)

    await bus.subscribe("voz.*", consumidor, name="consumidor")

    publicado = await bus.emit(
        "voz.transcricao",
        source="stt",
        payload={"texto": "que horas sao", "duracao_ms": 412},
        correlation_id="corr-1",
    )

    await _esperar(lambda: len(recebidos) == 1)

    assert recebidos[0].id == publicado.id
    assert recebidos[0].payload["texto"] == "que horas sao"

    linhas = await store.recent_events(limit=10)
    persistido = next(linha for linha in linhas if linha["id"] == publicado.id)
    assert persistido["type"] == "voz.transcricao"
    assert persistido["source"] == "stt"
    assert persistido["correlation_id"] == "corr-1"
    assert persistido["payload"] == {"texto": "que horas sao", "duracao_ms": 412}


async def test_pattern_filtra_assinantes(bus: InProcessEventBus) -> None:
    voz: list[Event] = []
    tudo: list[Event] = []

    await bus.subscribe("voz.*", voz.append, name="so-voz")
    await bus.subscribe("*", tudo.append, name="tudo")

    await bus.emit("voz.wake", source="wake", payload={})
    await bus.emit("dispositivo.offline", source="mesh", payload={"id": "vermelho"})

    await _esperar(lambda: len(tudo) == 2)
    await asyncio.sleep(0.02)

    assert [e.type for e in voz] == ["voz.wake"]
    assert [e.type for e in tudo] == ["voz.wake", "dispositivo.offline"]


async def test_handler_quebrado_nao_derruba_os_outros(bus: InProcessEventBus) -> None:
    ok: list[Event] = []

    def explode(event: Event) -> None:
        raise RuntimeError("handler ruim")

    await bus.subscribe("*", explode, name="ruim")
    await bus.subscribe("*", ok.append, name="bom")

    await bus.emit("sistema.teste", source="teste", payload={"n": 1})
    await bus.emit("sistema.teste", source="teste", payload={"n": 2})

    await _esperar(lambda: len(ok) == 2)
    await _esperar(lambda: bus.stats_dict()["erros_handler"] == 2)
    assert bus.running


async def test_unsubscribe_para_de_receber(bus: InProcessEventBus) -> None:
    recebidos: list[Event] = []
    sub = await bus.subscribe("*", recebidos.append, name="temporario")

    await bus.emit("sistema.teste", source="teste", payload={})
    await _esperar(lambda: len(recebidos) == 1)

    await sub.unsubscribe()
    await bus.emit("sistema.teste", source="teste", payload={})
    await asyncio.sleep(0.05)

    assert len(recebidos) == 1


async def test_fila_cheia_descarta_o_mais_antigo_sem_travar(store: Store) -> None:
    b = InProcessEventBus(store, queue_maxsize=2)
    await b.start()
    liberar = asyncio.Event()

    async def lento(event: Event) -> None:
        await liberar.wait()

    await b.subscribe("*", lento, name="lento")
    try:
        for i in range(10):
            await asyncio.wait_for(
                b.emit("sistema.carga", source="teste", payload={"i": i}, persist=False),
                timeout=1.0,
            )
        assert b.stats_dict()["descartados"] > 0
    finally:
        liberar.set()
        await b.stop()


async def test_persist_false_nao_escreve_no_banco(bus: InProcessEventBus, store: Store) -> None:
    antes = await store.count_events()
    await bus.emit("audio.amplitude", source="mic", payload={"rms": 0.42}, persist=False)
    await asyncio.sleep(0.02)
    assert await store.count_events() == antes


async def test_recorder_respeita_o_limite(bus: InProcessEventBus) -> None:
    recorder = Recorder(limite=3)
    await bus.subscribe("*", recorder, name="recorder")

    for i in range(6):
        await bus.emit("sistema.teste", source="teste", payload={"i": i})

    # espera pela ENTREGA dos 6: len == 3 acontece transitoriamente no 3o evento
    await _esperar(lambda: bus.stats_dict()["entregues"] == 6)
    assert [e.payload["i"] for e in recorder.eventos] == [3, 4, 5]


@pytest.mark.parametrize(
    ("pattern", "tipo", "casa"),
    [
        ("*", "qualquer.coisa", True),
        ("voz.*", "voz.wake", True),
        ("voz.*", "dispositivo.wake", False),
        ("dispositivo.*", "dispositivo.tela.espelhada", True),
    ],
)
async def test_matching_de_pattern(
    bus: InProcessEventBus, pattern: str, tipo: str, casa: bool
) -> None:
    recebidos: list[Event] = []
    await bus.subscribe(pattern, recebidos.append, name="p")
    await bus.emit(tipo, source="teste", payload={})
    await asyncio.sleep(0.05)
    assert bool(recebidos) is casa
