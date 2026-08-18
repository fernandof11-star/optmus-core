"""Logging estruturado (structlog).

Sem ``print`` em lugar nenhum do projeto. Todo evento de log carrega
contexto em campos, para que a F5 seja depuravel as 2h da manha.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_configured = False


def configure_logging(*, level: str = "info", json_output: bool = False) -> None:
    """Configura structlog + stdlib. Idempotente."""
    global _configured
    if _configured:
        return

    nivel = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=nivel,
        force=True,
    )
    # uvicorn duplica mensagens se manter os handlers proprios.
    for nome in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(nome).handlers.clear()
        logging.getLogger(nome).propagate = True

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(nivel),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def reset_logging() -> None:
    """Permite reconfigurar (testes)."""
    global _configured
    _configured = False
    structlog.reset_defaults()


def get_logger(name: str, **initial: Any) -> structlog.stdlib.BoundLogger:
    """Logger nomeado, opcionalmente com contexto fixo."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger.bind(**initial) if initial else logger
