"""Identidade de dispositivo, para amarrar quem pede a quem confirma.

## O problema que isto resolve

Ate aqui, ``POST /seguranca/confirmar`` aceitava o token de confirmacao de
qualquer um que tivesse o ``OPTMUS_API_TOKEN``. A tela de confirmacao dizia
"um humano autorizou"; o que o Core sabia de verdade era "alguem com o token da
API autorizou". Sao coisas diferentes, e a diferenca importa quando a acao
autorizada e mandar mensagem para outra pessoa.

## Por que um header nao bastaria

Marcar a pendencia com um ``X-Optmus-Dispositivo`` declarado daria atribuicao
na auditoria e impediria confusao entre abas - mas quem tivesse o token da API
forjaria o header numa linha de ``curl``. Para o vinculo virar garantia, a
identidade do dispositivo precisa ser um segredo que o token da API **nao**
concede.

Por isso cada dispositivo gera um segredo proprio, que **nunca trafega depois
do registro**: a confirmacao leva um HMAC do token da pendencia. Quem tem so o
token da API nao consegue produzir esse HMAC.

## O ponto de confianca, dito com todas as letras

O registro e **confio-no-primeiro-uso**: o primeiro que apresentar um id novo
fica dono dele. Isso e uma janela real - quem tivesse o token da API antes do
seu HUD registrar poderia registrar um dispositivo. O que ele **nao** pode e
tomar um id ja registrado: reapresentar um id com outro segredo e recusado, e e
essa recusa que sustenta o resto.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Final

from core.logging import get_logger
from memory.store import Store

log = get_logger("security.dispositivos")

CHAVE_REGISTRO: Final[str] = "dispositivos"

# Origem de uma pendencia nascida do laco de voz local. Nao e um dispositivo
# registrado: o microfone nao tem como assinar nada.
ORIGEM_VOZ: Final[str] = "voz-local"
# Pedido que chegou sem dispositivo identificado - script, curl, teste.
ORIGEM_DESCONHECIDA: Final[str] = "desconhecida"

# Id e rotulo, nao segredo: entra em log e em chave de dicionario.
ID_VALIDO: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z0-9._-]{4,64}$")
SEGREDO_MINIMO: Final[int] = 32

_origem: ContextVar[str] = ContextVar("optmus_origem", default=ORIGEM_DESCONHECIDA)


def origem_atual() -> str:
    """Quem originou o trabalho que esta rodando agora."""
    return _origem.get()


@contextmanager
def origem(valor: str) -> Iterator[None]:
    """Marca a origem pelo tempo de um pedido.

    Com ``reset`` no ``finally``, e nao um ``set`` solto: o laco de voz e uma
    tarefa longa e viva: um ``set`` sem reset vazaria a origem de um turno para
    o turno seguinte, e a pendencia sairia carimbada com o dispositivo errado.
    """
    marca = _origem.set(valor or ORIGEM_DESCONHECIDA)
    try:
        yield
    finally:
        _origem.reset(marca)


def prova(segredo: str, acao: str, token: str) -> str:
    """HMAC que o dispositivo apresenta no lugar do segredo.

    A acao entra na mensagem junto do token de proposito: sem ela, uma prova
    capturada para recusar serviria para confirmar. Sao decisoes opostas e nao
    podem compartilhar credencial.
    """
    return hmac.new(
        segredo.encode("utf-8"), f"{acao}:{token}".encode(), sha256
    ).hexdigest()


class DispositivoDesconhecido(PermissionError):
    """Id nunca registrado neste Core."""


class ProvaInvalida(PermissionError):
    """A prova nao bate com o segredo registrado."""


class IdJaRegistrado(PermissionError):
    """Tentativa de reapresentar um id com outro segredo."""


class RegistroDeDispositivos:
    """Guarda id -> segredo, e verifica provas."""

    def __init__(self, store: Store) -> None:
        self._store = store
        # Ler-modificar-gravar num unico JSON tem ponto de espera no meio: dois
        # registros simultaneos perderiam um dos dois sem este cadeado.
        self._cadeado = asyncio.Lock()

    async def _carregar(self) -> dict[str, dict[str, Any]]:
        bruto = await self._store.meta_get(CHAVE_REGISTRO)
        if not bruto:
            return {}
        try:
            return dict(json.loads(bruto))
        except (ValueError, TypeError):
            # Registro ilegivel apaga a confirmacao de todo mundo. Prefiro o
            # erro alto a reescrever por cima e perder os segredos em silencio.
            log.error("dispositivos.registro_corrompido", tamanho=len(bruto))
            raise

    async def registrar(self, dispositivo: str, segredo: str) -> dict[str, Any]:
        """Confio-no-primeiro-uso, com uma excecao que e o coracao do desenho.

        Reapresentar um id existente com outro segredo e **recusado**. Sem essa
        recusa, quem tivesse o token da API sequestraria o id do seu HUD
        registrando-o de novo com um segredo proprio - e todo o resto do
        mecanismo viraria enfeite.
        """
        if not ID_VALIDO.match(dispositivo):
            raise ValueError("id de dispositivo invalido: use 4-64 [a-zA-Z0-9._-]")
        if len(segredo) < SEGREDO_MINIMO:
            raise ValueError(f"segredo curto demais: minimo {SEGREDO_MINIMO} caracteres")

        async with self._cadeado:
            registro = await self._carregar()
            atual = registro.get(dispositivo)
            if atual is not None:
                if not hmac.compare_digest(str(atual["segredo"]), segredo):
                    log.warning("dispositivos.id_tomado", dispositivo=dispositivo)
                    raise IdJaRegistrado(
                        f"o id '{dispositivo}' ja esta registrado com outro segredo"
                    )
                return {"registrado": False, "ja_existia": True}

            registro[dispositivo] = {
                "segredo": segredo,
                "registrado_em": datetime.now(UTC).isoformat(),
            }
            await self._store.meta_set(CHAVE_REGISTRO, json.dumps(registro))

        log.info("dispositivos.registrado", dispositivo=dispositivo, total=len(registro))
        return {"registrado": True, "ja_existia": False}

    async def verificar(self, dispositivo: str, acao: str, token: str, apresentada: str) -> None:
        """Confere a prova. Silencio significa aprovado."""
        registro = await self._carregar()
        entrada = registro.get(dispositivo)
        if entrada is None:
            log.warning("dispositivos.desconhecido", dispositivo=dispositivo, acao=acao)
            raise DispositivoDesconhecido(
                f"dispositivo '{dispositivo}' nao registrado neste Core"
            )

        esperada = prova(str(entrada["segredo"]), acao, token)
        # compare_digest, nao ==: comparacao comum vaza o tamanho do prefixo
        # correto pelo tempo, e prova e material de credencial.
        if not hmac.compare_digest(esperada, apresentada or ""):
            log.warning("dispositivos.prova_invalida", dispositivo=dispositivo, acao=acao)
            raise ProvaInvalida("prova do dispositivo nao confere")

    async def listar(self) -> list[dict[str, Any]]:
        """Sem os segredos - isto sai numa resposta HTTP."""
        registro = await self._carregar()
        return [
            {"dispositivo": nome, "registrado_em": dados.get("registrado_em")}
            for nome, dados in sorted(registro.items())
        ]

    async def esquecer(self, dispositivo: str) -> bool:
        async with self._cadeado:
            registro = await self._carregar()
            if registro.pop(dispositivo, None) is None:
                return False
            await self._store.meta_set(CHAVE_REGISTRO, json.dumps(registro))
        log.info("dispositivos.esquecido", dispositivo=dispositivo)
        return True
