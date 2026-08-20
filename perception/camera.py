"""Captura de um quadro da webcam local.

Este modulo NAO fala com o modelo e nao sabe o que e uma ferramenta. Ele abre a
camera, entrega um JPEG e devolve o dispositivo. Quem decide o que fazer com a
imagem e ``tools/impl/visao.py``.

Tres coisas aqui existem por causa de defeito real, nao de zelo:

1. **Esperar a exposicao assentar, e nao contar quadros.** Webcam liga com
   auto-exposicao ainda convergindo, e o tempo disso depende da cena. Medido
   apontando para uma tela clara num quarto escuro::

       quadro    brilho   pixels saturados
            0     180,6            57,1%
            5     227,4            80,3%   <- onde o codigo capturava
           10     182,4            32,3%
           20     140,9             2,1%
           39      99,7             0,0%   <- texto legivel

   Um numero fixo de 5 nao era so insuficiente: caia perto do PICO da
   superexposicao, e a foto saia um retangulo branco. Ver ``_aquecer``.

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
visao custa ~2 s a mais que uma sem, antes mesmo de falar com o modelo. Cena
clara paga mais, porque o aquecimento espera a exposicao assentar - foi de
2,1 s para 4,1 s com uma tela branca no quadro, e valeu: e a diferenca entre
uma foto legivel e um retangulo branco.

Os quatro defeitos que a webcam de verdade revelou, com a medicao de cada um,
estao em ``docs/CAMERA.md``. Nenhum deles aparece em teste com mock.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import UTC, datetime
from typing import Any

from core.logging import get_logger

log = get_logger("perception.camera")

# Aquecimento ADAPTATIVO: descarta quadros ate a exposicao parar de mudar.
#
# Antes isto era um numero fixo (5), e o numero estava errado do jeito mais
# traicoeiro possivel. Medido apontando a camera para uma tela clara num quarto
# escuro:
#
#     quadro    brilho   pixels saturados
#          0     180,6            57,1%
#          5     227,4            80,3%   <- onde o codigo capturava
#         10     182,4            32,3%
#         20     140,9             2,1%
#         39      99,7             0,0%   <- texto legivel
#
# O quadro 5 nao era so insuficiente: ficava perto do PICO da superexposicao. A
# foto saia um retangulo branco, e o modelo - corretamente - dizia que nao dava
# para ler. Num quarto escuro e uniforme a exposicao ja nasce estavel e o laco
# sai em poucos quadros; e diante de algo claro que ele ganha o tempo que
# precisa. Numero fixo nao cobre os dois casos.
#
# O criterio e DUPLO, porque cada metade falha num cenario oposto:
#
# - so estabilidade de brilho: uma parede branca de verdade assenta em 250 com
#   60% dos pixels estourados. Sairia "convergido" com a foto ilegivel.
# - so saturacao: um quarto escuro tem 0% de saturacao desde o quadro zero.
#   Sairia no primeiro quadro, sem aquecimento nenhum - que e justamente o que
#   o piso existe para impedir.
#
# Sai quando o brilho parou de mudar E a saturacao esta aceitavel, respeitando
# o piso. Se um dos dois nao acontecer, quem manda e o teto.
QUADROS_DE_AQUECIMENTO = 5
QUADROS_MAXIMOS = 45
# Variacao de brilho entre quadros abaixo da qual consideramos estavel.
ESTABILIDADE = 0.015
QUADROS_ESTAVEIS = 3
# Fracao de pixels estourados que ainda permite ler texto. Acima disso o branco
# vira chapado e a letra some junto.
SATURACAO_MAXIMA = 0.05
# Teto de tempo: cena que pisca (monitor, lampada) nunca estabiliza, e esperar
# para sempre seria pior que uma foto imperfeita.
TEMPO_MAXIMO_MS = 3000.0
# Amostragem 1 em 4 nas duas dimensoes: 1/16 dos pixels. Medir brilho e
# saturacao no quadro inteiro custa ~4 ms, e a 45 quadros isso e um quinto de
# segundo gasto para refinar uma media que ja e estavel na amostra.
PASSO_DA_AMOSTRA = 4

# Qualidade do JPEG. 85 e o ponto onde artefato de compressao ainda nao atrapalha
# leitura de texto pequeno, e o upload nao dobra de tamanho a toa.
QUALIDADE_JPEG = 85


def _metricas(quadro: Any) -> tuple[float, float]:
    """Brilho medio e fracao de pixels estourados, sobre uma amostra do quadro.

    A amostra existe por custo: medir os 2,7 milhoes de valores de um quadro
    720p leva alguns milissegundos, e o laco de aquecimento faz isso ate 45
    vezes. Um em cada quatro pixels nas duas dimensoes da a mesma resposta para
    medias globais.
    """
    amostra = quadro[::PASSO_DA_AMOSTRA, ::PASSO_DA_AMOSTRA]
    return float(amostra.mean()), float((amostra > 250).mean())


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

        descartados, convergiu, saturacao, quadro = self._aquecer(device)
        if quadro is None:
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
            # Falso significa "a foto pode estar clara ou escura demais". Quem
            # le texto precisa saber disso: e a diferenca entre "nao ha texto"
            # e "havia texto e a foto estourou".
            "exposicao_convergiu": convergiu,
            "saturacao": round(saturacao, 4),
        }

    def _aquecer(self, device: Any) -> tuple[int, bool, float, Any]:
        """Le quadros ate a exposicao assentar.

        Devolve ``(descartados, convergiu, saturacao, quadro)``. O ultimo quadro
        lido e o que vale - nao ha leitura extra depois do laco, que so gastaria
        tempo para devolver a mesma cena.
        """
        anterior: float | None = None
        estaveis = 0
        saturacao = 0.0
        quadro = None
        lidos = 0
        limite = time.perf_counter() + TEMPO_MAXIMO_MS / 1000

        for i in range(QUADROS_MAXIMOS):
            ok, novo = device.read()
            if not ok or novo is None:
                break
            quadro, lidos = novo, i + 1

            brilho, saturacao = _metricas(novo)
            if anterior is not None:
                variacao = abs(brilho - anterior) / max(anterior, 1.0)
                estaveis = estaveis + 1 if variacao < ESTABILIDADE else 0
            anterior = brilho

            pronto = (
                lidos >= QUADROS_DE_AQUECIMENTO
                and estaveis >= QUADROS_ESTAVEIS
                and saturacao <= SATURACAO_MAXIMA
            )
            if pronto:
                return max(lidos - 1, 0), True, saturacao, quadro
            if time.perf_counter() > limite:
                self._avisar_sem_convergir(lidos, saturacao, estaveis, "tempo maximo")
                return max(lidos - 1, 0), False, saturacao, quadro

        if quadro is not None:
            self._avisar_sem_convergir(lidos, saturacao, estaveis, "teto de quadros")
        return max(lidos - 1, 0), False, saturacao, quadro

    def _avisar_sem_convergir(
        self, quadros: int, saturacao: float, estaveis: int, motivo: str
    ) -> None:
        """Diz QUAL das duas metades falhou - as causas e os consertos diferem.

        Saturacao alta com brilho estavel e cena clara demais para a camera:
        afaste o objeto ou acenda a luz do ambiente. Brilho instavel e cena que
        pisca, tipicamente um monitor.
        """
        culpa = (
            "saturacao acima do limite" if saturacao > SATURACAO_MAXIMA
            else "brilho ainda oscilando"
        )
        log.warning(
            "camera.exposicao_nao_convergiu",
            quadros=quadros,
            motivo=motivo,
            causa=culpa,
            saturacao=round(saturacao, 3),
            quadros_estaveis=estaveis,
            impacto="a foto pode estar clara ou escura demais para ler texto",
        )

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
