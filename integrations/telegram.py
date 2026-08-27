"""Cliente da Bot API do Telegram.

Caminho **oficial**: a Bot API e publica, gratuita, sem processo de aprovacao e
sem risco de banimento de conta. Foi escolhida para os avisos proativos
justamente por isso - e por nao ter a janela de 24 horas do WhatsApp Business,
que transformaria "bom dia, senhor, o senhor tem prova hoje" num template
aprovado pela Meta, sem nenhuma das palavras do Optmus.

Este modulo so fala HTTP. Quem decide o que mandar e ``tools/impl/telegram.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

from core.config import Settings
from core.logging import get_logger

log = get_logger("integrations.telegram")

BASE_URL: Final[str] = "https://api.telegram.org"
# Teto do Telegram para uma mensagem. Acima disso a API recusa a chamada
# inteira - e perder o aviso todo por excesso de texto seria pior que corta-lo.
LIMITE_DE_TEXTO: Final[int] = 4096
TIMEOUT_S: Final[float] = 15.0
TENTATIVAS: Final[int] = 3


class TelegramError(RuntimeError):
    """O Telegram recusou ou nao respondeu."""


class TelegramNaoConfigurado(TelegramError):
    """Falta OPTMUS_TELEGRAM_BOT_TOKEN ou OPTMUS_TELEGRAM_CHAT_ID."""


def truncar(texto: str, limite: int = LIMITE_DE_TEXTO) -> str:
    """Corta no limite do Telegram, avisando que cortou.

    O aviso importa: uma mensagem que termina no meio de uma frase parece
    mensagem corrompida, e a pessoa fica sem saber se faltou conteudo ou se o
    Optmus se perdeu.
    """
    if len(texto) <= limite:
        return texto
    marca = "\n[...] mensagem cortada no limite do Telegram"
    return texto[: limite - len(marca)] + marca


class TelegramClient:
    """Envio de mensagem para UM destino, fixado na configuracao."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def configurado(self) -> bool:
        return (
            self._settings.telegram_bot_token is not None
            and bool(self._settings.telegram_chat_id)
        )

    def _url(self, metodo: str) -> str:
        token = self._settings.telegram_bot_token
        if token is None or not self._settings.telegram_chat_id:
            raise TelegramNaoConfigurado(
                "Telegram nao configurado. Crie um bot com o @BotFather, ponha o "
                "token em OPTMUS_TELEGRAM_BOT_TOKEN e descubra seu chat com "
                "`python scripts/telegram_id.py`."
            )
        return f"{BASE_URL}/bot{token.get_secret_value()}/{metodo}"

    async def enviar(self, texto: str) -> dict[str, Any]:
        """Manda uma mensagem para o destino configurado.

        **Sem `parse_mode`, de proposito.** O Markdown do Telegram exige escapar
        mais de dez caracteres, e um `-` ou `.` solto derruba a mensagem inteira
        com "can't parse entities". O Optmus ja escreve em texto puro - o prompt
        de sistema proibe markdown porque a resposta vira audio -, entao formatar
        aqui so adicionaria uma classe de falha sem ganho nenhum.
        """
        import httpx

        corpo = {
            "chat_id": self._settings.telegram_chat_id,
            "text": truncar(texto),
            "disable_web_page_preview": True,
        }
        url = self._url("sendMessage")

        for tentativa in range(TENTATIVAS):
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT_S) as http:
                    resposta = await http.post(url, json=corpo)
            except Exception as exc:  # rede instavel merece retry, nao falha
                if tentativa == TENTATIVAS - 1:
                    raise TelegramError(f"{type(exc).__name__}: {exc}") from exc
                await asyncio.sleep(0.5 * (2**tentativa))
                continue

            if resposta.status_code == 429:
                # O Telegram diz quanto esperar. Ignorar so gera mais 429.
                espera = float(
                    (resposta.json().get("parameters") or {}).get("retry_after", 1)
                )
                log.warning("telegram.rate_limit", espera_s=espera)
                await asyncio.sleep(min(espera, 10.0))
                continue

            dados = resposta.json()
            if not dados.get("ok"):
                # A descricao do Telegram e especifica e acionavel - "chat not
                # found" diz o que conferir, "HTTP 400" nao diz nada.
                raise TelegramError(str(dados.get("description", f"HTTP {resposta.status_code}")))

            log.info("telegram.enviado", caracteres=len(corpo["text"]))
            return dict(dados.get("result", {}))

        raise TelegramError("Telegram nao respondeu apos as tentativas")
