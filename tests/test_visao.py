"""OlharTool: resolucao por modo, risco EXTERNO e a imagem fora da auditoria.

Sem OpenCV e sem camera - a captura entra por fabrica injetada.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from core.config import Settings, get_settings, reset_settings_cache
from perception.camera import CameraIndisponivelError
from security.policy import RiskLevel
from tools.impl.visao import OlharTool
from tools.registry import ToolResult

JPEG = b"\xff\xd8\xff" + b"corpo-da-imagem" * 40 + b"\xff\xd9"


class CameraFalsa:
    """Registra o que foi pedido e devolve um JPEG de mentira."""

    def __init__(
        self,
        largura: int,
        altura: int,
        indice: int,
        *,
        erro: Exception | None = None,
        nativa: tuple[int, int] | None = None,
    ) -> None:
        self.pedida = (largura, altura)
        self.indice = indice
        self.nativa = nativa or (1280, 720)
        self._erro = erro
        self.liberada = False

    async def __aenter__(self) -> CameraFalsa:
        return self

    async def __aexit__(self, *_: Any) -> None:
        self.liberada = True

    async def capturar(self) -> tuple[bytes, dict[str, Any]]:
        if self._erro is not None:
            raise self._erro
        return JPEG, {
            "resolucao_nativa": self.nativa,
            "resolucao_entregue": self.pedida,
            "redimensionado": self.pedida != self.nativa,
            "quadros_descartados": 5,
            "latencia_captura_ms": 1843.13,
            "timestamp": "2026-08-19T12:00:00+00:00",
        }


def _ferramenta(settings: Settings, **kw: Any) -> tuple[OlharTool, list[CameraFalsa]]:
    criadas: list[CameraFalsa] = []

    def fabrica(largura: int, altura: int, indice: int) -> Any:
        camera = CameraFalsa(largura, altura, indice, **kw)
        criadas.append(camera)
        return camera

    return OlharTool(settings, fabrica=fabrica), criadas


# ------------------------------------------------------------------ modos
@pytest.mark.parametrize(
    ("modo", "esperado"), [("descrever", (1024, 576)), ("ler", (1280, 720))]
)
async def test_cada_modo_pede_sua_resolucao(
    settings: Settings, modo: str, esperado: tuple[int, int]
) -> None:
    """'ler' precisa de pixel: economizar resolucao em OCR produz leitura
    errada com cara de certa, que e pior que nao ler."""
    ferramenta, criadas = _ferramenta(settings)
    resultado = await ferramenta.execute(modo=modo)

    assert criadas[0].pedida == esperado
    assert resultado.metadata["resolucao_entregue"] == esperado
    assert resultado.imagens[0].largura, resultado.imagens[0].altura == esperado


async def test_modo_invalido_nao_acende_a_camera(settings: Settings) -> None:
    ferramenta, criadas = _ferramenta(settings)
    resultado = await ferramenta.execute(modo="filmar")

    assert resultado.is_error
    assert criadas == [], "nem chegou a abrir o dispositivo"


# ---------------------------------------------------------------- imagem
async def test_imagem_vai_em_imagens_e_nunca_no_texto(settings: Settings) -> None:
    """A separacao da F4.1 e o que mantem a foto fora da auditoria.

    ``AuditLog.registrar`` grava ``resultado.content``. Se a imagem estivesse
    embutida no texto, uma foto da sala do usuario iria para o SQLite - truncada
    em 500 caracteres, o que e pior ainda: um pedaco de imagem que nao serve
    para nada e mesmo assim vazou.
    """
    ferramenta, _ = _ferramenta(settings)
    resultado = await ferramenta.execute(modo="descrever")

    assert len(resultado.imagens) == 1
    assert resultado.imagens[0].dados_b64 == base64.b64encode(JPEG).decode("ascii")

    b64 = base64.b64encode(JPEG).decode("ascii")
    assert b64 not in resultado.content
    assert b64[:32] not in resultado.content, "nem um pedaco do base64"
    assert len(resultado.content) < 300, "content e uma frase, nao um arquivo"


async def test_auditoria_e_so_metadado_serializavel(settings: Settings) -> None:
    """O que a trilha grava tem que caber num log e nao conter imagem."""
    ferramenta, _ = _ferramenta(settings)
    resultado = await ferramenta.execute(modo="ler")

    como_json = json.dumps(resultado.metadata)
    assert len(como_json) < 500
    assert base64.b64encode(JPEG).decode("ascii")[:24] not in como_json
    assert set(resultado.metadata) >= {
        "modo",
        "resolucao_entregue",
        "latencia_captura_ms",
        "timestamp",
        "tokens_estimados",
    }


async def test_tokens_estimados_acompanham_a_resolucao(settings: Settings) -> None:
    ferramenta, _ = _ferramenta(settings)
    descrever = await ferramenta.execute(modo="descrever")
    ler = await ferramenta.execute(modo="ler")

    assert descrever.metadata["tokens_estimados"] == 786
    assert ler.metadata["tokens_estimados"] == 1229


# ----------------------------------------------------------------- risco
def test_risco_e_externo_porque_o_quadro_sai_da_maquina(settings: Settings) -> None:
    """LEITURA descreveria a mecanica e esconderia a consequencia.

    A politica so exige confirmacao a partir de EXTERNO - classificar abaixo
    disso faria a camera acender sem ninguem autorizar.
    """
    ferramenta, _ = _ferramenta(settings)
    assert ferramenta.risk is RiskLevel.EXTERNO
    assert ferramenta.risk.ordem >= RiskLevel.EXTERNO.ordem


def test_resumo_falado_descreve_o_mundo_fisico(settings: Settings) -> None:
    """Quem confirma e uma pessoa decidindo se quer ser fotografada agora."""
    ferramenta, _ = _ferramenta(settings)

    frase = ferramenta.resumir({"modo": "ler", "motivo": "conferir o codigo do boleto"})
    assert "webcam" in frase
    assert "conferir o codigo do boleto" in frase
    assert "olhar" not in frase.split()[0], "nao e o nome da funcao"

    assert "ambiente" in ferramenta.resumir({"modo": "descrever"})


# ----------------------------------------------------------------- falhas
async def test_camera_indisponivel_vira_resposta_amigavel(settings: Settings) -> None:
    """Excecao crua mataria o turno e o usuario ouviria silencio."""
    ferramenta, criadas = _ferramenta(
        settings, erro=CameraIndisponivelError("camera 0 nao abriu")
    )
    resultado = await ferramenta.execute(modo="descrever")

    assert isinstance(resultado, ToolResult)
    assert resultado.is_error
    assert "camera" in resultado.content.lower()
    assert "outro programa" in resultado.content, "diz o que conferir"
    assert resultado.imagens == []
    assert criadas[0].liberada, "o dispositivo e devolvido mesmo com erro"


# ------------------------------------------------------- disponibilidade
async def test_visao_desligada_tira_a_ferramenta_do_schema(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """Interruptor para desligar a camera sem desinstalar nada."""
    ferramenta, _ = _ferramenta(settings)
    assert await ferramenta.available() is True

    monkeypatch.setenv("OPTMUS_VISION_ENABLED", "false")
    reset_settings_cache()
    desligada, _ = _ferramenta(get_settings())
    assert await desligada.available() is False


async def test_sem_opencv_a_ferramenta_nao_e_oferecida(settings: Settings) -> None:
    """Oferecer visao que nao funciona e pior que nao ter: o modelo inventaria
    o que "viu"."""
    import builtins

    ferramenta = OlharTool(settings)  # fabrica real, exige cv2
    original = builtins.__import__

    def sem_cv2(nome: str, *args: Any, **kw: Any) -> Any:
        if nome == "cv2":
            raise ImportError("No module named 'cv2'")
        return original(nome, *args, **kw)

    builtins.__import__ = sem_cv2
    try:
        assert await ferramenta.available() is False
    finally:
        builtins.__import__ = original


async def test_indice_da_camera_vem_da_configuracao(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setenv("OPTMUS_CAMERA_INDEX", "2")
    reset_settings_cache()
    ferramenta, criadas = _ferramenta(get_settings())
    await ferramenta.execute(modo="descrever")

    assert criadas[0].indice == 2


# --------------------------------------------------- integracao com a F4.1
async def test_a_imagem_do_olhar_passa_pelo_pipeline_generico(
    settings: Settings,
) -> None:
    """A F4.1 tem que valer para QUALQUER ferramenta com imagens.

    Se o OlharTool precisasse de tratamento especial no agente, seria sinal de
    que a F4.1 nao generalizou - e a proxima ferramenta com imagem repetiria o
    problema. Este teste roda a ferramenta de verdade dentro do laco do agente
    e confere que o bloco de imagem chegou ao modelo pelo caminho comum.
    """
    from core.agent import MARCADOR_IMAGEM, Agent, ToolOutcome
    from core.llm import LLMTurn
    from tests.fakes import FakeLLM, turno_de_ferramenta

    ferramenta, _ = _ferramenta(settings)

    class RegistroDeUmaFerramenta:
        """Registro minimo: so o contrato que o agente usa."""

        def schemas(self) -> list[dict[str, Any]]:
            return [ferramenta.to_schema()]

        async def execute(self, name: str, arguments: dict[str, Any], **_: Any) -> ToolOutcome:
            r = await ferramenta.execute(**arguments)
            return ToolOutcome(r.content, r.is_error, tuple(r.imagens))

    cliente = FakeLLM([
        turno_de_ferramenta("olhar", {"modo": "descrever"}, id_="t1"),
        LLMTurn(text="Uma caneca azul sobre a mesa.", stop_reason="end_turn"),
    ])
    resultado = await Agent(cliente, settings, tools=RegistroDeUmaFerramenta()).run(
        "o que voce esta vendo"
    )

    enviado = cliente.chamadas[-1]["messages"][-1]["content"][0]["content"]
    assert isinstance(enviado, list), "virou lista de blocos, sem caminho especial"
    assert enviado[1]["type"] == "image"
    assert enviado[1]["source"]["data"] == base64.b64encode(JPEG).decode("ascii")
    assert resultado.text == "Uma caneca azul sobre a mesa."
    assert MARCADOR_IMAGEM not in str(enviado), "a imagem recem-capturada sobrevive"
