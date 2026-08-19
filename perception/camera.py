"""Captura de um quadro da webcam local.

Este modulo NAO fala com o modelo e nao sabe o que e uma ferramenta. Ele abre a
camera, entrega um JPEG e devolve o dispositivo. Quem decide o que fazer com a
imagem e ``tools/impl/visao.py``.

Tres coisas aqui existem por causa de defeito real, nao de zelo:

1. **Descartar os primeiros quadros.** Webcam liga com auto-exposicao e
   auto-white-balance ainda convergindo. O primeiro quadro sai preto,
   esverdeado ou estourado - e o modelo descreve com toda a confianca um
   retangulo escuro, porque ele nao tem como saber que a foto e que estava
   ruim.

2. **Redimensionar como regra fixa, nunca como caso especial.** Custo de imagem
   e ``(largura x altura) / 750`` tokens. Uma webcam 4K entregando quadro cru
   custa ~11.000 tokens por olhada, contra 786 em 1024x576 - quatorze vezes
   mais caro pela mesma informacao. A regra vale sempre, e nao "quando a camera
   for grande".

3. **Liberar o dispositivo sempre.** Segurar o handle mantem o LED aceso e
   impede Meet, Zoom e qualquer outro programa de usar a camera. Um assistente
   que sequestra a webcam e desinstalado no primeiro dia.

O OpenCV entra por import tardio: o modulo carrega sem ele, os testes rodam sem
hardware, e a imagem do container - que nao tem camera nenhuma - nao carrega
40 MB de wheel para nada.

Custo medido numa webcam 720p real, com a escolha de backend de ``_abrir``::

    modo         nativa      entregue    latencia    tokens
    descrever    1280x720    1024x576     1843 ms       786
    ler          1280x720    1280x720     2147 ms      1229

Quase toda essa latencia e abertura de dispositivo e primeiro quadro; a
codificacao JPEG leva 3 ms. Vale para dimensionar expectativa: uma pergunta com
visao custa ~2 s a mais que uma sem, antes mesmo de falar com o modelo.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import UTC, datetime
from typing import Any

from core.logging import get_logger

log = get_logger("perception.camera")

# Quantos quadros jogar fora antes do que vale. Cinco cobre a convergencia de
# exposicao das webcams comuns sem passar de ~150 ms no total.
QUADROS_DE_AQUECIMENTO = 5

# Qualidade do JPEG. 85 e o ponto onde artefato de compressao ainda nao atrapalha
# leitura de texto pequeno, e o upload nao dobra de tamanho a toa.
QUALIDADE_JPEG = 85


class CameraIndisponivelError(RuntimeError):
    """A camera nao abriu: ausente, ocupada por outro programa ou sem permissao."""


class CameraCapture:
    """Um quadro da webcam local, na resolucao pedida ou menor.

    Use como gerenciador de contexto - e o que garante a devolucao do
    dispositivo mesmo quando a captura falha no meio::

        async with CameraCapture(1024, 576) as camera:
            jpeg, meta = await camera.capturar()

    O dispositivo abre em ``capturar()``, nao em ``__aenter__``, de proposito:
    assim ``CameraIndisponivelError`` acontece dentro do bloco, onde quem chama
    consegue tratar, em vez de estourar no ``async with``.
    """

    def __init__(
        self,
        largura_pedida: int = 1280,
        altura_pedida: int = 720,
        *,
        indice: int = 0,
        backend: Any | None = None,
    ) -> None:
        self.resolucao_pedida = (largura_pedida, altura_pedida)
        self.latencia_captura_ms: float = 0.0
        self.device: Any | None = None
        self._indice = indice
        # Injetavel para teste. None significa "importe o cv2 de verdade quando
        # precisar" - o import fica no metodo, nao no topo do arquivo.
        self._backend = backend

    # ------------------------------------------------------------- contexto
    async def __aenter__(self) -> CameraCapture:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.liberar()

    async def liberar(self) -> None:
        """Devolve o dispositivo. Idempotente e nao levanta.

        Chamado pelo ``__aexit__``, inclusive quando a captura levantou. Um erro
        aqui nao pode mascarar o erro original que trouxe a gente ate o
        ``__aexit__``, entao ele vira log e a vida segue.
        """
        device, self.device = self.device, None
        if device is None:
            return
        try:
            await asyncio.to_thread(device.release)
            log.debug("camera.liberada", indice=self._indice)
        except Exception as exc:  # noqa: BLE001 - liberar nunca derruba o turno
            log.warning("camera.falha_ao_liberar", indice=self._indice, erro=str(exc))

    # -------------------------------------------------------------- captura
    def _cv2(self) -> Any:
        if self._backend is not None:
            return self._backend
        try:
            import cv2
        except ImportError as exc:
            raise CameraIndisponivelError(
                "OpenCV nao instalado. Rode: pip install -e '.[visao]'"
            ) from exc
        self._backend = cv2
        return cv2

    async def capturar(self) -> tuple[bytes, dict[str, Any]]:
        """Um JPEG e os metadados da captura.

        Levanta ``CameraIndisponivelError`` se o dispositivo nao abrir ou nao
        entregar quadro.

        Tudo roda em thread separada: as chamadas do OpenCV sao bloqueantes e
        levam centenas de milissegundos. No loop de eventos elas travariam o
        Core inteiro - inclusive a resposta de voz em andamento.
        """
        cv2 = self._cv2()
        inicio = time.perf_counter()
        jpeg, meta = await asyncio.to_thread(self._capturar_bloqueante, cv2)
        self.latencia_captura_ms = round((time.perf_counter() - inicio) * 1000, 2)

        meta["latencia_captura_ms"] = self.latencia_captura_ms
        meta["timestamp"] = datetime.now(UTC).isoformat()
        log.info(
            "camera.capturou",
            **{k: v for k, v in meta.items() if k != "timestamp"},
            bytes=len(jpeg),
        )
        return jpeg, meta

    def _abrir(self, cv2: Any) -> Any:
        """Abre o dispositivo pelo backend mais rapido que funcionar.

        MEDIDO nesta maquina, com a mesma webcam, pedindo a mesma resolucao:

            backend      VideoCapture()   set() x2   total da captura
            MSMF (padrao)     4077 ms      6282 ms        11633 ms
            DirectShow         265 ms       513 ms         2555 ms

        O MSMF reinicia a pipeline inteira a cada ``set()`` - seis segundos so
        para sugerir uma resolucao. Como o Windows escolhe MSMF por padrao, uma
        ferramenta de visao custaria onze segundos por olhada sem esta linha.

        O fallback existe porque backend nao e garantia: se o DirectShow nao
        abrir esta camera, o padrao ainda tem chance, e onze segundos e melhor
        que "nao consigo ver".
        """
        preferido = getattr(cv2, "CAP_DSHOW", None) if sys.platform == "win32" else None
        if preferido is not None:
            device = cv2.VideoCapture(self._indice, preferido)
            if device.isOpened():
                return device
            log.warning("camera.backend_preferido_falhou", indice=self._indice, backend="DSHOW")
            device.release()
        return cv2.VideoCapture(self._indice)

    def _capturar_bloqueante(self, cv2: Any) -> tuple[bytes, dict[str, Any]]:
        """Parte sincrona da captura. Roda sempre fora do loop de eventos."""
        if self.device is None:
            self.device = self._abrir(cv2)

        device = self.device
        if not device.isOpened():
            raise CameraIndisponivelError(
                f"camera {self._indice} nao abriu - pode estar ausente, sem permissao "
                "ou em uso por outro programa"
            )

        # Pedir a resolucao ao driver e so uma sugestao: a maioria das webcams
        # entrega o modo suportado mais proximo, e algumas ignoram. Por isso a
        # resolucao real e medida no quadro, nunca assumida a partir daqui.
        largura, altura = self.resolucao_pedida
        device.set(cv2.CAP_PROP_FRAME_WIDTH, largura)
        device.set(cv2.CAP_PROP_FRAME_HEIGHT, altura)

        descartados = 0
        for _ in range(QUADROS_DE_AQUECIMENTO):
            ok, _quadro = device.read()
            if not ok:
                break
            descartados += 1

        ok, quadro = device.read()
        if not ok or quadro is None:
            raise CameraIndisponivelError(
                f"camera {self._indice} abriu mas nao entregou quadro"
            )

        nativa_h, nativa_w = int(quadro.shape[0]), int(quadro.shape[1])
        quadro, entregue = self._ajustar(cv2, quadro, (nativa_w, nativa_h))

        ok, buffer = cv2.imencode(".jpg", quadro, [int(cv2.IMWRITE_JPEG_QUALITY), QUALIDADE_JPEG])
        if not ok:
            raise CameraIndisponivelError("falha ao codificar o quadro em JPEG")

        return bytes(buffer), {
            "resolucao_nativa": (nativa_w, nativa_h),
            "resolucao_entregue": entregue,
            "redimensionado": entregue != (nativa_w, nativa_h),
            "quadros_descartados": descartados,
        }

    def _ajustar(
        self, cv2: Any, quadro: Any, nativa: tuple[int, int]
    ) -> tuple[Any, tuple[int, int]]:
        """Encolhe o quadro para caber no pedido. Nunca amplia.

        **Proporcao preservada.** Esticar 1600x1200 para 1024x576 caberia na
        conta de tokens e entregaria um mundo achatado - texto distorcido le
        pior, e o modelo descreveria as formas erradas. Uma camera 4:3 pedindo
        1024x576 recebe 768x576.

        **Nunca amplia**: aumentar uma webcam 640x480 para 1280x720 nao cria
        detalhe nenhum e mais que dobra o custo em tokens do mesmo conteudo.
        """
        nativa_w, nativa_h = nativa
        pedida_w, pedida_h = self.resolucao_pedida
        if nativa_w <= pedida_w and nativa_h <= pedida_h:
            return quadro, nativa

        escala = min(pedida_w / nativa_w, pedida_h / nativa_h)
        alvo = (max(1, round(nativa_w * escala)), max(1, round(nativa_h * escala)))
        # INTER_AREA e o filtro certo para reducao: media a area de origem em
        # vez de amostrar pontos, entao texto pequeno nao vira serrilhado.
        return cv2.resize(quadro, alvo, interpolation=cv2.INTER_AREA), alvo
