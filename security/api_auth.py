"""Autenticacao da API do Core.

Enquanto o Core escuta em ``127.0.0.1``, nao ter auth e uma escolha razoavel:
quem esta na maquina ja tem tudo. No momento em que ele sobe para a internet,
a mesma API expoe memoria pessoal, execucao de ferramenta, kill switch e o
cerebro pago - **sem senha nenhuma**.

Por isso este modulo faz duas coisas, e a segunda importa mais que a primeira:

1. Exige ``Authorization: Bearer <OPTMUS_API_TOKEN>`` em toda rota.
2. **Recusa subir** um processo exposto sem token configurado. Uma protecao que
   depende de alguem lembrar de ligar nao e protecao - e um lembrete.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from typing import Final

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from core.config import ConfigError, Settings
from core.logging import get_logger

log = get_logger("security.api_auth")

# Unica rota publica: liveness do orquestrador (Railway, Docker, k8s) precisa
# saber se o processo esta de pe sem carregar credencial no healthcheck.
ROTAS_PUBLICAS: Final[frozenset[str]] = frozenset({"/health/live"})

# Enderecos que so a propria maquina alcanca.
HOSTS_LOCAIS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})


def exposto_na_rede(settings: Settings) -> bool:
    """True quando o processo aceita conexao de fora da maquina."""
    return settings.http_host not in HOSTS_LOCAIS


def verificar_exposicao(settings: Settings) -> None:
    """Falha na inicializacao se o Core subir exposto e sem token.

    Chamado no lifespan. Preferimos nao subir a subir aberto: um Core exposto
    e uma porta para o Notion pessoal, para o WhatsApp (F6) e para os celulares
    (F5), tudo sem autenticacao.
    """
    if not exposto_na_rede(settings) and not settings.env.value == "prod":
        return
    if settings.api_token is not None:
        return
    raise ConfigError(
        f"Core exposto (host={settings.http_host}, env={settings.env.value}) sem "
        "OPTMUS_API_TOKEN. Gere um com "
        '`python -c "import secrets; print(secrets.token_urlsafe(48))"` '
        "e configure antes de expor a API."
    )


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Bearer token fixo. Simples de proposito: um usuario, um segredo."""

    def __init__(self, app: object, settings: Settings) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._settings = settings
        self._ativo = settings.api_token is not None
        if not self._ativo:
            log.warning(
                "api.sem_autenticacao",
                host=settings.http_host,
                impacto="qualquer processo local alcanca memoria e ferramentas",
            )

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not self._ativo or request.url.path in ROTAS_PUBLICAS:
            return await call_next(request)

        enviado = _token_do_cabecalho(request)
        assert self._settings.api_token is not None
        esperado = self._settings.api_token.get_secret_value()

        if enviado is None or not secrets.compare_digest(enviado, esperado):
            log.warning(
                "api.acesso_negado",
                caminho=request.url.path,
                origem=request.client.host if request.client else "?",
                motivo="token ausente" if enviado is None else "token invalido",
            )
            return JSONResponse(
                {"detail": "nao autenticado"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


def _token_do_cabecalho(request: Request) -> str | None:
    cabecalho = request.headers.get("authorization", "")
    if cabecalho.lower().startswith("bearer "):
        return cabecalho[7:].strip()
    return None
