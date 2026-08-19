"""Ferramenta de visao: um quadro da webcam local para o modelo ver.

Sem OCR separado. O Claude tem visao nativa, entao a imagem vai inteira e ele
descreve, le texto e calcula em cima do que viu - tres capacidades por um
caminho so, em vez de um pipeline de OCR que erra em fonte pequena e perde o
contexto visual.

**Risco EXTERNO, nao LEITURA.** Localmente isto so le hardware. Mas o risco que
importa nao e ler: e que o quadro **sai da maquina** e vai para a Anthropic.
Classificar como LEITURA descreveria a mecanica e esconderia a consequencia - e
e a consequencia que a politica precisa poder barrar.

**A auditoria nunca ve a imagem.** ``ToolResult.content`` e texto e e o que o
``AuditLog`` grava; a imagem viaja em ``ToolResult.imagens``, que a trilha nao
toca. Essa separacao e da F4.1 e existe exatamente para isto: sem ela, o
primeiro descuido colocaria uma foto da sua sala em base64 num SQLite sem prazo
de validade.

Custo real de um turno com visao, medido de ponta a ponta com webcam e modelo
de verdade (duas capturas, tres rodadas)::

    rodada 1 (modelo decide chamar)   6864 ms
    olhar modo=descrever              1986 ms
    rodada 2 (modelo ve a imagem)     2354 ms
    olhar modo=ler                    2491 ms
    rodada 3 (modelo responde)        3154 ms
    ------------------------------------------
    TOTAL                            16851 ms

**A camera nao e o gargalo**: sao 4,5 s dos 16,9 s, 27%. O resto e raciocinio
do modelo, e a maior fatia unica e a rodada 1 - 6,9 s ANTES de a camera acender,
so para decidir usar a ferramenta. Se um dia isto precisar ficar mais rapido, e
ali que se mexe, nao na captura.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any, ClassVar

from core.config import Settings
from core.llm import Imagem
from core.logging import get_logger
from perception.camera import CameraCapture, CameraIndisponivelError
from security.policy import RiskLevel
from tools.registry import Tool, ToolResult

log = get_logger("tools.visao")

# Resolucao por modo. "descrever" nao precisa de pixel; "ler" precisa, porque
# economizar resolucao em OCR produz leitura errada com cara de certa - o pior
# resultado possivel, pior que nao ler.
RESOLUCOES: dict[str, tuple[int, int]] = {
    "descrever": (1024, 576),
    "ler": (1280, 720),
}

FabricaDeCamera = Callable[[int, int, int], CameraCapture]


def _camera_padrao(largura: int, altura: int, indice: int) -> CameraCapture:
    return CameraCapture(largura, altura, indice=indice)


class OlharTool(Tool):
    """Um quadro da webcam local, para o modelo descrever ou ler."""

    name = "olhar"
    risk = RiskLevel.EXTERNO
    description = (
        "Liga a webcam local, captura UM quadro e mostra a imagem para voce ver. "
        "Use quando o usuario perguntar o que voce esta vendo, pedir para olhar "
        "algo, ou pedir para ler algo que ele mostrar na frente da camera. "
        "Escolha modo='ler' quando houver texto a ler (papel, tela, etiqueta, "
        "codigo) - a imagem vem mais nitida. Use modo='descrever' para o resto. "
        "A camera acende e apaga a cada chamada: nao ha video continuo, e cada "
        "chamada custa uma foto nova. Nao chame de novo sem motivo novo."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "modo": {
                "type": "string",
                "enum": sorted(RESOLUCOES),
                "description": (
                    "descrever = ambiente, objetos, pessoas (mais leve); "
                    "ler = texto na frente da camera (mais nitido)"
                ),
            },
            "motivo": {
                "type": "string",
                "maxLength": 120,
                "description": (
                    "O que voce quer ver, em poucas palavras. Aparece na "
                    "confirmacao lida em voz alta para o usuario."
                ),
            },
        },
        "required": ["modo"],
        "additionalProperties": False,
    }

    def __init__(
        self, settings: Settings, *, fabrica: FabricaDeCamera | None = None
    ) -> None:
        self._settings = settings
        self._fabrica = fabrica or _camera_padrao

    async def available(self) -> bool:
        """Sem OpenCV ou com a visao desligada, a ferramenta nem e oferecida.

        Oferecer uma ferramenta que nao pode funcionar e pior do que nao ter: o
        modelo tenta, falha e improvisa uma descricao. Aqui isso seria pior
        ainda - ele inventaria o que "viu".
        """
        if not self._settings.vision_enabled:
            return False
        if self._fabrica is not _camera_padrao:
            return True  # camera injetada em teste
        try:
            import cv2  # noqa: F401
        except ImportError:
            log.info(
                "visao.indisponivel",
                motivo="OpenCV ausente",
                acao='pip install -e ".[visao]"',
            )
            return False
        return True

    def resumir(self, parametros: dict[str, Any]) -> str:
        """Frase lida em voz alta antes de acender a camera.

        Precisa dizer o que vai acontecer no mundo fisico - "vou ligar a
        camera" -, nao o nome de uma funcao. Quem confirma e uma pessoa
        decidindo se quer ser fotografada agora.
        """
        modo = str(parametros.get("modo", "descrever"))
        acao = "ler o que estiver na frente da camera" if modo == "ler" else "olhar o ambiente"
        motivo = str(parametros.get("motivo") or "").strip()
        complemento = f" para {motivo}" if motivo else ""
        return f"ligar a webcam e {acao}{complemento}"

    async def execute(self, **kwargs: Any) -> ToolResult:
        modo = str(kwargs.get("modo", "descrever"))
        if modo not in RESOLUCOES:
            return ToolResult.erro(
                f"modo invalido: {modo}. Use 'descrever' ou 'ler'.", modo=modo
            )
        largura, altura = RESOLUCOES[modo]

        # Gerenciador de contexto: e o que devolve o dispositivo mesmo quando a
        # captura levanta no meio. Camera presa mantem o LED aceso e bloqueia
        # qualquer outro programa.
        async with self._fabrica(largura, altura, self._settings.camera_index) as camera:
            try:
                jpeg, meta = await camera.capturar()
            except CameraIndisponivelError as exc:
                # Erro de ferramenta e resposta, nao excecao: o modelo precisa
                # poder dizer ao usuario o que houve e seguir a conversa.
                log.warning("visao.camera_indisponivel", modo=modo, erro=str(exc))
                return ToolResult.erro(
                    "Nao consegui acessar a camera. Verifique se ela esta conectada "
                    "e se nenhum outro programa esta usando.",
                    modo=modo,
                    erro="camera_indisponivel",
                )

        largura_real, altura_real = meta["resolucao_entregue"]
        imagem = Imagem(
            dados_b64=base64.b64encode(jpeg).decode("ascii"),
            media_type="image/jpeg",
            largura=largura_real,
            altura=altura_real,
            origem="webcam",
        )

        pedido = (
            "Leia o texto que aparece na imagem. Se algo estiver ilegivel, diga "
            "que nao deu para ler em vez de adivinhar."
            if modo == "ler"
            else "Descreva o que aparece na imagem."
        )
        # SO TEXTO aqui. A imagem vai em `imagens`, e e o que mantem a foto fora
        # da trilha de auditoria, que grava content.
        conteudo = (
            f"Quadro capturado da webcam em {meta['timestamp']}: "
            f"{largura_real}x{altura_real}, modo {modo}, "
            f"{meta['latencia_captura_ms']:.0f} ms. {pedido}"
        )

        return ToolResult(
            content=conteudo,
            imagens=[imagem],
            # Metadados, nunca a imagem: e o que vai para log e diagnostico.
            metadata={
                "modo": modo,
                "resolucao_nativa": meta["resolucao_nativa"],
                "resolucao_entregue": meta["resolucao_entregue"],
                "redimensionado": meta["redimensionado"],
                "quadros_descartados": meta["quadros_descartados"],
                "latencia_captura_ms": meta["latencia_captura_ms"],
                "timestamp": meta["timestamp"],
                "tokens_estimados": imagem.tokens_estimados,
                "bytes_jpeg": len(jpeg),
            },
        )
