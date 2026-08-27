"""Loop de voz: wake -> STT -> roteador -> LLM -> TTS.

Este e o unico lugar que conhece a ordem das etapas. Cada uma delas e um
componente trocavel, cronometrado e publicado como evento no barramento -
o HUD da F4 renderiza o estado do sistema so escutando esses eventos.

Duas coisas fazem o sistema parecer vivo:

1. **Camada 1 antes do LLM.** "que horas sao" responde sem tocar na rede.
2. **Fala antes de terminar de pensar.** Os deltas do modelo entram numa fila
   e um consumidor separado sintetiza frase a frase. Se a sintese ficasse no
   caminho da leitura do stream, o modelo teria que esperar o audio terminar
   para continuar gerando.

O caminho de texto (:meth:`handle_text`) e o mesmo do caminho de voz a partir
do roteador. Isso nao e detalhe de teste: e como o HUD, a API e, na F6, o
WhatsApp entram no assistente sem duplicar pipeline.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.agent import Agent
from core.bus import EventBus
from core.config import Settings
from core.llm import Imagem
from core.logging import get_logger
from core.metrics import LatencyTracker, TurnMetrics
from core.router import Acao, Camada, IntentRouter
from expression.tts import SpeechSynthesizer
from memory.system import MemorySystem
from perception.stt import Transcriber
from perception.vad import UtteranceCapture
from perception.wake import WakeDetector
from security.dispositivos import ORIGEM_DESCONHECIDA, ORIGEM_VOZ, origem

log = get_logger("core.voice_loop")


@dataclass(slots=True)
class TurnOutcome:
    turn_id: str
    entrada: str
    resposta: str
    camada: Camada
    acao: Acao = Acao.RESPONDER
    regra: str | None = None
    rodadas: int = 0
    falado: bool = False
    erro: str | None = None
    latencia: dict[str, Any] = field(default_factory=dict)


class VoiceLoop:
    """Orquestra um turno de conversa, por voz ou por texto."""

    def __init__(
        self,
        settings: Settings,
        *,
        bus: EventBus,
        router: IntentRouter,
        agent: Agent,
        synthesizer: SpeechSynthesizer,
        tracker: LatencyTracker,
        memory: MemorySystem | None = None,
        transcriber: Transcriber | None = None,
        wake: WakeDetector | None = None,
        capture: UtteranceCapture | None = None,
    ) -> None:
        self._settings = settings
        self._bus = bus
        self._router = router
        self._agent = agent
        self._synth = synthesizer
        self._tracker = tracker
        self._memory = memory
        self._transcriber = transcriber
        self._wake = wake
        self._capture = capture or UtteranceCapture(settings)

        self._turno: TurnMetrics | None = None
        self._falando: asyncio.Task[str] | None = None
        self._silenciado = False
        self.turnos = 0

    def attach_voice_io(self, transcriber: Transcriber, wake: WakeDetector) -> None:
        """Liga microfone e transcricao depois do boot.

        O loop nasce util so com texto; a escuta e um upgrade que pode falhar
        (sem microfone, sem modelo) sem levar o assistente junto.
        """
        self._transcriber = transcriber
        self._wake = wake

    @property
    def escutando(self) -> bool:
        return self._wake is not None and self._transcriber is not None

    async def trigger_wake(self) -> bool:
        """Gatilho manual de wake word (HUD, atalho fisico, API).

        Devolve ``False`` quando nao ha escuta montada - sem microfone ou sem
        STT nao existe turno para abrir. Falhar em silencio aqui produz o pior
        tipo de bug: a chamada "funciona", nada acontece e nao ha log para
        seguir.
        """
        if self._wake is None or self._transcriber is None:
            log.warning(
                "voz.gatilho_ignorado",
                motivo="escuta nao montada",
                tem_wake=self._wake is not None,
                tem_stt=self._transcriber is not None,
                acao="ligue OPTMUS_VOICE_ENABLED com o extra [voz], ou use /voz/texto",
            )
            return False
        await self._wake.trigger()
        log.info("voz.gatilho_manual", wake=self._wake.name)
        return True

    # ------------------------------------------------------------- texto
    async def handle_text(
        self,
        texto: str,
        *,
        source: str = "api",
        turn_id: str | None = None,
        falar: bool = True,
    ) -> TurnOutcome:
        """Processa uma fala ja transcrita. Caminho comum de voz, HUD e API.

        ``falar=False`` para cliente que sintetiza do proprio lado - o
        navegador, por exemplo. Sem isso o Core fala em voz alta na MAQUINA
        ONDE ELE RODA quando alguem manda mensagem pelo site, e ainda cobra a
        espera: medido em 3,8 s de TTS num turno cuja resposta ja estava
        pronta em 2 ms.
        """
        turn_id = turn_id or uuid.uuid4().hex[:12]
        turno = self._turno or TurnMetrics(turn_id)
        self._turno = turno
        self._synth.set_first_audio_hook(lambda: turno.mark("primeira_silaba"))
        self.turnos += 1

        await self._bus.emit(
            "voz.entrada", source=source, payload={"texto": texto}, correlation_id=turn_id
        )

        with turno.stage("router"):
            rota = self._router.route(texto)

        if rota.acao is Acao.PARAR:
            await self.stop_speaking()
            return await self._finalizar(
                TurnOutcome(turn_id, texto, rota.resposta or "", rota.camada,
                            acao=rota.acao, regra=rota.regra),
                turno,
                falar=falar,
            )

        if rota.acao is Acao.SILENCIAR:
            self._silenciado = True
            await self.stop_speaking()
            return await self._finalizar(
                TurnOutcome(turn_id, texto, "", rota.camada, acao=rota.acao, regra=rota.regra),
                turno,
                falar=False,
            )

        if rota.resolvido:
            return await self._finalizar(
                TurnOutcome(turn_id, texto, rota.resposta or "", rota.camada, regra=rota.regra),
                turno,
                falar=falar,
            )

        return await self._rodar_agente(texto, turn_id, turno, falar=falar)

    async def retomar_apos_confirmacao(
        self,
        *,
        ferramenta: str,
        resumo: str,
        resultado: str,
        erro: bool = False,
        imagens: Sequence[Imagem] = (),
        origem: str = ORIGEM_DESCONHECIDA,
        correlation_id: str | None = None,
    ) -> TurnOutcome:
        """Devolve ao agente o que uma acao confirmada produziu.

        ## O buraco que isto fecha

        A confirmacao acontece FORA do turno: o turno que pediu a acao ja
        terminou - tinha que terminar, porque o usuario precisava ouvir a
        pergunta. Quando o "sim" chegava por HTTP, ``executar_confirmado``
        rodava a ferramenta e devolvia o resultado para... a resposta HTTP. O
        agente nunca via. Com a camera isso era literal: ela capturava, o
        quadro ia para o campo ``imagens``, e ninguem olhava.

        ## Turno novo, e nao retomada do antigo

        A alternativa seria suspender o laco do agente esperando o "sim" e
        responder o ``tool_use`` original com o resultado de verdade. Nao da,
        por tres motivos concretos: aquele ``tool_use`` **ja foi respondido**
        com "AGUARDANDO CONFIRMACAO" e nao se responde duas vezes; segurar uma
        corrotina viva por ate 120 s prenderia ``self._turno``, que e slot
        unico, bloqueando qualquer outra fala; e um reinicio do processo
        perderia a acao no meio.

        Entao entra como turno novo, com o resultado no texto de abertura e a
        foto anexada. O historico ja tem a pergunta e o "posso ligar a webcam?",
        entao a conversa continua coerente.

        ## Quem ouve a resposta

        ``origem`` decide, e por isso o vinculo de dispositivo veio antes: se a
        pendencia nasceu da voz, a pessoa nao esta olhando para a tela e a
        resposta e falada. Se nasceu do HUD, quem sintetiza e o navegador -
        falar aqui faria o Core falar em voz alta na maquina onde ele roda.
        """
        turn_id = correlation_id or uuid.uuid4().hex[:12]
        turno = TurnMetrics(f"{turn_id}-retomada")
        self._turno = turno

        # Texto de evento, nao de usuario. O modelo precisa saber que isto e o
        # desfecho de algo que ELE pediu e um humano autorizou - se parecesse
        # fala do usuario, ele responderia como se tivessem lhe contado.
        situacao = "falhou" if erro else "foi autorizada e executada"
        abertura = (
            f"[sistema] A acao pendente ({resumo}) {situacao}. "
            f"Resultado de {ferramenta}: {resultado}. "
            "Comente o resultado para o usuario, em uma ou duas frases. "
            "Nao chame a ferramenta de novo."
        )

        falar = origem == ORIGEM_VOZ
        log.info(
            "voz.retomada",
            ferramenta=ferramenta,
            origem=origem,
            imagens=len(imagens),
            falar=falar,
        )
        return await self._rodar_agente(
            abertura, turn_id, turno, falar=falar, imagens=imagens, entrada_visivel=resumo
        )

    async def _rodar_agente(
        self,
        texto: str,
        turn_id: str,
        turno: TurnMetrics,
        *,
        falar: bool = True,
        imagens: Sequence[Imagem] = (),
        entrada_visivel: str | None = None,
    ) -> TurnOutcome:
        self._synth.resume()

        # Recuperar ANTES de falar com o modelo: perfil e memoria entram no
        # prompt de sistema, nao numa segunda chamada depois.
        contexto = ""
        historico: list[dict[str, Any]] = []
        if self._memory is not None:
            with turno.stage("memoria"):
                contexto = await self._memory.context_for(texto)
                historico = self._memory.working.messages()

        fila: asyncio.Queue[str | None] = asyncio.Queue()

        async def deltas() -> AsyncIterator[str]:
            while (pedaco := await fila.get()) is not None:
                yield pedaco

        # Sem sintetizador quando quem pediu fala do proprio lado. Nao basta
        # descartar o audio no fim: a tarefa de TTS consome a fila em tempo de
        # fala, e o turno so termina quando ela termina.
        falante: asyncio.Task[str] | None = None
        if falar:
            falante = asyncio.create_task(
                self._synth.speak_stream(deltas()), name=f"tts-{turn_id}"
            )
            self._falando = falante

        with turno.stage("llm"):
            resultado = await self._agent.run(
                texto,
                history=historico,
                context=contexto,
                # on_text so faz sentido com alguem consumindo a fila.
                on_text=fila.put if falar else None,
                correlation_id=turn_id,
                imagens=imagens,
            )

        await fila.put(None)
        if falante is not None:
            with turno.stage("tts_restante"):
                try:
                    await falante
                except asyncio.CancelledError:
                    log.info("voz.fala_interrompida", turn_id=turn_id)
            self._falando = None

        if self._memory is not None:
            with turno.stage("memoria_gravacao"):
                # Grava o que a pessoa reconheceria, nao o andaime "[sistema]
                # A acao pendente...". A memoria episodica e lida por humano e
                # realimenta turnos futuros; um bloco de instrucao interna ali
                # vira contexto falso na proxima recuperacao.
                await self._memory.record_turn(
                    entrada_visivel or texto, resultado.text, correlation_id=turn_id
                )

        return await self._finalizar(
            TurnOutcome(
                turn_id=turn_id,
                entrada=entrada_visivel or texto,
                resposta=resultado.text,
                camada=Camada.LLM,
                rodadas=resultado.rounds,
                falado=falar,
                erro=resultado.erro,
            ),
            turno,
            # Ja falamos em streaming acima; _finalizar nao repete.
            falar=False,
        )

    async def _finalizar(
        self, resultado: TurnOutcome, turno: TurnMetrics, *, falar: bool
    ) -> TurnOutcome:
        if falar and resultado.resposta and not self._silenciado:
            self._synth.resume()
            with turno.stage("tts"):
                resultado.falado = await self._synth.speak(resultado.resposta)

        turno.mark("fim")
        self._tracker.record(turno)
        resultado.latencia = turno.to_dict()
        self._turno = None

        await self._bus.emit(
            "voz.resposta",
            source="voice_loop",
            payload={
                "texto": resultado.resposta,
                "camada": resultado.camada.value,
                "acao": resultado.acao.value,
                "rodadas": resultado.rodadas,
                "erro": resultado.erro,
            },
            correlation_id=resultado.turn_id,
        )
        await self._bus.emit(
            "voz.latencia",
            source="voice_loop",
            payload=resultado.latencia,
            correlation_id=resultado.turn_id,
            persist=False,
        )
        log.info(
            "voz.turno",
            turn_id=resultado.turn_id,
            camada=resultado.camada.value,
            total_ms=resultado.latencia.get("total_ms"),
            primeira_silaba_ms=turno.marcos.get("primeira_silaba"),
        )
        return resultado

    # --------------------------------------------------------------- voz
    async def run_forever(self, frames_factory: Any) -> None:
        """Escuta continuamente: wake -> captura -> transcricao -> turno."""
        if self._wake is None or self._transcriber is None:
            raise RuntimeError("loop de voz exige wake word e transcritor")

        log.info("voz.escutando", wake=self._wake.name)
        while True:
            frames = frames_factory()
            if not await self._wake.wait_for_wake(frames):
                log.warning("voz.fluxo_de_audio_encerrado")
                return
            try:
                await self._turno_de_voz(frames_factory())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("voz.turno_falhou", erro=f"{type(exc).__name__}: {exc}", exc_info=True)
                await self._bus.emit(
                    "voz.erro", source="voice_loop", payload={"erro": str(exc)}
                )

    async def _turno_de_voz(self, frames: AsyncIterator[bytes]) -> None:
        turn_id = uuid.uuid4().hex[:12]
        turno = TurnMetrics(turn_id)
        self._turno = turno
        self._silenciado = False

        await self._bus.emit("voz.wake", source="wake", payload={}, correlation_id=turn_id)
        await self.stop_speaking()  # barge-in: falar por cima do Optmus interrompe

        with turno.stage("captura"):
            fala = await self._capture.capture(frames)
        if not fala.utilizavel:
            log.debug("voz.fala_descartada", motivo=fala.motivo.value)
            self._turno = None
            return

        assert self._transcriber is not None
        with turno.stage("stt"):
            transcricao = await self._transcriber.transcribe(fala.pcm)

        await self._bus.emit(
            "voz.transcricao",
            source="stt",
            payload={
                "texto": transcricao.text,
                "duracao_ms": transcricao.duracao_ms,
                "audio_ms": transcricao.audio_ms,
                "fator_tempo_real": transcricao.fator_tempo_real,
            },
            correlation_id=turn_id,
        )
        if transcricao.vazia:
            self._turno = None
            return

        # A voz nao tem dispositivo que assine - o microfone nao produz HMAC.
        # Carimbar ORIGEM_VOZ e o que permite a tela autorizar depois: a
        # politica trata essa origem como abertura declarada, nao como falha.
        with origem(ORIGEM_VOZ):
            await self.handle_text(transcricao.text, source="voz", turn_id=turn_id)

    # -------------------------------------------------------- kill switch
    async def stop_speaking(self) -> None:
        """Aborta a fala em andamento. Base do "Optmus, parar tudo"."""
        await self._synth.stop()
        if self._falando is not None and not self._falando.done():
            self._falando.cancel()
        self._synth.resume()

    def unmute(self) -> None:
        self._silenciado = False

    @property
    def silenciado(self) -> bool:
        return self._silenciado

    def stats(self) -> dict[str, Any]:
        return {
            "turnos": self.turnos,
            "silenciado": self._silenciado,
            "historico": len(self._memory.working) if self._memory is not None else 0,
            "roteador": self._router.stats(),
            "tts": self._synth.motor_ativo,
        }
