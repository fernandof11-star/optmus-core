"""Sintese: corte em frases, cadeia de fallback e kill switch."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from expression.tts import SentenceBuffer, SpeechSynthesizer
from tests.fakes import ExplodindoTTSEngine, FakeTTSEngine, GravadorDePlayer


def test_buffer_libera_frase_completa() -> None:
    buffer = SentenceBuffer()
    assert buffer.push("Tres mil e ") == []
    assert buffer.push("duzentos reais. ") == ["Tres mil e duzentos reais."]


def test_buffer_nao_pica_fragmento_curto() -> None:
    """"Sao 4." viraria audio picotado; espera a proxima pontuacao."""
    buffer = SentenceBuffer(min_chars=12)
    assert buffer.push("Sao 4.") == []
    assert buffer.push(" E vinte da tarde.") == ["Sao 4. E vinte da tarde."]


def test_buffer_flush_devolve_o_resto() -> None:
    buffer = SentenceBuffer()
    buffer.push("sem pontuacao no fim")
    assert buffer.flush() == "sem pontuacao no fim"
    assert buffer.flush() == ""


async def test_fala_comeca_antes_do_fim_da_geracao() -> None:
    """Criterio de latencia: a 1a frase e sintetizada com o stream ainda aberto."""
    motor = FakeTTSEngine()
    synth = SpeechSynthesizer([motor], GravadorDePlayer())
    faladas_ate_agora: list[int] = []

    async def deltas() -> AsyncIterator[str]:
        for pedaco in ("Abrindo o YouTube. ", "Play nos tres. ", "Feito."):
            yield pedaco
            faladas_ate_agora.append(len(motor.falas))

    await synth.speak_stream(deltas())
    assert faladas_ate_agora[0] == 1, "primeira frase deve sair antes do 2o delta"
    assert motor.falas == ["Abrindo o YouTube.", "Play nos tres.", "Feito."]


async def test_cadeia_pula_motor_indisponivel() -> None:
    indisponivel = FakeTTSEngine("elevenlabs", disponivel=False)
    fallback = FakeTTSEngine("piper")
    synth = SpeechSynthesizer([indisponivel, fallback], GravadorDePlayer())

    assert await synth.speak("teste de fallback")
    assert synth.motor_ativo == "piper"
    assert fallback.falas == ["teste de fallback"]


async def test_motor_que_falha_no_meio_nao_derruba_o_turno() -> None:
    synth = SpeechSynthesizer([ExplodindoTTSEngine("ruim")], GravadorDePlayer())
    assert await synth.speak("frase qualquer") is False
    assert synth.motor_ativo is None


async def test_sem_nenhum_motor_o_optmus_fica_mudo_nao_morto() -> None:
    synth = SpeechSynthesizer([FakeTTSEngine(disponivel=False)], GravadorDePlayer())
    assert await synth.speak("ninguem me ouve") is False


async def test_stop_interrompe_a_fala() -> None:
    motor = FakeTTSEngine()
    player = GravadorDePlayer()
    synth = SpeechSynthesizer([motor], player)

    await synth.stop()
    assert await synth.speak("nao devo falar isso") is False
    assert player.parou is True

    synth.resume()
    assert await synth.speak("agora sim, pode falar")


async def test_marco_de_primeira_silaba_dispara_uma_vez() -> None:
    disparos: list[int] = []
    synth = SpeechSynthesizer(
        [FakeTTSEngine()], GravadorDePlayer(), on_first_audio=lambda: disparos.append(1)
    )
    await synth.speak("uma frase inteira aqui")
    assert len(disparos) == 1


@pytest.mark.parametrize("texto", ["", "   "])
async def test_texto_vazio_nao_sintetiza(texto: str) -> None:
    motor = FakeTTSEngine()
    synth = SpeechSynthesizer([motor], GravadorDePlayer())
    assert await synth.speak(texto) is False
    assert motor.falas == []
