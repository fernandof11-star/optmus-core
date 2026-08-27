"""O resultado de uma acao confirmada volta para o agente.

O Achado Serio 1: a confirmacao acontece FORA do turno, e ate 25/08/2026 o que
a acao produzia parava na resposta HTTP. Com a camera isso era literal - ela
capturava, o quadro ia para ``ToolResult.imagens``, e ninguem olhava.

O teste que carrega o peso e :func:`test_a_foto_chega_ao_modelo`.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from core.agent import MARCADOR_IMAGEM, Agent, _descartar_imagens
from core.config import Settings
from core.llm import Imagem, LLMTurn
from security.dispositivos import ORIGEM_DESCONHECIDA, ORIGEM_VOZ
from security.policy import RiskLevel
from tools.registry import ResultadoConfirmado, Tool, ToolRegistry, ToolResult

JPEG_B64 = "/9j/4AAQSkZJRg=="


def _imagem() -> Imagem:
    return Imagem(
        dados_b64=JPEG_B64, media_type="image/jpeg", largura=1024, altura=576,
        origem="webcam",
    )


class ClienteFalso:
    """Guarda as mensagens que recebeu e devolve texto simples."""

    def __init__(self, respostas: list[LLMTurn] | None = None) -> None:
        self.vistas: list[list[dict[str, Any]]] = []
        self._respostas = respostas or []

    def server_tools(self) -> list[dict[str, Any]]:
        return []

    async def stream_turn(self, *, system, messages, tools=None, on_text=None) -> LLMTurn:
        # Copia profunda o suficiente: o agente MUTA a lista depois (despejo de
        # imagem), e sem copiar o teste veria o estado final, nao o enviado.
        import copy

        self.vistas.append(copy.deepcopy(messages))
        if self._respostas:
            return self._respostas.pop(0)
        return LLMTurn(text="vi sim", assistant_content=[], tool_calls=[], stop_reason="end_turn")


def _agente(settings: Settings, cliente: ClienteFalso) -> Agent:
    return Agent(cliente, settings)  # type: ignore[arg-type]


# --------------------------------------------------- a foto chega ao modelo
async def test_a_foto_chega_ao_modelo(settings: Settings) -> None:
    """O Achado Serio 1, virado teste.

    Sem isto a webcam acendia, o obturador disparava, a pessoa autorizava - e o
    quadro morria num campo que ninguem lia. O modelo entao respondia sobre uma
    foto que nunca viu, que e a pior falha possivel para uma ferramenta de
    visao: ele inventaria o que "viu".
    """
    cliente = ClienteFalso()
    await _agente(settings, cliente).run("[sistema] resultado", imagens=[_imagem()])

    abertura = cliente.vistas[0][-1]
    blocos = abertura["content"]

    assert isinstance(blocos, list), "abertura com imagem e lista de blocos"
    assert blocos[0]["type"] == "text", "texto primeiro: ele diz o que a imagem e"
    imagens = [b for b in blocos if b.get("type") == "image"]
    assert len(imagens) == 1
    assert imagens[0]["source"]["data"] == JPEG_B64


async def test_turno_sem_imagem_continua_texto_puro(settings: Settings) -> None:
    """O caminho normal nao pode virar lista de blocos a toa."""
    cliente = ClienteFalso()
    await _agente(settings, cliente).run("que horas sao")

    assert cliente.vistas[0][-1]["content"] == "que horas sao"


def test_imagem_solta_tambem_e_despejada() -> None:
    """A imagem da retomada nao e ``tool_result`` - e ramo proprio no despejo.

    Sem ele o quadro escapava da limpeza e viajava nas seis rodadas: ate seis
    vezes o preco de uma foto que o modelo ja tinha olhado na primeira. O
    despejo existia justamente para impedir isso no caminho da ferramenta.
    """
    mensagens = [
        {"role": "user", "content": [
            {"type": "text", "text": "olha"},
            {"type": "image", "source": {"type": "base64", "data": JPEG_B64}},
        ]}
    ]
    trocadas = _descartar_imagens(mensagens)

    assert trocadas == 1
    tipos = [b["type"] for b in mensagens[0]["content"]]
    assert tipos == ["text", "text"]
    assert mensagens[0]["content"][1]["text"] == MARCADOR_IMAGEM


# ------------------------------------------- o resultado sabe de quem era
class FerramentaFalsa(Tool):
    name = "olhar"
    description = "olha"
    schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}
    risk = RiskLevel.EXTERNO

    def resumir(self, parametros: dict[str, Any]) -> str:
        return "ligar a webcam e olhar o ambiente"

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(content="Quadro capturado.", imagens=[_imagem()])


async def _registro(settings: Settings, store: Any) -> ToolRegistry:
    from security.audit import AuditLog
    from security.policy import PolicyEngine

    r = ToolRegistry(policy=PolicyEngine(settings, store), audit=AuditLog(store), store=store)
    r.register(FerramentaFalsa())
    await r.refresh()
    return r


async def test_confirmado_carrega_a_pendencia(settings: Settings, store: Any) -> None:
    """Sem estes campos o chamador nao sabe QUAL acao terminou, nem de onde o
    pedido veio - e sem a origem nao da para decidir se a resposta e falada."""
    from security.dispositivos import origem

    registro = await _registro(settings, store)
    with origem(ORIGEM_VOZ):
        decisao = await registro.policy.avaliar(
            ferramenta="olhar", risco=RiskLevel.EXTERNO, parametros={}, resumo="ligar a webcam"
        )
    assert decisao.token is not None

    saida = await registro.executar_confirmado(decisao.token)

    assert isinstance(saida, ResultadoConfirmado)
    assert saida.ferramenta == "olhar"
    assert saida.origem == ORIGEM_VOZ
    assert saida.resumo == "ligar a webcam"
    assert len(saida.imagens) == 1, "a imagem sobrevive ate aqui"


async def test_recusa_da_politica_nao_finge_execucao(settings: Settings, store: Any) -> None:
    """Token invalido nao executou nada: sem ferramenta, nao ha o que comentar.

    E o campo vazio que o endpoint usa para NAO acordar o agente - comentar uma
    execucao que nao houve seria o agente relatando um fato inventado.
    """
    registro = await _registro(settings, store)
    saida = await registro.executar_confirmado("token-inventado")

    assert saida.is_error
    assert saida.ferramenta == ""


# ------------------------------------------------------ quem ouve a resposta
@pytest.mark.parametrize(
    ("origem_", "espera_falar"),
    [(ORIGEM_VOZ, True), ("hud-7f3a", False), (ORIGEM_DESCONHECIDA, False)],
)
async def test_so_pedido_por_voz_e_falado(
    settings: Settings, origem_: str, espera_falar: bool
) -> None:
    """Falar num pedido vindo do HUD faria o Core falar em voz alta na maquina
    onde ELE roda enquanto a pessoa esta no navegador - o mesmo absurdo que o
    ``falar=False`` do ``/chat`` ja tinha corrigido."""
    from core.voice_loop import VoiceLoop

    vistos: list[bool] = []

    async def espiao(self, texto, turn_id, turno, *, falar=True, **kw):
        vistos.append(falar)
        from core.voice_loop import TurnOutcome

        return TurnOutcome(turn_id, texto, "ok", self._router.route("x").camada)

    original = VoiceLoop._rodar_agente
    VoiceLoop._rodar_agente = espiao  # type: ignore[method-assign]
    try:
        laco = VoiceLoop.__new__(VoiceLoop)
        laco._turno = None
        laco._router = type("R", (), {"route": lambda self, t: type("X", (), {"camada": None})()})()
        await VoiceLoop.retomar_apos_confirmacao(
            laco, ferramenta="olhar", resumo="r", resultado="ok", origem=origem_
        )
    finally:
        VoiceLoop._rodar_agente = original  # type: ignore[method-assign]

    assert vistos == [espera_falar]
