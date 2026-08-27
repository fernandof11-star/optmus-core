"""Descobre o seu chat_id do Telegram.

O passo mais chato da configuracao, e o unico que nao da para automatizar: o
Telegram nao revela o chat de ninguem antes de a pessoa falar com o bot. Isso e
proposital - impede que um bot mande mensagem para quem nunca o procurou.

    python scripts/telegram_id.py

Antes de rodar:
  1. Fale com o @BotFather no Telegram, mande /newbot e siga as perguntas.
  2. Ponha o token no .env como OPTMUS_TELEGRAM_BOT_TOKEN.
  3. Abra a conversa com o SEU bot e mande qualquer coisa - um "oi" serve.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import get_settings
from integrations.telegram import BASE_URL


async def principal() -> int:
    import httpx

    settings = get_settings()
    token = settings.telegram_bot_token
    if token is None:
        print("Falta OPTMUS_TELEGRAM_BOT_TOKEN no .env.")
        print("Crie o bot com o @BotFather e cole o token que ele devolver.")
        return 1

    base = f"{BASE_URL}/bot{token.get_secret_value()}"
    async with httpx.AsyncClient(timeout=20.0) as http:
        quem = (await http.get(f"{base}/getMe")).json()
        if not quem.get("ok"):
            print(f"Token recusado pelo Telegram: {quem.get('description')}")
            return 1
        print(f"Bot: @{quem['result'].get('username')}")

        # getUpdates so devolve mensagens recentes (o Telegram guarda 24 h) e
        # NAO devolve nada se o webhook estiver ligado - vale avisar, porque o
        # sintoma seria uma lista vazia sem explicacao.
        dados = (await http.get(f"{base}/getUpdates", params={"limit": 20})).json()

    if not dados.get("ok"):
        print(f"getUpdates recusado: {dados.get('description')}")
        return 1

    chats: dict[int, str] = {}
    for update in dados.get("result", []):
        msg = update.get("message") or update.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            nome = chat.get("username") or chat.get("first_name") or chat.get("title") or "?"
            chats[chat["id"]] = f"{nome} ({chat.get('type')})"

    if not chats:
        print("\nNenhuma mensagem recente.")
        print("Abra a conversa com o bot no Telegram, mande um 'oi' e rode de novo.")
        print("(Se voce ja mandou ha mais de 24 h, mande outra: o Telegram descarta.)")
        return 1

    print("\nChats encontrados:")
    for chat_id, quem_e in chats.items():
        print(f"  OPTMUS_TELEGRAM_CHAT_ID={chat_id}    {quem_e}")
    print("\nCopie a linha do SEU chat para o .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(principal()))
