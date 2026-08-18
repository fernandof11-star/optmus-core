"""Instrumentacao de latencia."""

from __future__ import annotations

import asyncio

from core.metrics import LatencyTracker, TurnMetrics


async def test_stage_mede_duracao() -> None:
    turno = TurnMetrics("t1")
    with turno.stage("stt"):
        await asyncio.sleep(0.02)
    assert turno.etapas["stt"] >= 15
    assert turno.total_ms >= turno.etapas["stt"]


async def test_mark_e_idempotente() -> None:
    turno = TurnMetrics("t1")
    await asyncio.sleep(0.01)
    primeiro = turno.mark("primeira_silaba")
    await asyncio.sleep(0.01)
    assert turno.mark("primeira_silaba") == primeiro


def test_tracker_resume_percentis() -> None:
    tracker = LatencyTracker(window=10, target_ms=1200)
    for _ in range(5):
        turno = TurnMetrics("t")
        turno.etapas["llm"] = 100.0
        turno.marcos["primeira_silaba"] = 400.0
        tracker.record(turno)

    resumo = tracker.summary()
    assert resumo["turnos"] == 5
    assert resumo["acima_da_meta"] == 0
    assert resumo["series"]["etapa.llm"]["p50"] == 100.0
    assert resumo["series"]["marco.primeira_silaba"]["max"] == 400.0


def test_tracker_conta_turno_acima_da_meta() -> None:
    tracker = LatencyTracker(target_ms=1200)
    lento = TurnMetrics("lento")
    lento.marcos["primeira_silaba"] = 2500.0
    tracker.record(lento)
    assert tracker.summary()["acima_da_meta"] == 1


def test_janela_deslizante_descarta_o_antigo() -> None:
    tracker = LatencyTracker(window=10)
    for i in range(25):
        turno = TurnMetrics(str(i))
        turno.etapas["stt"] = float(i)
        tracker.record(turno)
    assert tracker.summary()["series"]["etapa.stt"]["n"] == 10
