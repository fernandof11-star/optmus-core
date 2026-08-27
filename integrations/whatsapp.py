"""Cliente WhatsApp pelo caminho **nao oficial**, via neonize/whatsmeow.

## O que este arquivo assume, dito na cara

Isto usa o protocolo do WhatsApp Web por engenharia reversa. Nao e suportado
pela Meta, viola os termos de uso, e **a conta sera banida** - a duvida e
quando. Clientes assim duram tipicamente de duas a oito semanas, a deteccao e
automatica e acontece na camada de rede, antes de qualquer mensagem ser lida
por alguem. Nao ha padrao previsivel.

Por isso este modulo existe com tres restricoes que **nao sao configuraveis**:

1. **So numero secundario.** Nunca o numero principal de quem usa.
2. **So local.** Se detectar plataforma hospedada, se recusa a funcionar - ver
   :func:`disponivel`. Nao "desligado por configuracao": impossivel.
3. **Nunca responde sozinho.** Nenhum tratador de evento e registrado aqui.
   O Optmus manda quando voce autoriza, e so. Resposta automatica e o padrao
   de comportamento que mais rapido derruba a conta - e seria tambem o caminho
   por onde uma injecao vinda de uma mensagem recebida se propagaria sozinha.

## O par com o telefone

O whatsmeow guarda a sessao num SQLite proprio. Parear e um passo manual e
unico, feito por ``scripts/whatsapp_parear.py``: o Core nunca inicia pareamento
sozinho, porque isso significaria um QR code aparecendo num log de servidor.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Final

from core.config import Settings
from core.logging import get_logger
from security.api_auth import hospedado

log = get_logger("integrations.whatsapp")

TIMEOUT_CONEXAO_S: Final[float] = 30.0

# Pausa entre envios. Rajada e o sinal de automacao mais obvio que existe, e o
# Optmus nao tem caso de uso legitimo para dois envios no mesmo segundo.
INTERVALO_MINIMO_S: Final[float] = 3.0


class WhatsAppError(RuntimeError):
    """Falha no envio ou na sessao."""


class WhatsAppIndisponivel(WhatsAppError):
    """Sem neonize, sem sessao pareada, ou rodando onde nao devia."""


def neonize_instalado() -> bool:
    try:
        import neonize  # noqa: F401
    except ImportError:
        return False
    return True


def sessao_vinculada(caminho: Path) -> tuple[bool, str]:
    """O arquivo de sessao tem um aparelho vinculado de verdade?

    **Existir nao e estar pareado.** O whatsmeow cria o SQLite com as 17 tabelas
    no instante em que o cliente e construido, muito antes de alguem digitar o
    codigo no telefone. Um pareamento abandonado no meio deixa um arquivo de
    160 kB com zero aparelhos - e foi exatamente o que aconteceu em 25/08/2026,
    quando `Path.exists()` era a checagem: a ferramenta se ofereceu ao modelo,
    o portao pediu autorizacao, e so DEPOIS do humano autorizar e que o envio
    descobriu que nao havia sessao. Autorizar uma acao impossivel e o pior
    lugar possivel para essa descoberta.

    A tabela ``whatsmeow_device`` e observacao; o arquivo existir e crenca.
    """
    if not caminho.exists():
        return False, "sem sessao pareada: rode python scripts/whatsapp_parear.py"

    try:
        con = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True, timeout=2.0)
        try:
            (quantos,) = con.execute("SELECT COUNT(*) FROM whatsmeow_device").fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        # Falha fechando: nao dar para confirmar o pareamento nao e o mesmo que
        # estar pareado. O log diz o motivo; a ferramenta some do schema.
        return False, f"nao consegui ler a sessao ({exc}). Pareie de novo."

    if quantos == 0:
        return False, (
            "a sessao existe mas NENHUM aparelho esta vinculado - o pareamento "
            "nao completou. Apague o arquivo e rode python scripts/whatsapp_parear.py"
        )
    return True, "ok"


def disponivel(settings: Settings) -> tuple[bool, str]:
    """Pode funcionar aqui? Devolve tambem o motivo, para o log e o /health.

    A checagem de plataforma hospedada vem **primeiro** e nao pergunta nenhuma
    configuracao. O raciocinio e o mesmo do ``verificar_exposicao``: uma flag
    que diz "estou local" e uma crenca, e crenca erra. ``hospedado()`` olha as
    variaveis que Railway, Render, Heroku e Fly injetam - isso e observacao.

    Rodar isto no Railway seria mandar mensagem de uma conta pessoal a partir de
    um IP de datacenter compartilhado, que e o jeito mais rapido conhecido de
    perder o numero.
    """
    if hospedado():
        return False, "plataforma hospedada: o WhatsApp nao oficial e so local"
    if not settings.whatsapp_enabled:
        return False, "OPTMUS_WHATSAPP_ENABLED=false"
    if not neonize_instalado():
        return False, 'neonize nao instalado: pip install -e ".[whatsapp]"'
    return sessao_vinculada(Path(settings.whatsapp_session_path))


class WhatsAppClient:
    """Envio de texto para um numero ja resolvido pela lista de contatos.

    Recebe **numero**, nao apelido: a decisao de para quem se pode mandar e da
    ``integrations.contatos``, e misturar as duas responsabilidades faria a
    checagem da lista virar algo que da para esquecer de chamar.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cliente: Any = None
        self._ultimo_envio: float = 0.0
        # Serializa os envios: duas ferramentas em paralelo na mesma sessao do
        # whatsmeow e caminho para estado corrompido, e o intervalo minimo
        # perderia o sentido se dois envios o medissem ao mesmo tempo.
        self._cadeado = asyncio.Lock()

    async def _conectar(self) -> Any:
        if self._cliente is not None and self._cliente.is_connected:
            return self._cliente

        ok, motivo = disponivel(self._settings)
        if not ok:
            raise WhatsAppIndisponivel(motivo)

        from neonize.aioze.client import NewAClient

        # Sem tratador de evento nenhum, de proposito - ver o cabecalho.
        cliente = NewAClient(str(self._settings.whatsapp_session_path))
        try:
            await asyncio.wait_for(cliente.connect(), timeout=TIMEOUT_CONEXAO_S)
        except TimeoutError as exc:
            raise WhatsAppError(
                f"a sessao do WhatsApp nao conectou em {TIMEOUT_CONEXAO_S:.0f}s"
            ) from exc

        if not cliente.is_logged_in:
            raise WhatsAppIndisponivel(
                "sessao existe mas nao esta logada: o telefone pode ter "
                "desvinculado o aparelho. Rode python scripts/whatsapp_parear.py"
            )

        self._cliente = cliente
        log.info("whatsapp.conectado")
        return cliente

    async def enviar(self, numero: str, texto: str) -> dict[str, Any]:
        """Manda uma mensagem de texto. ``numero`` em E.164 sem o ``+``."""
        from neonize.utils import build_jid

        async with self._cadeado:
            agora = asyncio.get_running_loop().time()
            espera = INTERVALO_MINIMO_S - (agora - self._ultimo_envio)
            if espera > 0:
                log.info("whatsapp.espacando", segundos=round(espera, 2))
                await asyncio.sleep(espera)

            cliente = await self._conectar()
            try:
                resposta = await cliente.send_message(build_jid(numero), texto)
            except Exception as exc:
                raise WhatsAppError(f"{type(exc).__name__}: {exc}") from exc

            self._ultimo_envio = asyncio.get_running_loop().time()

        # O numero NAO entra no log: ele vai para a trilha de auditoria pelo
        # caminho da ferramenta, ja mascarado. Aqui so o tamanho.
        log.info("whatsapp.enviado", caracteres=len(texto))
        return {"id": getattr(resposta, "ID", None)}

    async def desconectar(self) -> None:
        if self._cliente is not None:
            try:
                self._cliente.disconnect()
            except Exception as exc:  # noqa: BLE001 - desligar nao derruba o processo
                log.warning("whatsapp.desconexao_falhou", erro=str(exc))
            self._cliente = None
