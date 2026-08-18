"""Consolidador noturno e memoria procedural."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.config import Settings
from core.llm import LLMTurn
from memory.consolidator import Consolidator, ConsolidatorScheduler, _parse_fatos
from memory.embeddings import HashingEmbedder
from memory.procedural import ProceduralMemory
from memory.store import Store
from memory.system import MemorySystem
from tests.fakes import FakeLLM


@pytest.fixture
def memoria(settings: Settings, store: Store) -> MemorySystem:
    return MemorySystem(settings, store, embedder=HashingEmbedder(settings.embedding_dim))


def _consolidador(settings, memoria, llm=None, bus=None) -> Consolidator:
    return Consolidator(
        settings,
        episodic=memoria.episodic,
        semantic=memoria.semantic,
        procedural=memoria.procedural,
        llm=llm,
        bus=bus,
    )


# -------------------------------------------------------------- extracao
async def test_extrai_fato_durador_do_dia(settings: Settings, memoria: MemorySystem) -> None:
    await memoria.episodic.record_exchange("meu contador e o Ricardo", "Anotado.")
    llm = FakeLLM([
        LLMTurn(text='[{"fato": "o contador do usuario e Ricardo", "confianca": 0.9}]')
    ])

    resultado = await _consolidador(settings, memoria, llm).run()

    assert resultado.episodios == 1
    assert resultado.fatos_novos == 1
    assert [linha["content"] for linha in await memoria.semantic.vigentes()] == [
        "o contador do usuario e Ricardo"
    ]


async def test_nao_regrava_fato_que_ja_sabe(settings: Settings, memoria: MemorySystem) -> None:
    await memoria.semantic.remember("o contador do usuario e Ricardo")
    await memoria.episodic.record_exchange("meu contador e o Ricardo", "Anotado.")
    llm = FakeLLM([LLMTurn(text='[{"fato": "o contador do usuario e Ricardo"}]')])

    resultado = await _consolidador(settings, memoria, llm).run()
    assert resultado.fatos_novos == 0
    assert resultado.fatos_repetidos == 1


async def test_episodio_consolidado_nao_volta_na_proxima_noite(
    settings: Settings, memoria: MemorySystem
) -> None:
    await memoria.episodic.record("qualquer coisa")
    consolidador = _consolidador(settings, memoria, FakeLLM([LLMTurn(text="[]")]))

    assert (await consolidador.run()).episodios == 1
    assert (await consolidador.run()).episodios == 0


async def test_sem_cerebro_ainda_consolida_o_resto(
    settings: Settings, memoria: MemorySystem
) -> None:
    await memoria.episodic.record("qualquer coisa")
    resultado = await _consolidador(settings, memoria, llm=None).run()

    assert resultado.erro is None
    assert resultado.episodios == 1
    assert resultado.fatos_novos == 0


async def test_json_sujo_nao_derruba_a_consolidacao(
    settings: Settings, memoria: MemorySystem
) -> None:
    await memoria.episodic.record("qualquer coisa")
    llm = FakeLLM([LLMTurn(text="Claro! Aqui: [{'fato': quebrado}] espero ter ajudado")])

    resultado = await _consolidador(settings, memoria, llm).run()
    assert resultado.erro is None
    assert resultado.fatos_novos == 0


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ('[{"fato": "x", "confianca": 0.5}]', 1),
        ('texto antes [{"fato": "x"}] texto depois', 1),
        ("[]", 0),
        ("sem json nenhum", 0),
        ('[{"sem_campo_fato": true}]', 0),
    ],
)
def test_parse_de_fatos_e_tolerante(texto: str, esperado: int) -> None:
    assert len(_parse_fatos(texto)) == esperado


# ------------------------------------------------------------- procedural
def _episodio(conteudo: str, quando: datetime) -> dict[str, object]:
    return {"content": conteudo, "created_at": quando.isoformat(), "id": 0}


def test_rotina_exige_repeticao_em_semanas_distintas() -> None:
    procedural = ProceduralMemory(store=None, min_ocorrencias=3)  # type: ignore[arg-type]
    sexta = datetime(2026, 8, 7, 18, 0, tzinfo=UTC)

    mesma_semana = [_episodio("me manda o resumo da semana", sexta) for _ in range(4)]
    assert procedural.detectar(mesma_semana) == [], "4 pedidos na mesma sexta e tarefa, nao habito"

    tres_semanas = [
        _episodio("me manda o resumo da semana", sexta - timedelta(weeks=n)) for n in range(3)
    ]
    rotinas = procedural.detectar(tres_semanas)
    assert len(rotinas) == 1
    assert rotinas[0].dia_da_semana == 4
    assert "toda sexta" in rotinas[0].descrever()


def test_pedidos_diferentes_nao_viram_a_mesma_rotina() -> None:
    procedural = ProceduralMemory(store=None, min_ocorrencias=3)  # type: ignore[arg-type]
    sexta = datetime(2026, 8, 7, 18, 0, tzinfo=UTC)
    episodios = [
        _episodio("me manda o resumo da semana", sexta - timedelta(weeks=n)) for n in range(3)
    ] + [_episodio("abre o youtube", sexta - timedelta(weeks=n)) for n in range(3)]

    assinaturas = {r.assinatura for r in procedural.detectar(episodios)}
    assert len(assinaturas) == 2


def test_frases_equivalentes_caem_na_mesma_assinatura() -> None:
    procedural = ProceduralMemory(store=None, min_ocorrencias=2)  # type: ignore[arg-type]
    sexta = datetime(2026, 8, 7, 18, 0, tzinfo=UTC)
    episodios = [
        _episodio("me manda o resumo da semana", sexta),
        _episodio("manda o resumo da semana ai", sexta - timedelta(weeks=1)),
    ]
    assert len(procedural.detectar(episodios)) == 1


async def test_rotina_detectada_e_gravada_uma_vez_so(
    settings: Settings, memoria: MemorySystem
) -> None:
    sexta = datetime.now(UTC) - timedelta(days=(datetime.now(UTC).weekday() - 4) % 7)
    for n in range(3):
        await memoria.episodic.store.insert_memory(
            layer="episodica",
            content="me manda o resumo da semana -> Aqui esta.",
            source="conversa",
            created_at=(sexta - timedelta(weeks=n)).isoformat(timespec="milliseconds"),
        )

    # O consolidador so roda quando ha episodio novo do dia - a deteccao de
    # rotina olha 60 dias para tras, mas o gatilho e o que aconteceu hoje.
    await memoria.episodic.record("algo aconteceu hoje")

    consolidador = _consolidador(settings, memoria)
    assert (await consolidador.run()).rotinas_novas == 1
    assert len(await memoria.procedural.rotinas()) == 1

    await memoria.episodic.record("outra coisa qualquer")
    assert (await consolidador.run()).rotinas_novas == 0, "rotina ja conhecida nao duplica"


async def test_sem_episodio_novo_o_consolidador_nao_faz_nada(
    settings: Settings, memoria: MemorySystem
) -> None:
    """Nada aconteceu hoje: nao ha o que digerir, e a passada e barata."""
    resultado = await _consolidador(settings, memoria).run()
    assert resultado.episodios == 0
    assert resultado.rotinas_novas == 0


# -------------------------------------------------------------- agendador
def test_agendador_calcula_a_proxima_madrugada(settings: Settings, memoria: MemorySystem) -> None:
    agendador = ConsolidatorScheduler(_consolidador(settings, memoria), hora=4)

    manha = datetime(2026, 8, 11, 9, 0)
    assert agendador.segundos_ate_proxima(manha) == pytest.approx(19 * 3600, abs=1)

    madrugada = datetime(2026, 8, 11, 3, 0)
    assert agendador.segundos_ate_proxima(madrugada) == pytest.approx(3600, abs=1)


async def test_execucao_publica_evento(settings: Settings, memoria: MemorySystem, bus) -> None:
    recebidos: list[object] = []
    await bus.subscribe("memoria.*", recebidos.append, name="espiao")

    agendador = ConsolidatorScheduler(_consolidador(settings, memoria, bus=bus), hora=4, bus=bus)
    await memoria.episodic.record("algo aconteceu")
    await agendador.executar()

    for _ in range(200):
        if recebidos:
            break
        await _tick()
    assert [e.type for e in recebidos] == ["memoria.consolidada"]  # type: ignore[attr-defined]
    assert agendador.stats()["execucoes"] == 1


async def _tick() -> None:
    import asyncio

    await asyncio.sleep(0.005)
