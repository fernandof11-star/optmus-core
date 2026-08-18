"""Camada 1 do roteador: o que nunca deve chegar ao modelo grande."""

from __future__ import annotations

import pytest

from core.config import Settings
from core.router import Acao, Camada, IntentRouter, normalizar


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("Que horas são?", "que horas sao"),
        ("  OPTMUS,   para   TUDO! ", "optmus para tudo"),
        ("Bom dia.", "bom dia"),
    ],
)
def test_normalizacao_tira_acento_e_pontuacao(texto: str, esperado: str) -> None:
    assert normalizar(texto) == esperado


@pytest.mark.parametrize(
    "texto",
    ["que horas são", "Que horas sao?", "me da as horas", "que hora é"],
)
def test_hora_resolve_sem_llm(settings: Settings, texto: str) -> None:
    rota = IntentRouter(settings).route(texto)
    assert rota.camada is Camada.DETERMINISTICA
    assert rota.regra == "hora"
    assert rota.resposta


def test_data_resolve_sem_llm(settings: Settings) -> None:
    rota = IntentRouter(settings).route("que dia é hoje")
    assert rota.resolvido and rota.regra == "data"


@pytest.mark.parametrize("texto", ["para tudo", "Optmus, parar tudo", "cancela", "pare"])
def test_kill_switch_e_deterministico(settings: Settings, texto: str) -> None:
    """O "para" NAO pode depender de rede: precisa morrer na camada 1."""
    rota = IntentRouter(settings).route(texto)
    assert rota.camada is Camada.DETERMINISTICA
    assert rota.acao is Acao.PARAR


def test_silenciar(settings: Settings) -> None:
    assert IntentRouter(settings).route("silencio").acao is Acao.SILENCIAR


def test_saudacao_usa_o_tratamento_configurado(settings: Settings) -> None:
    rota = IntentRouter(settings).route("bom dia")
    assert settings.user_honorific in (rota.resposta or "")


@pytest.mark.parametrize(
    "texto",
    [
        "quanto gastei esse mes",
        "abre o youtube nos tres celulares",
        "manda mensagem pro Joao",
        "me lembra de ligar pro contador amanha",
    ],
)
def test_comando_real_sobe_para_o_llm(settings: Settings, texto: str) -> None:
    rota = IntentRouter(settings).route(texto)
    assert rota.camada is Camada.LLM
    assert rota.resposta is None


def test_texto_vazio_nao_vai_para_o_llm(settings: Settings) -> None:
    assert IntentRouter(settings).route("   ").resolvido


def test_stats_contam_as_duas_camadas(settings: Settings) -> None:
    router = IntentRouter(settings)
    for texto in ("que horas sao", "bom dia", "quanto gastei esse mes"):
        router.route(texto)
    stats = router.stats()
    assert (stats["total"], stats["camada1"], stats["camada2"]) == (3, 2, 1)
    assert stats["taxa_camada1"] == pytest.approx(0.6667, abs=1e-3)
