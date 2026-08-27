"""F7: os freios de quem fala sem ser chamado.

Nenhum teste aqui e sobre entrega. Todos sao sobre **nao falar**: a unica
funcao do Optmus que interrompe alguem e a unica em que o defeito interessante
e o excesso, nao a falta.

O que carrega mais peso:
:func:`test_o_compositor_nao_tem_ferramenta_nenhuma` - a guarda estrutural que
impede um caminho sem portao de alcancar terceiros.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from core.config import Settings, get_settings, reset_settings_cache
from core.proatividade import (
    SEM_AVISO,
    Gatilho,
    Proatividade,
)
from core.proatividade_fontes import CanalComposto, CompositorLLM, PrazosDoNotion
from memory.store import Store

MANHA = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
MADRUGADA = datetime(2026, 9, 14, 3, 0, tzinfo=UTC)


@pytest.fixture
def pro_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("OPTMUS_PROACTIVE_ENABLED", "true")
    monkeypatch.setenv("OPTMUS_PROACTIVE_DAILY_BUDGET", "2")
    reset_settings_cache()
    return get_settings()


class FonteFalsa:
    def __init__(self, gatilhos: list[Gatilho] | None = None, erro: Exception | None = None):
        self._gatilhos = gatilhos or []
        self._erro = erro
        self.chamadas = 0

    async def coletar(self, agora: datetime) -> list[Gatilho]:
        self.chamadas += 1
        if self._erro is not None:
            raise self._erro
        return list(self._gatilhos)


class CompositorFalso:
    def __init__(self, texto: str = "voce tem prova amanha") -> None:
        self.texto = texto
        self.recebidos: list[str] = []

    async def escrever(self, fatos: str) -> str:
        self.recebidos.append(fatos)
        return self.texto


class CanalFalso:
    def __init__(self, entrega: bool = True, erro: Exception | None = None) -> None:
        self.entrega = entrega
        self.enviados: list[str] = []
        self._erro = erro

    async def avisar(self, texto: str) -> bool:
        if self._erro is not None:
            raise self._erro
        self.enviados.append(texto)
        return self.entrega


def _gatilho(chave: str = "a", urgencia: int = 1) -> Gatilho:
    return Gatilho(
        chave=chave, assunto=f"assunto-{chave}", fatos=f"- fato {chave}", urgencia=urgencia
    )


def _motor(
    settings: Settings, store: Store, *, fontes: list[Any], canal: Any = None, comp: Any = None
) -> tuple[Proatividade, CompositorFalso, CanalFalso]:
    compositor = comp or CompositorFalso()
    saida = canal or CanalFalso()
    motor = Proatividade(
        settings, store, compositor=compositor, canal=saida, fontes=fontes
    )
    return motor, compositor, saida


# ------------------------------------------------- a guarda que mais importa
def test_o_compositor_nao_tem_ferramenta_nenhuma(settings: Settings) -> None:
    """Um caminho sem portao nao pode alcancar terceiros.

    Aviso proativo nao tem humano esperando para confirmar, entao o portao de
    EXTERNO nao se aplica. Se o compositor tivesse ferramentas, o modelo
    poderia chamar `whatsapp_enviar` no meio de escrever um lembrete - e
    mandaria mensagem para outra pessoa sem ninguem autorizar.

    Nao adianta pedir isso no prompt: o prompt e sugestao, `tools=None` e
    estrutura.
    """
    class ClienteMudo:
        def server_tools(self) -> list[Any]:
            return []

    compositor = CompositorLLM(ClienteMudo(), settings)  # type: ignore[arg-type]

    assert compositor._agente._tools is None
    assert compositor._agente._montar_ferramentas() in (None, [])


# ----------------------------------------------------------- nao interromper
async def test_desligada_nao_faz_nada(settings: Settings, store: Store) -> None:
    """Padrao e ficar quieto: ligar sozinho seria decidir por quem instalou."""
    fonte = FonteFalsa([_gatilho()])
    motor, _, canal = _motor(settings, store, fontes=[fonte])

    r = await motor.ciclo(MANHA)

    assert r.avisou is False
    assert "desligada" in r.motivo
    assert fonte.chamadas == 0, "nem consultou a fonte"
    assert canal.enviados == []


async def test_janela_de_silencio_cala(pro_settings: Settings, store: Store) -> None:
    """Descarta, nao adia - e descartar nao perde nada, porque a fonte e
    relida no ciclo seguinte. Adiar produziria enxurrada as 8h."""
    motor, _, canal = _motor(pro_settings, store, fontes=[FonteFalsa([_gatilho()])])

    assert (await motor.ciclo(MADRUGADA)).avisou is False
    assert canal.enviados == []

    # Mesma pendencia, hora util: sai sozinha, sem fila nenhuma.
    assert (await motor.ciclo(MANHA)).avisou is True


def test_silencio_cruza_a_meia_noite(pro_settings: Settings, store: Store) -> None:
    """22h-8h e intervalo que vira o dia. Comparar como `inicio <= h < fim`
    daria falso para TODA hora, e a janela nunca calaria nada."""
    motor, _, _ = _motor(pro_settings, store, fontes=[])

    for hora in (22, 23, 0, 3, 7):
        assert motor.em_silencio(MANHA.replace(hour=hora)) is True, hora
    for hora in (8, 12, 21):
        assert motor.em_silencio(MANHA.replace(hour=hora)) is False, hora


async def test_um_aviso_por_ciclo(pro_settings: Settings, store: Store) -> None:
    """Tres prazos vencendo hoje viram UM aviso. Rajada e o que faz alguem
    silenciar o assistente para sempre."""
    fonte = FonteFalsa([_gatilho("a"), _gatilho("b"), _gatilho("c")])
    motor, _, canal = _motor(pro_settings, store, fontes=[fonte])

    r = await motor.ciclo(MANHA)

    assert r.gatilhos == 3
    assert len(canal.enviados) == 1


async def test_o_mais_urgente_ganha(pro_settings: Settings, store: Store) -> None:
    fonte = FonteFalsa([_gatilho("baixa", 0), _gatilho("alta", 3), _gatilho("media", 1)])
    motor, _, _ = _motor(pro_settings, store, fontes=[fonte])

    assert (await motor.ciclo(MANHA)).assunto == "assunto-alta"


# ------------------------------------------------------------- orcamento
async def test_orcamento_e_teto_rigido(pro_settings: Settings, store: Store) -> None:
    """Dois por dia significa dois, e o terceiro nao entra em fila."""
    fonte = FonteFalsa([_gatilho("a"), _gatilho("b"), _gatilho("c")])
    motor, _, canal = _motor(pro_settings, store, fontes=[fonte])

    for _ in range(4):
        await motor.ciclo(MANHA)

    assert len(canal.enviados) == 2
    assert (await motor.ciclo(MANHA)).motivo == "orcamento do dia esgotado"


async def test_orcamento_vira_no_dia_seguinte(pro_settings: Settings, store: Store) -> None:
    """A contagem e por data, nao por contador que alguem precisa zerar - um
    contador desses erraria em todo reinicio do processo."""
    motor, _, _ = _motor(
        pro_settings, store, fontes=[FonteFalsa([_gatilho("a"), _gatilho("b")])]
    )

    await motor.ciclo(MANHA)
    await motor.ciclo(MANHA)
    assert (await motor.restante(MANHA)) == 0

    amanha = MANHA + timedelta(days=1)
    assert (await motor.restante(amanha)) == 2
    assert (await motor.ciclo(amanha)).avisou is True


async def test_entrega_falhada_nao_gasta_orcamento(pro_settings: Settings, store: Store) -> None:
    """Cobrar por um aviso que nao chegou faria uma rede instavel silenciar o
    dia inteiro."""
    canal = CanalFalso(entrega=False)
    motor, _, _ = _motor(pro_settings, store, fontes=[FonteFalsa([_gatilho()])], canal=canal)

    r = await motor.ciclo(MANHA)

    assert r.avisou is False
    assert await motor.gasto_hoje(MANHA) == 0


# ------------------------------------------------------------ repeticao
async def test_mesmo_assunto_nao_repete(pro_settings: Settings, store: Store) -> None:
    """Sem isto, "voce tem prova amanha" sairia a cada trinta minutos."""
    motor, _, canal = _motor(pro_settings, store, fontes=[FonteFalsa([_gatilho("a")])])

    await motor.ciclo(MANHA)
    r = await motor.ciclo(MANHA.replace(hour=11))

    assert len(canal.enviados) == 1
    assert "ja avisado" in r.motivo


async def test_repete_depois_do_periodo_de_espera(pro_settings: Settings, store: Store) -> None:
    motor, _, canal = _motor(pro_settings, store, fontes=[FonteFalsa([_gatilho("a")])])

    await motor.ciclo(MANHA)
    # +25 h e nao +13 h: treze horas depois das 10h da 23h, dentro da janela de
    # silencio - a primeira versao deste teste falhou por isso, calada pelo
    # freio certo. O periodo de espera e de 12 h; 25 h passa dos dois.
    await motor.ciclo(MANHA + timedelta(hours=25))

    assert len(canal.enviados) == 2


async def test_a_chave_do_prazo_sobrevive_a_passagem_dos_dias() -> None:
    """O mesmo prazo, lido em dias diferentes, tem que ser o MESMO assunto.

    "faltam 2" vira "falta 1" amanha. Se a chave carregasse os dias restantes,
    o mesmo prazo viraria assunto novo toda madrugada e seria avisado todo dia
    ate vencer - a deduplicacao existiria no papel e nao seguraria nada.

    A primeira versao deste teste comparava `impressao()` com os MESMOS
    argumentos duas vezes: passava sempre, inclusive com o defeito injetado.
    Aqui a fonte e consultada em dois dias de verdade.
    """
    fonte = PrazosDoNotion(StatsCalculado("2026-09-16"))

    hoje = await fonte.coletar(MANHA)
    amanha = await fonte.coletar(MANHA + timedelta(days=1))

    assert hoje[0].fatos != amanha[0].fatos, "o texto muda - faltam 2, depois 1"
    assert hoje[0].chave == amanha[0].chave, "mas o assunto e o mesmo"


# -------------------------------------------------------------- nao inventa
async def test_sem_gatilho_nao_ha_aviso(pro_settings: Settings, store: Store) -> None:
    """Sem dado real, silencio. O modelo nem e chamado."""
    compositor = CompositorFalso()
    motor, _, canal = _motor(pro_settings, store, fontes=[FonteFalsa([])], comp=compositor)

    r = await motor.ciclo(MANHA)

    assert r.avisou is False
    assert r.motivo == "nada a dizer"
    assert compositor.recebidos == [], "o modelo nem foi chamado"
    assert canal.enviados == []


async def test_modelo_pode_recusar_o_aviso(pro_settings: Settings, store: Store) -> None:
    """Os fatos existem mas nao justificam interromper ninguem."""
    motor, _, canal = _motor(
        pro_settings, store, fontes=[FonteFalsa([_gatilho()])], comp=CompositorFalso(SEM_AVISO)
    )

    r = await motor.ciclo(MANHA)

    assert r.avisou is False
    assert canal.enviados == []
    assert await motor.gasto_hoje(MANHA) == 0, "nao gasta por um aviso que nao saiu"


async def test_so_os_fatos_chegam_ao_modelo(pro_settings: Settings, store: Store) -> None:
    compositor = CompositorFalso()
    motor, _, _ = _motor(
        pro_settings, store, fontes=[FonteFalsa([_gatilho("a")])], comp=compositor
    )

    await motor.ciclo(MANHA)

    assert compositor.recebidos == ["- fato a"]


# ----------------------------------------------------------------- falhas
async def test_fonte_quebrada_nao_cala_as_outras(pro_settings: Settings, store: Store) -> None:
    """O Notion fora do ar nao pode calar uma rotina detectada localmente."""
    ruim = FonteFalsa(erro=RuntimeError("notion caiu"))
    boa = FonteFalsa([_gatilho("viva")])
    motor, _, canal = _motor(pro_settings, store, fontes=[ruim, boa])

    r = await motor.ciclo(MANHA)

    assert r.avisou is True
    assert canal.enviados
    assert any("notion caiu" in f for f in r.falhas)


async def test_canal_quebrado_nao_cala_o_outro() -> None:
    """Quem esta no celular nao ve o navegador: falha de um nao cala o outro."""
    ruim = CanalFalso(erro=RuntimeError("telegram caiu"))
    bom = CanalFalso()

    assert await CanalComposto([ruim, bom]).avisar("oi") is True
    assert bom.enviados == ["oi"]


# ----------------------------------------------------------- fonte de prazos
class StatsCalculado:
    """Calcula `diasRestantes` a partir do dia consultado, como o Notion faz."""

    def __init__(self, data: str) -> None:
        self._data = data

    async def progress_alerts(self, *, hoje: Any = None) -> list[dict[str, Any]]:
        from datetime import date

        faltam = (date.fromisoformat(self._data) - hoje).days
        return [
            {"tipo": "prova", "titulo": "biologia", "data": self._data, "diasRestantes": faltam}
        ]


class StatsFalso:
    def __init__(self, alertas: list[dict[str, Any]]) -> None:
        self._alertas = alertas

    async def progress_alerts(self, *, hoje: Any = None) -> list[dict[str, Any]]:
        return self._alertas


async def test_prazo_distante_nao_vira_aviso() -> None:
    """Trinta dias antes nao e aviso, e ansiedade."""
    fonte = PrazosDoNotion(
        StatsFalso([
            {"tipo": "prova", "titulo": "biologia", "data": "2026-10-20", "diasRestantes": 30},
            {"tipo": "prova", "titulo": "quimica", "data": "2026-09-16", "diasRestantes": 2},
        ])
    )

    gatilhos = await fonte.coletar(MANHA)

    assert len(gatilhos) == 1
    assert gatilhos[0].assunto == "quimica"


async def test_prazo_vencido_ainda_avisa() -> None:
    """O que passou e o que mais importa saber."""
    fonte = PrazosDoNotion(
        StatsFalso([
            {"tipo": "tarefa", "titulo": "entregar", "data": "2026-09-12", "diasRestantes": -2}
        ])
    )

    gatilhos = await fonte.coletar(MANHA)

    assert len(gatilhos) == 1
    assert "venceu ha 2" in gatilhos[0].fatos
