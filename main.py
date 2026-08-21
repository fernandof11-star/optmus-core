"""Ponto de entrada do Optmus Core (F0).

Sobe o processo local: configuracao, persistencia, barramento de eventos e
uma API HTTP minima para healthcheck e injecao de eventos de teste.

    uvicorn main:app --reload
    optmus                      # apos `pip install -e .`
"""

from __future__ import annotations

import asyncio
import platform
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from core import __version__
from core.agent import Agent
from core.bus import Event, EventBus, InProcessEventBus, Recorder
from core.config import ConfigError, MissingConfigError, Settings, get_settings, runtime_notes
from core.llm import LLMClient, NullLLMClient, escolher_cliente
from core.logging import configure_logging, get_logger
from core.metrics import LatencyTracker
from core.router import IntentRouter
from core.voice_loop import TurnOutcome, VoiceLoop
from expression.tts import criar_sintetizador
from integrations import notion_map
from integrations.notion import NotionClient, NotionError
from integrations.notion_stats import MapaIncompleto, NotionStats, diagnosticar_alertas
from integrations.reconciliacao import conferir
from memory.consolidator import Consolidator, ConsolidatorScheduler
from memory.store import Store
from memory.system import MemorySystem
from perception.audio import AudioIndisponivel, MicrophoneStream
from perception.stt import STTIndisponivel, Transcriber
from perception.wake import criar_detector
from reports.mensal import ReportlabAusente, gerar_pdf, montar_dados
from security.api_auth import TokenAuthMiddleware, verificar_exposicao
from security.audit import AuditLog
from security.policy import PolicyEngine
from tools.impl.memory_tools import LembrarTool, PerfilAtualizarTool, RecordarTool
from tools.impl.optmus_web import OptmusWebChatTool, OptmusWebClient, OptmusWebTool
from tools.impl.system import SistemaStatusTool
from tools.impl.visao import OlharTool
from tools.registry import ToolRegistry

log = get_logger("main")

INICIO = datetime.now(UTC)

# Variaveis exigidas por fase. O /health mostra o que ainda falta;
# cada subsistema chama settings.require(...) quando for subir de verdade.
REQUISITOS_POR_FASE: dict[str, tuple[str, ...]] = {
    "F1_voz": ("anthropic_api_key", "elevenlabs_api_key", "elevenlabs_voice_id"),
    "F3_optmus_web": ("web_base_url", "web_password"),
    "F6_whatsapp": ("whatsapp_token", "whatsapp_phone_number_id"),
    "F6_instagram": ("instagram_token", "instagram_account_id"),
    "F6_casa": ("homeassistant_base_url", "homeassistant_token"),
    "seguranca_destrutivo": ("destructive_passphrase",),
}


# --------------------------------------------------------------------- modelos
class PublicarEventoRequest(BaseModel):
    type: str = Field(min_length=1, max_length=120, examples=["sistema.teste"])
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = Field(default="api", max_length=60)
    correlation_id: str | None = None
    persist: bool = True


class FalarRequest(BaseModel):
    texto: str = Field(min_length=1, max_length=2000, examples=["que horas sao"])
    source: str = Field(default="api", max_length=60)


class ChatRequest(BaseModel):
    mensagem: str = Field(min_length=1, max_length=2000, examples=["oi"])
    source: str = Field(default="web", max_length=60)


class ChatResponse(BaseModel):
    output: str
    turn_id: str
    tools_usadas: list[str] = Field(default_factory=list)
    timestamp: str
    erro: str | None = None


class FatoRequest(BaseModel):
    conteudo: str = Field(min_length=3, max_length=1000)
    source: str = Field(default="api", max_length=60)
    confianca: float = Field(default=0.8, ge=0.0, le=1.0)
    supersedes: int | None = Field(default=None, description="id do fato que este corrige")


class ConfirmarRequest(BaseModel):
    token: str = Field(min_length=4, max_length=64)
    frase_codigo: str | None = Field(default=None, description="exigida em acao DESTRUTIVA")


class TurnoResponse(BaseModel):
    turn_id: str
    entrada: str
    resposta: str
    camada: str
    acao: str
    regra: str | None
    rodadas: int
    falado: bool
    erro: str | None
    latencia: dict[str, Any]

    @classmethod
    def de(cls, turno: TurnOutcome) -> TurnoResponse:
        return cls(
            turn_id=turno.turn_id,
            entrada=turno.entrada,
            resposta=turno.resposta,
            camada=turno.camada.value,
            acao=turno.acao.value,
            regra=turno.regra,
            rodadas=turno.rodadas,
            falado=turno.falado,
            erro=turno.erro,
            latencia=turno.latencia,
        )


class EventoResponse(BaseModel):
    id: str
    type: str
    source: str
    payload: dict[str, Any]
    correlation_id: str | None
    created_at: str

    @classmethod
    def de(cls, event: Event) -> EventoResponse:
        return cls(
            id=event.id,
            type=event.type,
            source=event.source,
            payload=event.payload,
            correlation_id=event.correlation_id,
            created_at=event.iso_created_at(),
        )


# --------------------------------------------------------------------- ciclo
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        settings = get_settings()
    except Exception as exc:
        # Sem logging configurado ainda: configura o minimo para o erro sair legivel.
        configure_logging(level="error", json_output=False)
        get_logger("main").error("startup.configuracao_invalida", erro=str(exc))
        raise ConfigError(
            "Configuracao invalida. Copie .env.example para .env e preencha "
            "OPTMUS_SECRET_KEY."
        ) from exc

    configure_logging(level=settings.log_level.value, json_output=settings.use_json_logs)
    for nota in runtime_notes():
        log.warning("ambiente.aviso", nota=nota)

    # Antes de abrir qualquer porta: Core exposto sem token nao sobe.
    verificar_exposicao(settings)

    store = await Store(
        settings.database_path, embedding_dim=settings.embedding_dim
    ).connect()
    await store.migrate()
    await store.purge_expired_events(settings.event_retention_days)

    bus: EventBus = InProcessEventBus(store, queue_maxsize=settings.bus_queue_maxsize)
    await bus.start()

    recorder = Recorder(limite=50)
    await bus.subscribe("*", recorder, name="recorder")
    await bus.subscribe("*", _logar_evento, name="tracer")

    cliente = await _montar_cerebro(settings)
    tracker = LatencyTracker(window=settings.metrics_window, target_ms=settings.latency_target_ms)

    memoria = MemorySystem(settings, store)
    await memoria.start()

    degradacoes_ref: list[str] = []
    ferramentas = await _montar_ferramentas(
        settings,
        store=store,
        memoria=memoria,
        tracker=tracker,
        degradacoes=lambda: degradacoes_ref,
    )

    voz = VoiceLoop(
        settings,
        bus=bus,
        router=IntentRouter(settings),
        agent=Agent(
            cliente,
            settings,
            tools=ferramentas,
            on_event=lambda tipo, payload: bus.emit(tipo, source="agente", payload=payload),
        ),
        synthesizer=criar_sintetizador(settings),
        tracker=tracker,
        memory=memoria,
    )

    agendador = ConsolidatorScheduler(
        Consolidator(
            settings,
            episodic=memoria.episodic,
            semantic=memoria.semantic,
            procedural=memoria.procedural,
            llm=cliente,
            bus=bus,
        ),
        hora=settings.consolidator_hour,
        bus=bus,
    )

    app.state.settings = settings
    app.state.store = store
    app.state.bus = bus
    app.state.recorder = recorder
    app.state.llm = cliente
    app.state.tracker = tracker
    app.state.memoria = memoria
    app.state.ferramentas = ferramentas
    app.state.degradacoes = degradacoes_ref
    app.state.agendador = agendador
    app.state.voz = voz
    app.state.escuta = None
    app.state.sono = None

    if settings.consolidator_enabled:
        app.state.sono = asyncio.create_task(agendador.run_forever(), name="consolidador")

    if settings.voice_enabled:
        app.state.escuta = await _ligar_microfone(settings, voz)

    await bus.emit(
        "sistema.iniciado",
        source="core",
        payload={"versao": __version__, "env": settings.env.value, "cerebro": cliente.name},
    )
    log.info(
        "core.pronto",
        versao=__version__,
        env=settings.env.value,
        db=str(settings.database_path),
        busca_vetorial=store.vector_search_available,
        cerebro=cliente.name,
        escuta=app.state.escuta is not None,
        embedder=memoria.embedder.name,
    )

    try:
        yield
    finally:
        sono = app.state.sono
        if sono is not None:
            sono.cancel()
            await asyncio.gather(sono, return_exceptions=True)
        escuta = app.state.escuta
        if escuta is not None:
            escuta["task"].cancel()
            await asyncio.gather(escuta["task"], return_exceptions=True)
            await escuta["mic"].stop()
        await voz.stop_speaking()
        await bus.emit("sistema.encerrando", source="core", payload={})
        await bus.stop()
        await store.close()
        log.info("core.encerrado")


async def _montar_ferramentas(
    settings: Settings,
    *,
    store: Store,
    memoria: MemorySystem,
    tracker: LatencyTracker,
    degradacoes: Any,
) -> ToolRegistry:
    """Monta o registro com politica, auditoria e sandbox ligados."""
    registro = ToolRegistry(
        policy=PolicyEngine(settings, store),
        audit=AuditLog(store),
        store=store,
        sandbox_runs=settings.tool_sandbox_runs,
    )
    # Um cliente HTTP so: as duas ferramentas compartilham token e circuit
    # breaker. Dois clientes teriam dois circuitos e o Web cairia duas vezes.
    web = OptmusWebClient(settings)
    registro.register(OptmusWebTool(settings, web))
    registro.register(OptmusWebChatTool(settings, web))
    registro.register(LembrarTool(memoria))
    registro.register(RecordarTool(memoria))
    registro.register(PerfilAtualizarTool(memoria))
    registro.register(
        SistemaStatusTool(settings, memory=memoria, tracker=tracker, degradacoes=degradacoes)
    )
    # Sem caminho especial para visao: entra pelo mesmo registro, com a mesma
    # politica de EXTERNO. O refresh() abaixo a remove do schema quando nao ha
    # OpenCV - o que e sempre o caso no container, que nao tem camera.
    registro.register(OlharTool(settings))
    await registro.refresh()
    log.info("ferramentas.montadas", quantidade=len(registro.schemas()))
    return registro


async def _montar_cerebro(settings: Settings) -> LLMClient:
    """Anthropic, Ollama local ou nenhum - sem derrubar o processo."""
    try:
        return await escolher_cliente(settings)
    except MissingConfigError as exc:
        log.warning(
            "cerebro.indisponivel",
            erro=str(exc),
            impacto="camada 1 do roteador continua funcionando; o resto nao responde",
        )
        return NullLLMClient()


async def _ligar_microfone(settings: Settings, voz: VoiceLoop) -> dict[str, Any] | None:
    """Sobe a escuta continua. Falha aqui degrada, nao derruba (secao 3.5)."""
    try:
        mic = MicrophoneStream(settings)
        await mic.start()
    except AudioIndisponivel as exc:
        log.warning("voz.microfone_indisponivel", erro=str(exc), impacto="so o caminho de texto")
        return None

    transcritor = Transcriber(settings)
    try:
        await transcritor.load()
    except STTIndisponivel as exc:
        log.warning("voz.stt_indisponivel", erro=str(exc), impacto="so o caminho de texto")
        await mic.stop()
        return None

    voz.attach_voice_io(transcritor, criar_detector(settings))
    task = asyncio.create_task(voz.run_forever(mic.frames), name="voz-escuta")
    return {"task": task, "mic": mic, "transcritor": transcritor}


def _logar_evento(event: Event) -> None:
    log.debug("evento", type=event.type, source=event.source, id=event.id)


def _settings_para_app() -> Settings:
    """Configuracao lida no import, para middleware e docs.

    O lifespan revalida e falha com mensagem melhor; aqui uma configuracao
    invalida vira um Settings default so para o modulo importar - o processo
    ainda nao vai subir sem corrigir o .env.
    """
    try:
        return get_settings()
    except Exception:  # noqa: BLE001 - o erro util e levantado no lifespan
        return Settings.model_construct(api_token=None)


_settings_inicial = _settings_para_app()

app = FastAPI(
    title="Optmus Core",
    version=__version__,
    summary="Assistente ambiente: voz, memoria, dispositivos, HUD.",
    lifespan=lifespan,
    # Sem docs interativas em producao: elas enumeram toda a superficie da API,
    # inclusive as rotas de ferramenta e de memoria.
    docs_url=None if _settings_inicial.env.value == "prod" else "/docs",
    redoc_url=None,
    openapi_url=None if _settings_inicial.env.value == "prod" else "/openapi.json",
)
app.add_middleware(TokenAuthMiddleware, settings=_settings_inicial)
# CORS adicionado DEPOIS do auth, e por isso fica por FORA dele - o Starlette
# aplica os middlewares na ordem inversa. A ordem e o ponto: o navegador manda
# um OPTIONS de preflight ANTES do POST /chat, e preflight nao carrega
# credencial por definicao. Com o auth por fora, o preflight levava 401 sem
# nenhum header Access-Control-*, e o navegador bloqueava a chamada real - com
# a API funcionando perfeitamente no curl, que nao faz CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings_inicial.cors_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    # False de proposito: o token vai no header Authorization, nao em cookie.
    # Ligar isto sem necessidade permitiria que o navegador anexasse cookies de
    # sessao em requisicao de outra origem.
    allow_credentials=False,
    max_age=600,
)


@app.get("/health/live", tags=["sistema"])
async def liveness() -> dict[str, str]:
    """Liveness do orquestrador. Unica rota sem autenticacao.

    Deliberadamente burra: responde se o processo esta de pe, e nada mais.
    Estado real e diagnostico ficam em /health, que exige token.
    """
    return {"status": "vivo", "versao": __version__}


# ------------------------------------------------------------------ dependencias
def get_bus(request: Request) -> EventBus:
    bus: EventBus | None = getattr(request.app.state, "bus", None)
    if bus is None:  # pragma: no cover - so acontece fora do lifespan
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "barramento indisponivel")
    return bus


def get_store(request: Request) -> Store:
    store: Store | None = getattr(request.app.state, "store", None)
    if store is None:  # pragma: no cover
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "persistencia indisponivel")
    return store


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_voz(request: Request) -> VoiceLoop:
    voz: VoiceLoop | None = getattr(request.app.state, "voz", None)
    if voz is None:  # pragma: no cover
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "loop de voz indisponivel")
    return voz


def get_tracker(request: Request) -> LatencyTracker:
    return request.app.state.tracker  # type: ignore[no-any-return]


def get_memoria(request: Request) -> MemorySystem:
    memoria: MemorySystem | None = getattr(request.app.state, "memoria", None)
    if memoria is None:  # pragma: no cover
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "memoria indisponivel")
    return memoria


def get_agendador(request: Request) -> ConsolidatorScheduler:
    return request.app.state.agendador  # type: ignore[no-any-return]


BusDep = Annotated[EventBus, Depends(get_bus)]
StoreDep = Annotated[Store, Depends(get_store)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
VozDep = Annotated[VoiceLoop, Depends(get_voz)]
TrackerDep = Annotated[LatencyTracker, Depends(get_tracker)]
MemoriaDep = Annotated[MemorySystem, Depends(get_memoria)]
AgendadorDep = Annotated[ConsolidatorScheduler, Depends(get_agendador)]


# ----------------------------------------------------------------------- rotas
@app.get("/health", tags=["sistema"])
async def health(
    store: StoreDep, bus: BusDep, settings: SettingsDep, request: Request
) -> dict[str, Any]:
    """Estado real do processo. O HUD (F4) consome isto no painel superior esquerdo."""
    store_status = store.status()
    voz: VoiceLoop = request.app.state.voz
    cerebro: LLMClient = request.app.state.llm
    memoria: MemorySystem = request.app.state.memoria
    agendador: ConsolidatorScheduler = request.app.state.agendador
    memoria_stats = await memoria.stats()

    degradacoes: list[str] = []
    if not store_status.vector_search_available:
        degradacoes.append("busca vetorial indisponivel (sqlite-vec nao carregou)")
    if isinstance(cerebro, NullLLMClient):
        degradacoes.append("sem cerebro: so a camada 1 do roteador responde")
    if not memoria.embedder.semantico:
        degradacoes.append(
            "memoria em busca lexical: acha palavra, nao significado "
            '(instale o extra [memoria] para embeddings semanticos)'
        )
    if memoria.aviso_dimensao:
        degradacoes.append(memoria.aviso_dimensao)
    if settings.voice_enabled and not voz.escutando:
        degradacoes.append("escuta continua desligada: microfone ou STT indisponivel")

    ferramentas: ToolRegistry = request.app.state.ferramentas
    indisponiveis = [t["nome"] for t in ferramentas.listar() if not t["disponivel"]]
    if indisponiveis:
        degradacoes.append(f"ferramentas sem configuracao: {', '.join(indisponiveis)}")
    degradacoes.extend(runtime_notes())

    # sistema_status le esta mesma lista: o Optmus e o /health nao podem
    # discordar sobre o que esta quebrado.
    request.app.state.degradacoes[:] = degradacoes

    pendencias = {
        fase: [f"OPTMUS_{n.upper()}" for n in settings.missing(*campos)]
        for fase, campos in REQUISITOS_POR_FASE.items()
    }

    return {
        "status": "degradado" if degradacoes else "ok",
        "versao": __version__,
        "env": settings.env.value,
        "uptime_s": round((datetime.now(UTC) - INICIO).total_seconds(), 3),
        "runtime": {
            "python": platform.python_version(),
            "plataforma": platform.system(),
        },
        "persistencia": {
            "path": store_status.path,
            "busca_vetorial": store_status.vector_search_available,
            "dimensao_embedding": store_status.embedding_dim,
            "migrations_aplicadas": store_status.applied_migrations,
            "migrations_pendentes": store_status.pending_migrations,
            "eventos": await store.count_events(),
            "vec_erro": store_status.vec_error,
        },
        "barramento": {
            "ativo": getattr(bus, "running", False),
            **(bus.stats_dict() if isinstance(bus, InProcessEventBus) else {}),
            # depois do spread: a lista nomeada vence o contador homonimo
            "assinantes": getattr(bus, "subscribers", []),
        },
        "voz": {
            "cerebro": cerebro.name,
            "modelo": settings.anthropic_model if cerebro.name == "anthropic" else None,
            "escuta_ligada": settings.voice_enabled,
            "escutando": voz.escutando,
            **voz.stats(),
        },
        "memoria": memoria_stats,
        "consolidador": agendador.stats(),
        "ferramentas": ferramentas.listar(),
        "config_pendente": {k: v for k, v in pendencias.items() if v},
        "degradacoes": degradacoes,
    }


@app.get("/metrics", tags=["sistema"])
async def metrics(tracker: TrackerDep) -> dict[str, Any]:
    """Latencia por etapa. O numero, nao a sensacao, decide otimizacao."""
    return tracker.summary()


@app.post("/voz/texto", tags=["voz"])
async def falar_por_texto(corpo: FalarRequest, voz: VozDep) -> TurnoResponse:
    """Injeta uma fala ja transcrita: mesmo caminho do microfone, sem microfone.

    E como o HUD, os testes e (na F6) o WhatsApp entram no assistente.
    """
    return TurnoResponse.de(await voz.handle_text(corpo.texto, source=corpo.source))


@app.post("/chat", tags=["chat"])
async def chat(corpo: ChatRequest, voz: VozDep) -> ChatResponse:
    """Conversa por texto, para cliente que fala do proprio lado.

    Mesmo pipeline de ``/voz/texto`` - roteador, memoria, ferramentas -, com
    duas diferencas que existem por causa do navegador:

    - **Nao sintetiza voz no servidor.** Sem isto o Core fala em voz alta na
      maquina onde ele roda quando alguem digita no site. Alem do absurdo, o
      turno so terminava depois do audio: medi 3,8 s de TTS numa resposta que
      ficou pronta em 2 ms.
    - **Resposta enxuta.** O ``TurnoResponse`` carrega latencia por etapa e
      camada de roteamento, que e material de diagnostico, nao de interface.
    """
    turno = await voz.handle_text(corpo.mensagem, source=corpo.source, falar=False)
    return ChatResponse(
        output=turno.resposta,
        turn_id=turno.turn_id,
        tools_usadas=[],
        timestamp=datetime.now(UTC).isoformat(),
        erro=turno.erro,
    )


@app.post("/voz/gatilho", status_code=status.HTTP_202_ACCEPTED, tags=["voz"])
async def gatilho_manual(voz: VozDep) -> dict[str, str]:
    """Abre uma captura de microfone, como se a wake word tivesse disparado.

    Assincrono por natureza: a resposta sai por voz e pelos eventos
    ``voz.wake`` / ``voz.transcricao`` / ``voz.resposta``, nunca no corpo desta
    chamada. Para receber a resposta no HTTP, use ``POST /voz/texto``.

    Recusa com 409 quando nao ha escuta montada - um 202 nesse caso seria
    mentira: nenhum turno rodaria e nao haveria log nenhum para investigar.
    """
    if not await voz.trigger_wake():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "escuta nao montada: nao ha microfone nem STT para abrir um turno. "
            'Ligue OPTMUS_VOICE_ENABLED com o extra [voz] instalado, ou use '
            "POST /voz/texto.",
        )
    return {"status": "disparado"}


@app.get("/memoria/buscar", tags=["memoria"])
async def buscar_memoria(
    memoria: MemoriaDep,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    limit: Annotated[int, Query(ge=1, le=50)] = 5,
) -> dict[str, Any]:
    """Busca nas camadas permanentes, com a conta do score aberta.

    Devolver similaridade, recencia e frequencia separadas nao e enfeite: sem
    isso nao da para saber se a memoria trouxe algo irrelevante por ser recente
    ou por ser parecida - e sao problemas diferentes.
    """
    achados = await memoria.recall(q, limit=limit)
    return {
        "consulta": q,
        "embedder": memoria.embedder.name,
        "semantico": memoria.embedder.semantico,
        "resultados": [h.to_dict() for h in achados],
    }


@app.post("/memoria/fato", status_code=status.HTTP_201_CREATED, tags=["memoria"])
async def gravar_fato(corpo: FatoRequest, memoria: MemoriaDep) -> dict[str, Any]:
    """Grava um fato semantico. ``supersedes`` versiona o antigo, nao apaga."""
    fato = await memoria.semantic.remember(
        corpo.conteudo,
        source=corpo.source,
        confidence=corpo.confianca,
        supersedes=corpo.supersedes,
    )
    return {"id": fato.id, "conteudo": fato.conteudo, "superou": fato.superou}


@app.get("/memoria/rotinas", tags=["memoria"])
async def listar_rotinas(memoria: MemoriaDep) -> list[dict[str, Any]]:
    """Rotinas derivadas pelo consolidador (insumo da proatividade na F7)."""
    return await memoria.procedural.rotinas()


@app.post("/memoria/consolidar", tags=["memoria"])
async def consolidar_agora(agendador: AgendadorDep) -> dict[str, Any]:
    """Roda o consolidador sob demanda, sem esperar a madrugada."""
    return (await agendador.executar()).to_dict()


@app.post("/memoria/reindexar", tags=["memoria"])
async def reindexar(memoria: MemoriaDep) -> dict[str, Any]:
    """Recalcula todos os vetores com o provedor atual.

    Obrigatorio depois de trocar de modelo de embedding: vetores de modelos
    diferentes nao sao comparaveis e a busca degrada em silencio.
    """
    return {"vetores": await memoria.reindexar(), "embedder": memoria.embedder.name}


@app.get("/ferramentas", tags=["ferramentas"])
async def listar_ferramentas(request: Request) -> list[dict[str, Any]]:
    """O que o Optmus sabe fazer, com o risco declarado de cada uma."""
    registro: ToolRegistry = request.app.state.ferramentas
    return registro.listar()


@app.get("/ferramentas/optmus-web/diagnostico", tags=["ferramentas"])
async def diagnostico_optmus_web(request: Request) -> dict[str, Any]:
    """Sonda o Optmus Web e mostra o que foi enviado e o que voltou.

    O contrato de fio do Web e PRESUMIDO neste repositorio. Este endpoint existe
    para acertar formato de rota e esquema de autenticacao olhando a resposta
    real, em vez de adivinhar.
    """
    registro: ToolRegistry = request.app.state.ferramentas
    tool = registro.get("optmus_web")
    if tool is None:  # pragma: no cover
        raise HTTPException(status.HTTP_404_NOT_FOUND, "optmus_web nao registrada")
    return await tool.client.diagnostico()  # type: ignore[attr-defined]


@app.get("/seguranca/pendentes", tags=["seguranca"])
async def acoes_pendentes(request: Request) -> list[dict[str, Any]]:
    """Acoes de risco esperando confirmacao humana."""
    registro: ToolRegistry = request.app.state.ferramentas
    return registro.policy.pendentes()


@app.post("/seguranca/confirmar", tags=["seguranca"])
async def confirmar_acao(corpo: ConfirmarRequest, request: Request) -> dict[str, Any]:
    """Libera uma acao que a politica reteve. A confirmacao e humana, nao do LLM."""
    registro: ToolRegistry = request.app.state.ferramentas
    resultado = await registro.executar_confirmado(
        corpo.token, frase=corpo.frase_codigo, comando_origem="api"
    )
    return {"executado": not resultado.is_error, "resultado": resultado.content}


@app.get("/seguranca/auditoria", tags=["seguranca"])
async def auditoria(
    store: StoreDep, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[dict[str, Any]]:
    """Trilha append-only: o que executou, com que parametro, autorizado como."""
    return await AuditLog(store).recentes(limit)


@app.get("/notion/descobrir", tags=["notion"])
async def descobrir_notion(settings: SettingsDep) -> dict[str, Any]:
    """Lista as bases visiveis a integracao e propoe um rascunho do mapa.

    Existe porque o schema do seu Notion nao da para adivinhar. Os nomes de
    propriedade no rascunho sao reais; a associacao base->papel e heuristica
    pelo titulo. Revise antes de confiar - sobretudo qual coluna e o valor.
    """
    cliente = NotionClient(settings)
    if not cliente.configurado:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "OPTMUS_NOTION_TOKEN ausente. Crie uma integracao interna em "
            "notion.so/my-integrations e compartilhe as bases com ela.",
        )
    try:
        bases = await cliente.listar_bases()
    except NotionError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    return {
        "bases": [b.to_dict() for b in bases],
        "rascunho_do_mapa": notion_map.rascunho(bases),
        "onde_salvar": str(settings.notion_map_file),
        "proximo_passo": (
            "revise o rascunho, remova as chaves _propriedades_disponiveis e "
            f"grave em {settings.notion_map_file}"
        ),
    }


# Os nomes sao os mesmos que INDICADORES usa para as rotas do Optmus Web, de
# proposito: e o que permite comparar os dois lados sem tabela de traducao.
METODOS_NOTION: Final[dict[str, str]] = {
    "financeiro_mensal": "monthly",
    "trabalho": "work_tasks",
    "alertas": "progress_alerts",
    "gastos_por_categoria": "category_spending",
    "financeiro_semanal": "finance_weekly",
    "treino_frequencia": "workout_frequency",
    "treino_mensal": "workout_monthly",
    "taxa_de_poupanca": "savings_rate",
    "previsao_financeira": "forecast",
    "estudos": "study",
    "notas_escolares": "grades",
    "sono": "sleep",
    "tarefas": "tasks",
}


@app.get("/notion/stats/{indicador}", tags=["notion"])
async def notion_stats_endpoint(indicador: str, settings: SettingsDep) -> Any:
    """Agregacao calculada direto do Notion, sem passar pelo Optmus Web."""
    metodo = METODOS_NOTION.get(indicador)
    if metodo is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"indicador desconhecido: {indicador} "
            f"(disponiveis: {', '.join(sorted(METODOS_NOTION))})",
        )
    stats = _montar_notion_stats(settings)
    try:
        return await getattr(stats, metodo)()
    except (MapaIncompleto, NotionError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@app.get("/notion/alertas/diagnostico", tags=["notion"])
async def diagnostico_alertas(
    settings: SettingsDep,
    dias: Annotated[int | None, Query(ge=-3650, le=3650)] = None,
) -> dict[str, Any]:
    """Por que cada prazo entra ou nao no relatorio, com as datas exatas a usar.

    Pedir "mova para +30 dias" convida ao erro: um dia de diferenca joga a linha
    para fora e o resultado fica igual ao de um item distante - parece
    confirmacao, e nao e. Aqui vem a data exata que entra e a que fica de fora.

    ``?dias=-30`` devolve a data literal para colar no Notion, ja dizendo se ela
    entraria na janela do Core.
    """
    try:
        return await diagnosticar_alertas(_montar_notion_stats(settings), dias=dias)
    except (MapaIncompleto, NotionError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@app.get("/notion/conferir", tags=["notion"])
async def conferir_notion(settings: SettingsDep, request: Request) -> dict[str, Any]:
    """Compara Optmus Web x calculo local, campo a campo.

    E a peca que decide se o Web pode ser desligado. Uma unica divergencia ja
    reprova: nao existe "quase igual" quando o passo seguinte e apagar a outra
    fonte.
    """
    registro: ToolRegistry = request.app.state.ferramentas
    web = registro.get("optmus_web")
    if web is None or not await web.available():  # pragma: no cover
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Optmus Web nao configurado - nao ha com o que comparar"
        )
    relatorio = await conferir(web_client=web.client, stats=_montar_notion_stats(settings))
    return relatorio.to_dict()


@app.get("/relatorios/mensal", tags=["notion"])
async def relatorio_mensal(settings: SettingsDep) -> Response:
    """Relatorio mensal em PDF - o que hoje sai de /api/reports/monthly.

    Devolve o arquivo direto, com o mesmo padrao de nome do Web, para poder
    substituir o link antigo sem mexer em quem consome.
    """
    stats = _montar_notion_stats(settings)
    try:
        dados = await montar_dados(stats)
        pdf = gerar_pdf(dados)
    except ReportlabAusente as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except (MapaIncompleto, NotionError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="optmus-relatorio-{dados.mes}.pdf"'
        },
    )


@app.get("/relatorios/mensal/dados", tags=["notion"])
async def relatorio_mensal_dados(settings: SettingsDep) -> dict[str, Any]:
    """Os mesmos numeros do PDF, em JSON.

    Existe para conferir o relatorio sem extrair texto de PDF - e para quem
    quiser montar outra apresentacao em cima dos mesmos dados.
    """
    stats = _montar_notion_stats(settings)
    try:
        return (await montar_dados(stats)).to_dict()
    except (MapaIncompleto, NotionError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


def _montar_notion_stats(settings: Settings) -> NotionStats:
    """Monta as agregacoes com o mapa do ambiente ou do disco.

    A variavel tem precedencia sobre o arquivo. Em producao ela e o unico
    caminho: o mapa identifica as bases pessoais do usuario e por isso nao vai
    para a imagem (repositorio publico) nem depende de alguem lembrar de
    escrever um arquivo no volume por um console.
    """
    if settings.notion_map_json is not None:
        mapa = notion_map.de_texto(settings.notion_map_json.get_secret_value())
    else:
        mapa = notion_map.carregar(settings.notion_map_file)
    return NotionStats(settings, mapa)


@app.post("/sistema/parar", tags=["sistema"])
async def parar_tudo(voz: VozDep, bus: BusDep) -> dict[str, str]:
    """Kill switch. Aborta a fala em andamento agora."""
    await voz.stop_speaking()
    voz.unmute()
    await bus.emit("sistema.parar", source="api", payload={})
    return {"status": "parado"}


@app.post("/events", status_code=status.HTTP_202_ACCEPTED, tags=["eventos"])
async def publicar_evento(corpo: PublicarEventoRequest, bus: BusDep) -> EventoResponse:
    """Publica um evento no barramento. Usado para teste e por webhooks externos."""
    event = await bus.emit(
        corpo.type,
        source=corpo.source,
        payload=corpo.payload,
        correlation_id=corpo.correlation_id,
        persist=corpo.persist,
    )
    return EventoResponse.de(event)


@app.get("/events/recent", tags=["eventos"])
async def eventos_recentes(
    store: StoreDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    type: Annotated[str | None, Query(max_length=120)] = None,
) -> list[dict[str, Any]]:
    """Ultimos eventos persistidos, mais novos primeiro."""
    return await store.recent_events(limit=limit, type_=type)


def run() -> None:
    """Entry point do console script ``optmus``."""
    import uvicorn

    settings = get_settings()
    configure_logging(level=settings.log_level.value, json_output=settings.use_json_logs)
    uvicorn.run(
        "main:app",
        host=settings.http_host,
        port=settings.http_port,
        log_config=None,
        reload=settings.is_dev,
    )


if __name__ == "__main__":  # pragma: no cover
    try:
        run()
    except ConfigError as exc:
        configure_logging(level="error", json_output=False)
        get_logger("main").error("fatal", erro=str(exc))
        sys.exit(1)
