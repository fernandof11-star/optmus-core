"""Captura de webcam: resolucao, aquecimento e devolucao do dispositivo.

Sem OpenCV e sem camera. O ``FakeCv2`` implementa a fatia da API que o
``CameraCapture`` usa e conta o que foi chamado - e a contagem que prova o
aquecimento e a liberacao, coisas que nao aparecem no valor de retorno.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from perception.camera import (
    QUADROS_DE_AQUECIMENTO,
    CameraCapture,
    CameraIndisponivelError,
)


class QuadroFalso:
    """So o que o codigo le de um quadro do OpenCV: ``shape``."""

    def __init__(self, largura: int, altura: int) -> None:
        self.shape = (altura, largura, 3)


class DeviceFalso:
    def __init__(
        self,
        *,
        nativa: tuple[int, int] = (1920, 1080),
        abre: bool = True,
        leituras_ok: int | None = None,
    ) -> None:
        self.nativa = nativa
        self._abre = abre
        # None = sempre entrega. Numero = entrega N vezes e depois falha.
        self._leituras_ok = leituras_ok
        self.leituras = 0
        self.liberado = 0
        self.resolucao_pedida_ao_driver: list[tuple[int, float]] = []

    def isOpened(self) -> bool:
        return self._abre

    def set(self, prop: int, valor: float) -> bool:
        self.resolucao_pedida_ao_driver.append((prop, valor))
        return True

    def read(self) -> tuple[bool, Any]:
        if self._leituras_ok is not None and self.leituras >= self._leituras_ok:
            self.leituras += 1
            return False, None
        self.leituras += 1
        return True, QuadroFalso(*self.nativa)

    def release(self) -> None:
        self.liberado += 1


class FakeCv2:
    CAP_PROP_FRAME_WIDTH = 3
    CAP_PROP_FRAME_HEIGHT = 4
    IMWRITE_JPEG_QUALITY = 1
    INTER_AREA = 3
    CAP_DSHOW = 700

    def __init__(
        self,
        device: DeviceFalso,
        *,
        encode_ok: bool = True,
        device_fallback: DeviceFalso | None = None,
    ) -> None:
        self._device = device
        # Quando o backend preferido nao abre, a segunda tentativa recebe este.
        self._fallback = device_fallback
        self._encode_ok = encode_ok
        self.aberturas = 0
        self.backends_tentados: list[int | None] = []
        self.redimensionamentos: list[tuple[int, int]] = []

    def VideoCapture(self, indice: int, api: int | None = None) -> DeviceFalso:
        self.aberturas += 1
        self.backends_tentados.append(api)
        if api is None and self._fallback is not None:
            return self._fallback
        return self._device

    def resize(self, quadro: Any, alvo: tuple[int, int], interpolation: int = 0) -> QuadroFalso:
        self.redimensionamentos.append(alvo)
        return QuadroFalso(*alvo)

    def imencode(self, ext: str, quadro: Any, params: list[int]) -> tuple[bool, Any]:
        if not self._encode_ok:
            return False, None
        # JPEG de verdade comeca com FF D8 FF e termina com FF D9.
        return True, bytearray(b"\xff\xd8\xff" + b"corpo" + b"\xff\xd9")


def _camera(device: DeviceFalso, largura: int = 1024, altura: int = 576, **kw: Any) -> tuple[
    CameraCapture, FakeCv2
]:
    cv2 = FakeCv2(device, **kw)
    return CameraCapture(largura, altura, backend=cv2), cv2


# ------------------------------------------------------------------- captura
async def test_captura_devolve_jpeg_e_metadados_completos() -> None:
    device = DeviceFalso(nativa=(1920, 1080))
    camera, _ = _camera(device)

    async with camera:
        jpeg, meta = await camera.capturar()

    assert jpeg.startswith(b"\xff\xd8\xff"), "cabecalho de JPEG"
    assert jpeg.endswith(b"\xff\xd9"), "fim de JPEG"
    assert set(meta) == {
        "resolucao_nativa",
        "resolucao_entregue",
        "redimensionado",
        "quadros_descartados",
        "latencia_captura_ms",
        "timestamp",
    }
    assert meta["resolucao_nativa"] == (1920, 1080)


async def test_latencia_de_captura_e_medida() -> None:
    """Precisa ser numero proprio: a latencia do modelo se mede separada."""
    device = DeviceFalso()
    camera, _ = _camera(device)

    async with camera:
        _, meta = await camera.capturar()

    assert isinstance(meta["latencia_captura_ms"], float)
    assert meta["latencia_captura_ms"] > 0
    assert camera.latencia_captura_ms == meta["latencia_captura_ms"]


# -------------------------------------------------------------- resolucao
async def test_camera_4k_cai_para_a_resolucao_pedida() -> None:
    """Regra fixa, nao caso especial.

    3840x2160 cru custaria ~11.000 tokens por olhada; 1024x576 custa 786.
    """
    device = DeviceFalso(nativa=(3840, 2160))
    camera, cv2 = _camera(device, 1024, 576)

    async with camera:
        _, meta = await camera.capturar()

    assert meta["resolucao_entregue"] == (1024, 576)
    assert meta["redimensionado"] is True
    assert cv2.redimensionamentos == [(1024, 576)]


async def test_camera_menor_que_a_pedida_nao_e_esticada() -> None:
    """Ampliar nao cria detalhe e mais que dobra o custo do mesmo conteudo."""
    device = DeviceFalso(nativa=(640, 480))
    camera, cv2 = _camera(device, 1280, 720)

    async with camera:
        _, meta = await camera.capturar()

    assert meta["resolucao_entregue"] == (640, 480)
    assert meta["redimensionado"] is False
    assert cv2.redimensionamentos == [], "nem deve chamar resize"


async def test_proporcao_e_preservada_ao_encolher() -> None:
    """Camera 4:3 pedindo 16:9 recebe 768x576, nao 1024x576 achatado.

    Esticar caberia na conta de tokens e entregaria um mundo distorcido: texto
    esticado le pior, e o modelo descreveria formas erradas.
    """
    device = DeviceFalso(nativa=(1600, 1200))
    camera, _ = _camera(device, 1024, 576)

    async with camera:
        _, meta = await camera.capturar()

    assert meta["resolucao_entregue"] == (768, 576)
    largura, altura = meta["resolucao_entregue"]
    assert round(largura / altura, 3) == round(1600 / 1200, 3), "mesma proporcao da nativa"


# ------------------------------------------------------------- aquecimento
async def test_primeiros_quadros_sao_descartados() -> None:
    """O primeiro quadro sai preto ou esverdeado, e o modelo o descreveria."""
    device = DeviceFalso()
    camera, _ = _camera(device)

    async with camera:
        _, meta = await camera.capturar()

    assert meta["quadros_descartados"] == QUADROS_DE_AQUECIMENTO
    assert device.leituras == QUADROS_DE_AQUECIMENTO + 1, "descartados + o que vale"


# ---------------------------------------------------- devolucao do device
async def test_device_e_liberado_apos_uso_normal() -> None:
    """LED aceso e camera travada para Meet e Zoom se isto falhar."""
    device = DeviceFalso()
    camera, _ = _camera(device)

    async with camera:
        await camera.capturar()

    assert device.liberado == 1


async def test_device_e_liberado_mesmo_com_erro_na_captura() -> None:
    """O caminho que importa: o dispositivo nao pode vazar quando algo quebra."""
    device = DeviceFalso(leituras_ok=0)
    camera, _ = _camera(device)

    with pytest.raises(CameraIndisponivelError):
        async with camera:
            await camera.capturar()

    assert device.liberado == 1, "liberado apesar da excecao"


async def test_liberar_e_idempotente() -> None:
    device = DeviceFalso()
    camera, _ = _camera(device)

    async with camera:
        await camera.capturar()
    await camera.liberar()

    assert device.liberado == 1, "sair do contexto e liberar de novo nao libera duas vezes"


async def test_falha_ao_liberar_nao_mascara_o_erro_original() -> None:
    """Um release() quebrado nao pode esconder por que a captura falhou."""

    class DeviceQueQuebraAoLiberar(DeviceFalso):
        def release(self) -> None:
            raise OSError("dispositivo sumiu")

    device = DeviceQueQuebraAoLiberar(leituras_ok=0)
    camera, _ = _camera(device)

    with pytest.raises(CameraIndisponivelError, match="nao entregou quadro"):
        async with camera:
            await camera.capturar()


# ------------------------------------------------------------------- erros
async def test_camera_que_nao_abre_levanta_erro_tratavel() -> None:
    """Camera ausente ou ocupada nao pode derrubar o processo."""
    device = DeviceFalso(abre=False)
    camera, _ = _camera(device)

    async with camera:
        with pytest.raises(CameraIndisponivelError, match="nao abriu"):
            await camera.capturar()


async def test_falha_de_codificacao_vira_erro_tratavel() -> None:
    device = DeviceFalso()
    camera, _ = _camera(device, encode_ok=False)

    async with camera:
        with pytest.raises(CameraIndisponivelError, match="JPEG"):
            await camera.capturar()


async def test_sem_opencv_a_mensagem_diz_o_que_instalar() -> None:
    """O modulo carrega sem OpenCV; so a captura exige o extra."""
    camera = CameraCapture(1024, 576)
    camera._backend = None
    import builtins

    original = builtins.__import__

    def sem_cv2(nome: str, *args: Any, **kw: Any) -> Any:
        if nome == "cv2":
            raise ImportError("No module named 'cv2'")
        return original(nome, *args, **kw)

    builtins.__import__ = sem_cv2
    try:
        with pytest.raises(CameraIndisponivelError, match=r"\[visao\]"):
            await camera.capturar()
    finally:
        builtins.__import__ = original


async def test_resolucao_e_sugerida_ao_driver() -> None:
    """Pedir ao driver e barato e evita downscale de 4K a cada quadro."""
    device = DeviceFalso()
    camera, cv2 = _camera(device, 1024, 576)

    async with camera:
        await camera.capturar()

    pedidos = dict(device.resolucao_pedida_ao_driver)
    assert pedidos[cv2.CAP_PROP_FRAME_WIDTH] == 1024
    assert pedidos[cv2.CAP_PROP_FRAME_HEIGHT] == 576


async def test_device_e_aberto_uma_vez_so() -> None:
    """Duas capturas no mesmo contexto reusam o dispositivo ja aberto."""
    device = DeviceFalso()
    camera, cv2 = _camera(device)

    async with camera:
        await camera.capturar()
        await camera.capturar()

    assert cv2.aberturas == 1


# ----------------------------------------------------------------- backend
async def test_no_windows_prefere_directshow(monkeypatch: pytest.MonkeyPatch) -> None:
    """MEDIDO na mesma webcam: MSMF 11.633 ms, DirectShow 2.555 ms.

    O MSMF reinicia a pipeline a cada set() - 6 s so para sugerir resolucao.
    Como o Windows escolhe MSMF por padrao, sem esta preferencia a ferramenta
    de visao custaria onze segundos por olhada.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    device = DeviceFalso()
    camera, cv2 = _camera(device)

    async with camera:
        await camera.capturar()

    assert cv2.backends_tentados == [cv2.CAP_DSHOW], "abriu direto pelo DirectShow"


async def test_fora_do_windows_usa_o_backend_padrao(monkeypatch: pytest.MonkeyPatch) -> None:
    """CAP_DSHOW e API do Windows; em Linux o padrao (V4L2) e o certo."""
    monkeypatch.setattr(sys, "platform", "linux")
    device = DeviceFalso()
    camera, cv2 = _camera(device)

    async with camera:
        await camera.capturar()

    assert cv2.backends_tentados == [None]


async def test_backend_preferido_que_nao_abre_cai_para_o_padrao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend nao e garantia. Onze segundos e melhor que "nao consigo ver"."""
    monkeypatch.setattr(sys, "platform", "win32")
    recusa = DeviceFalso(abre=False)
    aceita = DeviceFalso(nativa=(1280, 720))
    camera, cv2 = _camera(recusa, device_fallback=aceita)

    async with camera:
        _, meta = await camera.capturar()

    assert cv2.backends_tentados == [cv2.CAP_DSHOW, None], "tentou o preferido, depois o padrao"
    assert recusa.liberado == 1, "o dispositivo que nao abriu tambem e devolvido"
    assert meta["resolucao_nativa"] == (1280, 720), "capturou pelo fallback"
