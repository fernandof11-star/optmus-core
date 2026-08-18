"""Criterio de aceite da F1: a fala percorre o pipeline e sai como audio.

"Optmus, que horas sao" responde em menos de 2s - aqui com dubles, para medir
a estrutura do pipeline; o numero de producao sai do /metrics na maquina real.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.agent import Agent
from core.bus import Event, InProcessEventBus
from core.config import Settings, get_settings, reset_settings_cache
from core.llm import LLMTurn
from core.metrics import LatencyTracker
from core.router import Acao, Camada, IntentRouter
from core.voice_loop import VoiceLoop
from expression.tts import SpeechSynthesizer
from memory.embeddings import HashingEmbedder
from memory.system import MemorySystem
from perception.vad import UtteranceCapture, VoiceActivityDetector
from tests.fakes import (
    FakeLLM,
    FakeTranscriber,
    FakeTTSEngine,
    FakeWake,
    GravadorDePlayer,
    frames_de_teste,
)


@pytest.fixture
def motor() -> FakeTTSEngine:
    return FakeTTSEngine()


@pytest.fixture
def memoria(settings: Settings, store) -> MemorySystem:
    return MemorySystem(settings, store, embedder=HashingEmbedder(settings.embedding_dim))


@pytest.fixture
def montar(settings: Settings, bus: InProcessEventBus, motor: FakeTTSEngine, memoria):
    def _montar(
        turnos: list[LLMTurn] | None = None, *, cliente: FakeLLM | None = None
    ) -> VoiceLoop:
        return VoiceLoop(
            settings,
            bus=bus,
            router=IntentRouter(settings),
            agent=Agent(cliente or FakeLLM(turnos or []), settings),
            synthesizer=SpeechSynthesizer([motor], GravadorDePlayer()),
            tracker=LatencyTracker(target_ms=settings.latency_target_ms),
            memory=memoria,
        )

    return _montar


async def test_comando_trivial_nao_toca_no_llm(montar, motor: FakeTTSEngine) -> None:
    voz = montar([LLMTurn(text="NAO DEVERIA SER CHAMADO", stop_reason="end_turn")])
    resultado = await voz.handle_text("que horas sao")

    assert resultado.camada is Camada.DETERMINISTICA
    assert resultado.regra == "hora"
    assert resultado.falado
    assert motor.falas == [resultado.resposta]
    assert "NAO DEVERIA" not in "".join(motor.falas)


async def test_comando_real_passa_pelo_llm_e_vira_audio(montar, motor: FakeTTSEngine) -> None:
    voz = montar([LLMTurn(text="Tres mil e duzentos esse mes.", stop_reason="end_turn")])
    resultado = await voz.handle_text("quanto gastei esse mes")

    assert resultado.camada is Camada.LLM
    assert resultado.resposta == "Tres mil e duzentos esse mes."
    assert motor.falas == ["Tres mil e duzentos esse mes."]


async def test_turno_publica_eventos_e_latencia(montar, bus: InProcessEventBus) -> None:
    recebidos: list[Event] = []
    await bus.subscribe("voz.*", recebidos.append, name="espiao")

    voz = montar([LLMTurn(text="Feito.", stop_reason="end_turn")])
    resultado = await voz.handle_text("faz uma coisa")

    for _ in range(200):
        if {e.type for e in recebidos} >= {"voz.entrada", "voz.resposta", "voz.latencia"}:
            break
        await _tick()
    tipos = [e.type for e in recebidos]
    assert tipos[0] == "voz.entrada"
    assert {"voz.resposta", "voz.latencia"} <= set(tipos)
    assert all(e.correlation_id == resultado.turn_id for e in recebidos)


async def _tick() -> None:
    import asyncio

    await asyncio.sleep(0.005)


async def test_latencia_e_medida_por_etapa(montar) -> None:
    voz = montar([LLMTurn(text="Resposta com tamanho suficiente.", stop_reason="end_turn")])
    resultado = await voz.handle_text("pergunta qualquer")

    assert "router" in resultado.latencia["etapas"]
    assert "llm" in resultado.latencia["etapas"]
    assert "primeira_silaba" in resultado.latencia["marcos"]
    assert resultado.latencia["total_ms"] > 0


async def test_kill_switch_e_atendido_pela_camada_1(montar, motor: FakeTTSEngine) -> None:
    voz = montar([LLMTurn(text="NAO DEVERIA SER CHAMADO", stop_reason="end_turn")])
    resultado = await voz.handle_text("Optmus, parar tudo")

    assert resultado.acao is Acao.PARAR
    assert resultado.camada is Camada.DETERMINISTICA


async def test_silenciar_impede_a_fala_ate_liberar(montar, motor: FakeTTSEngine) -> None:
    voz = montar([LLMTurn(text="deveria ficar quieto", stop_reason="end_turn")])
    await voz.handle_text("silencio")
    assert voz.silenciado

    await voz.handle_text("que horas sao")
    assert motor.falas == [], "silenciado nao fala nem resposta da camada 1"

    voz.unmute()
    await voz.handle_text("que horas sao")
    assert motor.falas


async def test_historico_alimenta_o_turno_seguinte(montar) -> None:
    cliente = FakeLLM([
        LLMTurn(text="Sao Paulo.", stop_reason="end_turn"),
        LLMTurn(text="Chuva a tarde.", stop_reason="end_turn"),
    ])
    voz = montar(cliente=cliente)
    await voz.handle_text("onde eu moro")
    await voz.handle_text("e o tempo la")

    segunda = cliente.chamadas[1]["messages"]
    assert segunda[0]["content"] == "onde eu moro"
    assert segunda[1]["content"] == "Sao Paulo."


async def test_lembra_de_algo_dito_ha_tres_dias_e_usa_espontaneamente(
    montar, memoria: MemorySystem, store
) -> None:
    """Criterio de aceite da F2, ponta a ponta.

    O usuario contou algo tres dias atras. Hoje ele pergunta sobre o assunto
    sem repetir o nome, e a memoria chega ao modelo sem ninguem pedir.
    """
    tres_dias_atras = (datetime.now(UTC) - timedelta(days=3)).isoformat(timespec="milliseconds")
    memory_id = await store.insert_memory(
        layer="episodica",
        content="meu contador chama Ricardo Almeida -> Anotado.",
        source="conversa",
        confidence=1.0,
        created_at=tres_dias_atras,
    )
    await store.upsert_vector(
        memory_id, await memoria.embedder.embed_one("meu contador chama Ricardo Almeida")
    )

    cliente = FakeLLM([LLMTurn(text="Ricardo Almeida.", stop_reason="end_turn")])
    voz = montar(cliente=cliente)
    await voz.handle_text("como chama meu contador mesmo")

    sistema = cliente.chamadas[0]["system"]
    assert "<memoria>" in sistema
    assert "Ricardo Almeida" in sistema, "a memoria de 3 dias atras nao chegou ao modelo"


async def test_turno_grava_episodio_para_o_futuro(montar, memoria: MemorySystem) -> None:
    voz = montar([LLMTurn(text="Anotado.", stop_reason="end_turn")])
    await voz.handle_text("meu contador chama Ricardo")

    assert await memoria.episodic.count() == 1
    assert len(memoria.working) == 2


async def test_camada_1_nao_paga_busca_de_memoria(montar) -> None:
    """"Que horas sao" nao pode custar uma consulta vetorial."""
    voz = montar([LLMTurn(text="NAO DEVERIA SER CHAMADO", stop_reason="end_turn")])
    resultado = await voz.handle_text("que horas sao")
    assert "memoria" not in resultado.latencia["etapas"]


async def test_turno_de_voz_completo_wake_ate_audio(
    settings: Settings, bus, motor: FakeTTSEngine, montar
) -> None:
    voz = montar([LLMTurn(text="Abrindo agora.", stop_reason="end_turn")])
    transcritor = FakeTranscriber(["abre o youtube"])
    voz.attach_voice_io(transcritor, FakeWake(disparos=1))  # type: ignore[arg-type]

    def frames():
        return frames_de_teste(120, com_voz=40)

    await voz.run_forever(frames)

    assert transcritor.chamadas == 1
    assert motor.falas == ["Abrindo agora."]
    assert voz.turnos == 1


async def test_fala_curta_demais_e_descartada(settings: Settings, bus, motor, montar) -> None:
    """Tosse e batida de porta nao viram chamada ao modelo."""
    voz = montar([LLMTurn(text="NAO DEVERIA SER CHAMADO", stop_reason="end_turn")])
    transcritor = FakeTranscriber(["ruido"])
    voz.attach_voice_io(transcritor, FakeWake(disparos=1))  # type: ignore[arg-type]

    await voz.run_forever(lambda: frames_de_teste(120, com_voz=1))

    assert transcritor.chamadas == 0
    assert motor.falas == []


async def test_gatilho_sem_escuta_devolve_falso(montar) -> None:
    voz = montar()
    assert voz.escutando is False
    assert await voz.trigger_wake() is False


async def test_gatilho_com_escuta_abre_o_turno(montar, motor: FakeTTSEngine) -> None:
    voz = montar([LLMTurn(text="Abrindo agora.", stop_reason="end_turn")])
    wake = FakeWake(disparos=0)
    voz.attach_voice_io(FakeTranscriber(["abre o youtube"]), wake)  # type: ignore[arg-type]

    assert await voz.trigger_wake() is True
    await voz.run_forever(lambda: frames_de_teste(120, com_voz=40))
    assert motor.falas == ["Abrindo agora."]


async def test_captura_para_no_silencio(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backend fixado em energia: o tom sintetico nao e fala para o webrtcvad,
    e a contagem exata de frames so e deterministica com um backend escolhido."""
    monkeypatch.setenv("OPTMUS_VAD_BACKEND", "energia")
    reset_settings_cache()
    energia = get_settings()

    fala = await UtteranceCapture(energia).capture(frames_de_teste(200, com_voz=30))
    esperado_frames = 30 + energia.vad_silence_ms // energia.audio_frame_ms
    assert fala.motivo.value == "silencio"
    assert len(fala.pcm) == esperado_frames * 320


def test_backend_do_vad_e_escolha_explicita(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTMUS_VAD_BACKEND", "energia")
    reset_settings_cache()
    assert VoiceActivityDetector(get_settings()).modo == "energia"


async def test_falar_false_nao_sintetiza_no_servidor(montar, motor: FakeTTSEngine) -> None:
    """Caminho do navegador: o Core responde, quem fala e o cliente.

    Sem isto, uma mensagem digitada no site faz a MAQUINA ONDE O CORE RODA
    falar em voz alta - e o turno so termina quando o audio termina. Medido em
    producao local: 3,8 s de TTS numa resposta pronta em 2 ms.
    """
    voz = montar([LLMTurn(text="Tres mil e duzentos esse mes.", stop_reason="end_turn")])
    resultado = await voz.handle_text("quanto gastei esse mes", falar=False)

    assert resultado.resposta == "Tres mil e duzentos esse mes.", "a resposta continua vindo"
    assert resultado.falado is False
    assert motor.falas == [], "nenhum audio sintetizado no servidor"
    assert "tts_restante" not in resultado.latencia.get("etapas", {}), (
        "sem sintese, o turno nao paga espera de audio"
    )


async def test_falar_false_tambem_vale_na_camada_deterministica(
    montar, motor: FakeTTSEngine
) -> None:
    """A camada 1 responde sem LLM - e tinha falar=True fixo no codigo."""
    voz = montar()
    resultado = await voz.handle_text("que horas sao", falar=False)

    assert resultado.resposta, "a hora continua sendo respondida"
    assert motor.falas == []


async def test_falar_padrao_continua_sintetizando(montar, motor: FakeTTSEngine) -> None:
    """O caminho de voz nao pode ter sido quebrado pelo parametro novo."""
    voz = montar([LLMTurn(text="Pronto.", stop_reason="end_turn")])
    resultado = await voz.handle_text("faz uma coisa dificil")

    assert resultado.falado is True
    assert motor.falas == ["Pronto."]
