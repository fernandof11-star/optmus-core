"""Revalida a visao de ponta a ponta: webcam real, modelo real, sem mock.

    python scripts/checar_visao.py          # fluxo completo, duas capturas
    python scripts/checar_visao.py --ler    # so o modo de leitura, uma captura

ATENCAO: acende a webcam e GASTA CHAVE DA ANTHROPIC. Nao roda em CI e nao
substitui a suite - tests/test_camera.py e tests/test_visao.py cobrem a logica
sem hardware e sem custo. Este script existe para o que o teste automatizado
nao alcanca: a camera de verdade, o modelo de verdade, e o tempo de verdade.

Rode depois de mexer em perception/camera.py, tools/impl/visao.py ou no laco de
ferramentas do agente.

O modo padrao pede DUAS capturas de proposito: e a unica forma de exercitar o
descarte de imagem da F4.1 no caminho real, porque ele so tem o que fazer a
partir da terceira rodada.

Use --ler com um papel escrito na frente da camera: e o caminho de OCR, que o
modo padrao nao exercita quando nao ha texto na cena.
"""

from __future__ import annotations

import asyncio
import copy
import sys
import time
from typing import Any

from core.agent import MARCADOR_IMAGEM, Agent, ToolOutcome
from core.config import get_settings
from core.llm import LLMClient, LLMTurn, TextSink, escolher_cliente
from memory.store import Store
from security.policy import PolicyEngine, RiskLevel
from tools.impl.visao import OlharTool

PERGUNTA_COMPLETA = (
    "Olhe o ambiente pela webcam. Depois, numa SEGUNDA chamada separada, use o "
    "modo de leitura para conferir se ha algum texto legivel. So entao me "
    "responda o que viu nas duas."
)
PERGUNTA_LEITURA = (
    "Use a webcam no modo de leitura e me diga exatamente o texto que estiver "
    "escrito no papel na minha frente. Se algo estiver ilegivel, diga que nao "
    "deu para ler em vez de adivinhar."
)


def contar_blocos(mensagens: list[dict[str, Any]]) -> tuple[int, int]:
    """(imagens, marcadores) dentro dos tool_result de uma rodada."""
    imagens = marcadores = 0
    for mensagem in mensagens:
        if not isinstance(mensagem.get("content"), list):
            continue
        for bloco in mensagem["content"]:
            if not isinstance(bloco, dict) or not isinstance(bloco.get("content"), list):
                continue
            for parte in bloco["content"]:
                if not isinstance(parte, dict):
                    continue
                if parte.get("type") == "image":
                    imagens += 1
                elif parte.get("type") == "text" and parte.get("text") == MARCADOR_IMAGEM:
                    marcadores += 1
    return imagens, marcadores


class ClienteCronometrado(LLMClient):
    """Espelha o cliente real, guardando mensagens e tempo de cada rodada.

    Copia profunda de propósito: o agente MUTA as mensagens ao descartar
    imagens, entao guardar a referencia mostraria o estado final em todas as
    rodadas - exatamente o que este script precisa distinguir.
    """

    name = "cronometrado"

    def __init__(self, real: LLMClient) -> None:
        self._real = real
        self.rodadas: list[list[dict[str, Any]]] = []
        self.tempos: list[float] = []

    async def available(self) -> bool:
        return await self._real.available()

    def server_tools(self) -> list[dict[str, Any]]:
        return self._real.server_tools()

    async def stream_turn(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: TextSink | None = None,
    ) -> LLMTurn:
        self.rodadas.append(copy.deepcopy(messages))
        imagens, marcadores = contar_blocos(messages)
        inicio = time.perf_counter()
        turno = await self._real.stream_turn(
            system=system, messages=messages, tools=tools, on_text=on_text
        )
        ms = (time.perf_counter() - inicio) * 1000
        self.tempos.append(ms)
        print(f"  [modelo]     rodada {len(self.rodadas)}: {ms:>7.0f} ms   "
              f"imagens={imagens} marcadores={marcadores}")
        return turno


async def main() -> int:
    somente_leitura = "--ler" in sys.argv
    settings = get_settings()
    ferramenta = OlharTool(settings)

    if not await ferramenta.available():
        print('OlharTool indisponivel. Instale: pip install -e ".[visao]"')
        print("E confira OPTMUS_VISION_ENABLED.")
        return 1

    print("Este script acende a webcam e gasta chave da Anthropic.\n")

    # ------------------------------------------------------ portao de risco
    store = await Store(settings.database_path, embedding_dim=settings.embedding_dim).connect()
    await store.migrate()
    decisao = await PolicyEngine(settings, store).avaliar(
        ferramenta="olhar",
        risco=RiskLevel.EXTERNO,
        parametros={"modo": "descrever"},
        resumo=ferramenta.resumir({"modo": "descrever"}),
    )
    print("PORTAO DE CONFIRMACAO (risco EXTERNO)")
    print(f"  exige confirmacao   {decisao.exige_confirmacao}")
    print(f'  frase lida em voz   "{ferramenta.resumir({"modo": "descrever"})}"')
    if not decisao.exige_confirmacao:
        print("  FALHOU: a camera acenderia sem ninguem autorizar.")
        return 1
    print("  a camera nao acende sem um humano confirmando.\n")

    # -------------------------------------------------------------- o turno
    capturas: list[dict[str, Any]] = []

    class RegistroConfirmado:
        """Este script e a confirmacao humana - por isso executa direto."""

        def schemas(self) -> list[dict[str, Any]]:
            return [ferramenta.to_schema()]

        async def execute(self, name: str, arguments: dict[str, Any], **_: Any) -> ToolOutcome:
            inicio = time.perf_counter()
            resultado = await ferramenta.execute(**arguments)
            ms = (time.perf_counter() - inicio) * 1000
            meta = resultado.metadata or {}
            capturas.append({**meta, "ms": ms})
            print(f"  [ferramenta] olhar(modo={arguments.get('modo')}): {ms:>7.0f} ms   "
                  f"{meta.get('resolucao_entregue')} "
                  f"{meta.get('tokens_estimados')} tokens")
            return ToolOutcome(resultado.content, resultado.is_error, tuple(resultado.imagens))

    pergunta = PERGUNTA_LEITURA if somente_leitura else PERGUNTA_COMPLETA
    print(f"TURNO REAL{' (modo leitura)' if somente_leitura else ''}")
    print(f'  pergunta: "{pergunta}"\n')

    cliente = ClienteCronometrado(await escolher_cliente(settings))
    inicio = time.perf_counter()
    resultado = await Agent(cliente, settings, tools=RegistroConfirmado()).run(pergunta)
    total = (time.perf_counter() - inicio) * 1000

    print(f"\n  RESPOSTA:\n    {resultado.text}\n")
    if resultado.erro:
        print(f"  ERRO: {resultado.erro}")
        return 1

    ms_camera = sum(c["ms"] for c in capturas)
    ms_modelo = sum(cliente.tempos)
    tokens = sum(int(c.get("tokens_estimados", 0)) for c in capturas)
    print(f"  capturas      {len(capturas)}")
    print(f"  camera        {ms_camera:>7.0f} ms  ({ms_camera / total * 100:.0f}% do total)")
    print(f"  modelo        {ms_modelo:>7.0f} ms  ({ms_modelo / total * 100:.0f}% do total)")
    print(f"  TOTAL         {total:>7.0f} ms")
    print(f"  tokens de imagem  {tokens}  (~US$ {tokens * 5 / 1_000_000:.4f})")

    # --------------------------------------------------- descarte da F4.1
    print("\nDESCARTE DE IMAGEM (F4.1) NO CAMINHO REAL")
    for i, mensagens in enumerate(cliente.rodadas, 1):
        imagens, marcadores = contar_blocos(mensagens)
        print(f"  rodada {i}: imagens={imagens} marcadores={marcadores}")

    if len(cliente.rodadas) < 3:
        print("  so duas rodadas: a imagem trafegou uma vez, sem desperdicio.")
        print("  o descarte so tem o que fazer a partir da terceira.")
        return 0

    imagens, marcadores = contar_blocos(cliente.rodadas[-1])
    if marcadores >= 1 and imagens >= 1:
        print("  a captura antiga virou marcador e a recente sobreviveu: correto.")
        return 0
    if marcadores == 0 and imagens > 1:
        print("  FALHOU: mais de uma imagem viajando junto - o descarte nao rodou.")
        return 1
    print("  inconclusivo: confira as contagens acima a mao.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
