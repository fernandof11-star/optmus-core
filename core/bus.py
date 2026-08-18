"""Barramento de eventos.

Tudo e evento: percepcao (voz, gesto, timer, webhook) e acao viram evento,
e o log de eventos e a fonte de verdade.

A interface publica e :class:`EventBus`. A v1 usa :class:`InProcessEventBus`
(asyncio, um processo so). Quando os workers virarem processos separados
(provavel na F5), entra um ``RedisEventBus`` implementando a mesma ABC -
troca de implementacao, nenhum chamador muda.

Garantias da implementacao em processo:
- entrega assincrona por fila propria de cada assinante: consumidor lento
  nao segura o publicador (a voz nao pode esperar o HUD);
- fila cheia descarta o evento MAIS ANTIGO e loga - perder um frame de
  amplitude de audio e aceitavel, travar o pipeline nao;
- excecao em handler nao mata o assinante nem o barramento;
- persistencia no SQLite antes do fanout, para o evento existir mesmo que
  o processo caia no meio da entrega.
"""

from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from core.logging import get_logger
from memory.store import Store

log = get_logger("core.bus")

Handler = Callable[["Event"], Awaitable[None] | None]

WILDCARD: Final[str] = "*"
_STOP = object()


class Event(BaseModel):
    """Unidade de troca do sistema.

    ``type`` usa namespace com ponto: ``voz.wake``, ``voz.transcricao``,
    ``ferramenta.executada``, ``dispositivo.offline``, ``sistema.health``.
    Assinantes usam glob: ``voz.*``, ``dispositivo.*``, ``*``.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    type: str
    source: str
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def iso_created_at(self) -> str:
        return self.created_at.isoformat(timespec="milliseconds")


@dataclass(slots=True)
class BusStats:
    publicados: int = 0
    persistidos: int = 0
    entregues: int = 0
    descartados: int = 0
    erros_handler: int = 0
    assinantes: int = 0


@dataclass(slots=True)
class Subscription:
    """Assinatura viva. Feche com :meth:`unsubscribe`."""

    pattern: str
    name: str
    handler: Handler
    queue: asyncio.Queue[Any]
    _bus: InProcessEventBus
    task: asyncio.Task[None] | None = None
    entregues: int = 0
    descartados: int = 0
    erros: int = 0

    def matches(self, event_type: str) -> bool:
        return self.pattern == WILDCARD or fnmatchcase(event_type, self.pattern)

    async def unsubscribe(self) -> None:
        await self._bus._remove(self)


class EventBus(ABC):
    """Contrato do barramento. Nao dependa de detalhes da implementacao."""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def publish(self, event: Event, *, persist: bool = True) -> Event: ...

    @abstractmethod
    async def subscribe(
        self, pattern: str, handler: Handler, *, name: str | None = None
    ) -> Subscription: ...

    async def emit(
        self,
        type_: str,
        source: str,
        payload: dict[str, Any] | None = None,
        *,
        correlation_id: str | None = None,
        persist: bool = True,
    ) -> Event:
        """Atalho: monta o :class:`Event` e publica."""
        return await self.publish(
            Event(
                type=type_,
                source=source,
                payload=payload or {},
                correlation_id=correlation_id,
            ),
            persist=persist,
        )


class InProcessEventBus(EventBus):
    """Pub/sub asyncio com persistencia no SQLite."""

    def __init__(self, store: Store | None = None, *, queue_maxsize: int = 1000) -> None:
        self._store = store
        self._queue_maxsize = queue_maxsize
        self._subs: list[Subscription] = []
        self._lock = asyncio.Lock()
        self._running = False
        self._stats = BusStats()

    # -------------------------------------------------------------- ciclo
    async def start(self) -> None:
        self._running = True
        log.info(
            "bus.iniciado",
            queue_maxsize=self._queue_maxsize,
            persistencia=self._store is not None,
        )

    async def stop(self) -> None:
        """Para o barramento drenando as filas antes de cancelar."""
        self._running = False
        async with self._lock:
            subs, self._subs = self._subs, []
        for sub in subs:
            await sub.queue.put(_STOP)
        for sub in subs:
            if sub.task is not None:
                try:
                    await asyncio.wait_for(sub.task, timeout=2.0)
                except (TimeoutError, asyncio.CancelledError):
                    sub.task.cancel()
        log.info("bus.parado", **self.stats_dict())

    # --------------------------------------------------------- assinatura
    async def subscribe(
        self, pattern: str, handler: Handler, *, name: str | None = None
    ) -> Subscription:
        sub = Subscription(
            pattern=pattern,
            name=name or getattr(handler, "__name__", "anonimo"),
            handler=handler,
            queue=asyncio.Queue(maxsize=self._queue_maxsize),
            _bus=self,
        )
        sub.task = asyncio.create_task(self._worker(sub), name=f"bus-sub-{sub.name}")
        async with self._lock:
            self._subs.append(sub)
            self._stats.assinantes = len(self._subs)
        log.debug("bus.assinante_registrado", assinante=sub.name, pattern=pattern)
        return sub

    async def _remove(self, sub: Subscription) -> None:
        async with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)
                self._stats.assinantes = len(self._subs)
        await sub.queue.put(_STOP)
        if sub.task is not None:
            try:
                await asyncio.wait_for(sub.task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                sub.task.cancel()
        log.debug("bus.assinante_removido", assinante=sub.name, entregues=sub.entregues)

    # -------------------------------------------------------- publicacao
    async def publish(self, event: Event, *, persist: bool = True) -> Event:
        """Persiste (opcional) e entrega a todo assinante compativel.

        ``persist=False`` existe para eventos de altissima frequencia -
        amplitude de audio a 30Hz nao merece uma linha no banco.
        """
        if not self._running:
            log.warning("bus.publicacao_com_barramento_parado", type=event.type)

        if persist and self._store is not None:
            try:
                await self._store.insert_event(
                    event_id=event.id,
                    type_=event.type,
                    source=event.source,
                    payload=event.payload,
                    correlation_id=event.correlation_id,
                    created_at=event.iso_created_at(),
                )
                self._stats.persistidos += 1
            except Exception as exc:  # noqa: BLE001 - persistencia nao pode derrubar o fluxo
                log.error("bus.falha_ao_persistir", type=event.type, erro=str(exc))

        self._stats.publicados += 1
        async with self._lock:
            alvos = [s for s in self._subs if s.matches(event.type)]

        for sub in alvos:
            self._enfileirar(sub, event)
        return event

    def _enfileirar(self, sub: Subscription, event: Event) -> None:
        try:
            sub.queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                descartado = sub.queue.get_nowait()
                sub.queue.task_done()
            except asyncio.QueueEmpty:
                descartado = None
            sub.descartados += 1
            self._stats.descartados += 1
            log.warning(
                "bus.evento_descartado",
                assinante=sub.name,
                motivo="fila cheia",
                descartado=getattr(descartado, "type", None),
                total_descartados=sub.descartados,
            )
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - so com concorrencia extrema
                log.error("bus.evento_perdido", assinante=sub.name, type=event.type)

    # ------------------------------------------------------------ worker
    async def _worker(self, sub: Subscription) -> None:
        while True:
            item = await sub.queue.get()
            try:
                if item is _STOP:
                    return
                event: Event = item
                try:
                    resultado = sub.handler(event)
                    if asyncio.iscoroutine(resultado):
                        await resultado
                    sub.entregues += 1
                    self._stats.entregues += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # handler ruim nao mata o barramento
                    sub.erros += 1
                    self._stats.erros_handler += 1
                    log.error(
                        "bus.erro_no_handler",
                        assinante=sub.name,
                        type=event.type,
                        erro=f"{type(exc).__name__}: {exc}",
                        exc_info=True,
                    )
            finally:
                sub.queue.task_done()

    # ------------------------------------------------------------- estado
    async def drain(self, timeout: float = 5.0) -> bool:
        """Espera as filas esvaziarem. Devolve ``False`` se estourou o tempo."""
        async with self._lock:
            filas = [s.queue for s in self._subs]
        try:
            await asyncio.wait_for(
                asyncio.gather(*(q.join() for q in filas)),
                timeout=timeout,
            )
            return True
        except TimeoutError:
            log.warning("bus.drain_timeout", timeout=timeout)
            return False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def subscribers(self) -> list[str]:
        return [f"{s.name}:{s.pattern}" for s in self._subs]

    def stats_dict(self) -> dict[str, int]:
        return {
            "publicados": self._stats.publicados,
            "persistidos": self._stats.persistidos,
            "entregues": self._stats.entregues,
            "descartados": self._stats.descartados,
            "erros_handler": self._stats.erros_handler,
            "assinantes": len(self._subs),
        }


@dataclass(slots=True)
class Recorder:
    """Assinante de diagnostico: guarda os ultimos N eventos em memoria."""

    limite: int = 50
    eventos: list[Event] = field(default_factory=list)

    def __call__(self, event: Event) -> None:
        self.eventos.append(event)
        if len(self.eventos) > self.limite:
            del self.eventos[: -self.limite]
