"""Vincula o Optmus a uma conta de WhatsApp, uma vez.

    python scripts/whatsapp_parear.py SEU_NUMERO

Use o numero do telefone que vai ficar vinculado - **o seu numero secundario**,
com pais e DDD. Nao copie um exemplo de documentacao: o pareamento vai falhar,
e antes de 25/08/2026 ele ainda deixava um arquivo de sessao pela metade que
enganava o Core.

ANTES DE RODAR, leia docs/WHATSAPP.md. Em resumo:

  - Use um numero SECUNDARIO. Este e o caminho nao oficial; a conta sera
    banida em algum momento e nao ha recurso.
  - So rode isto na SUA maquina. Nunca num servidor.

O pareamento e manual e fica fora do Core de proposito: se o Core pareasse
sozinho, um codigo de vinculo apareceria num log de servidor - e quem visse o
log entraria na conta.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import get_settings
from integrations.whatsapp import neonize_instalado, sessao_vinculada
from security.api_auth import hospedado


def _encerrar(codigo: int, caminho: Path | None = None) -> None:
    """Sai de verdade, apagando a sessao pela metade quando houve falha.

    Duas coisas que este script aprendeu na pratica:

    - **Sessao incompleta e pior que nenhuma.** O whatsmeow cria o SQLite
      inteiro ao construir o cliente; um pareamento abandonado deixa um arquivo
      com zero aparelhos que ja fez o Core acreditar estar pareado.
    - **``os._exit`` porque o processo nao morre sozinho.** Depois de
      ``connect()``, o runtime Go do whatsmeow deixa threads que nao sao
      daemon, e o interpretador fica pendurado no fim do ``main`` - medi um
      processo deste script vivo por mais de um dia. Cancelar a tarefa asyncio
      nao resolve: as threads sao de outro runtime.
    """
    if codigo != 0 and caminho is not None and caminho.exists():
        try:
            caminho.unlink()
            print(f"(sessao incompleta removida: {caminho})")
        except OSError as exc:
            print(f"(nao consegui remover {caminho}: {exc} - apague a mao)")

    sys.stdout.flush()
    os._exit(codigo)


async def principal(numero: str) -> int:
    if hospedado():
        print("Recusado: isto e uma plataforma hospedada.")
        print("O WhatsApp nao oficial so roda na sua maquina - ver docs/WHATSAPP.md.")
        return 2

    if not neonize_instalado():
        print('neonize nao instalado. Rode: pip install -e ".[whatsapp]"')
        return 1

    limpo = re.sub(r"[\s()+.-]", "", numero)
    if not re.match(r"^\d{8,15}$", limpo):
        print(f"'{numero}' nao parece E.164. Exemplo: +55 11 98765-4321")
        return 1

    settings = get_settings()
    caminho = Path(settings.whatsapp_session_path)
    caminho.parent.mkdir(parents=True, exist_ok=True)

    # Checagem de disco instantanea, antes de qualquer I/O de rede:
    # anyio.Path aqui seria cerimonia num script de linha de comando.
    if caminho.exists():  # noqa: ASYNC240
        vinculada, motivo = sessao_vinculada(caminho)
        if vinculada:
            print(f"Ja existe uma sessao VINCULADA em {caminho}.")
            print("Apague o arquivo se quiser parear outra conta - e desvincule")
            print("o aparelho antigo no telefone (Aparelhos conectados).")
            return 1
        # Sessao pela metade de uma tentativa anterior: comeca limpo em vez de
        # mandar a pessoa apagar um arquivo a mao.
        print(f"Sessao anterior incompleta ({motivo.split(' - ')[0]}). Recomecando.")
        caminho.unlink()  # noqa: ASYNC240

    from neonize.aioze.client import NewAClient

    cliente = NewAClient(str(caminho))
    print(f"Conectando... (sessao em {caminho})")
    conexao = asyncio.create_task(cliente.connect())
    await asyncio.sleep(3)

    try:
        codigo = await cliente.PairPhone(limpo, True)
    except Exception as exc:
        print(f"Falhou ao pedir o codigo: {type(exc).__name__}: {exc}")
        _encerrar(1, caminho)

    print()
    print(f"  CODIGO DE VINCULO:  {codigo}")
    print()
    print("No telefone com esse numero:")
    print("  WhatsApp -> Aparelhos conectados -> Conectar aparelho")
    print("  -> Conectar com numero de telefone -> digite o codigo acima")
    print()
    print("Esperando ate 2 minutos...")

    for _ in range(120):
        await asyncio.sleep(1)
        if cliente.is_logged_in:
            print("\nVinculado. Agora ponha no .env:")
            print("  OPTMUS_WHATSAPP_ENABLED=true")
            print("E crie data/contatos.json - o formato esta em docs/WHATSAPP.md.")
            print("Sem contatos na lista a ferramenta nao aparece para o modelo.")
            conexao.cancel()
            return 0

    print("\nTempo esgotado, nao vinculou. O codigo expira rapido - tente de novo.")
    conexao.cancel()
    return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(1)
    _encerrar(asyncio.run(principal(sys.argv[1])))
