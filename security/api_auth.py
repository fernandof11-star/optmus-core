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

import os
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


# Variaveis que so existem quando alguem hospeda o processo. Railway, Render,
# Heroku e Fly injetam PORT; a Railway ainda marca o ambiente com RAILWAY_*.
MARCAS_DE_PLATAFORMA: Final[tuple[str, ...]] = ("PORT", "RAILWAY_ENVIRONMENT", "RENDER", "DYNO")


def hospedado() -> bool:
    """True quando ha sinal de que o processo roda numa plataforma."""
    return any(marca in os.environ for marca in MARCAS_DE_PLATAFORMA)


def exposto_na_rede(settings: Settings) -> bool:
    """True quando o processo aceita conexao de fora da maquina.

    ``http_host`` sozinho NAO responde essa pergunta, e foi assim que o Core
    ficou publico sem autenticacao. O endereco em que ele realmente escuta vem
    da linha de comando do uvicorn (``--host 0.0.0.0``), nao desta configuracao
    - que e apenas a *crenca* da aplicacao sobre onde ela esta. Com
    ``OPTMUS_HTTP_HOST`` ausente, o padrao 127.0.0.1 fazia o Core se declarar
    local enquanto atendia a internet inteira.

    Por isso o sinal da plataforma entra aqui: PORT injetado significa que
    alguem esta hospedando este processo, independentemente do que a config diz.
    """
    return settings.http_host not in HOSTS_LOCAIS or hospedado()


def verificar_exposicao(settings: Settings) -> None:
    """Falha na inicializacao se o Core subir exposto e sem token.

    Chamado no lifespan. Preferimos nao subir a subir aberto: um Core exposto
    e uma porta para o Notion pessoal, para o WhatsApp (F6) e para os celulares
    (F5), tudo sem autenticacao.

    A ordem das checagens e deliberada: **primeiro o token**. A pergunta que
    importa nao e "estou exposto?", que depende de adivinhacao, e sim "tenho
    como me defender?". Sem token, so nao levanta quem provar ser local.
    """
    if settings.api_token is not None:
        return
    if not exposto_na_rede(settings) and settings.env.value != "prod":
        return
    raise ConfigError(
        f"Core exposto (host={settings.http_host}, env={settings.env.value}, "
        f"plataforma={hospedado()}) sem OPTMUS_API_TOKEN. Gere um com "
        '`python -c "import secrets; print(secrets.token_urlsafe(48))"` '
        "e configure antes de expor a API."
    )


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Bearer token fixo. Simples de proposito: um usuario, um segredo."""

    def __init__(self, app: object, settings: Settings) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._settings = settings
        self._ativo = settings.api_token is not None
        if self._ativo:
            log.info("api.autenticacao_ativa", rotas_publicas=sorted(ROTAS_PUBLICAS))
            return
        # Nivel error, nao warning: sem token esta API entrega memoria pessoal,
        # execucao de ferramenta e kill switch para quem chegar. Em maquina
        # local isso e aceitavel; em qualquer outro lugar o verificar_exposicao
        # ja deveria ter impedido a subida, e este log e a ultima chance de
        # alguem perceber que nao impediu.
        log.error(
            "api.SEM_AUTENTICACAO",
            host=settings.http_host,
            env=settings.env.value,
            hospedado=hospedado(),
            impacto="qualquer um que alcance esta porta tem acesso total",
            acao="configure OPTMUS_API_TOKEN",
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
