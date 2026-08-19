"""Confere se um Optmus Core exposto exige autenticacao de verdade.

Rode contra producao DEPOIS de todo deploy que mexa em configuracao:

    python scripts/checar_auth.py https://optmus.up.railway.app

Sai com codigo 0 so se a API estiver fechada. Qualquer rota privada que
responda sem token valido e falha - foi assim que o Core ficou publico em
2026-08-18, com POST /chat aceitando a string "SEU_OPTMUS_API_TOKEN_AQUI".

Nao precisa do token real: a checagem e justamente que token invalido nao passa.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request

TIMEOUT = 20
TOKEN_FALSO = "SEU_OPTMUS_API_TOKEN_AQUI"

# (metodo, caminho, corpo) - rotas que NUNCA devem responder sem token valido.
PRIVADAS: tuple[tuple[str, str, bytes | None], ...] = (
    ("POST", "/chat", b'{"mensagem":"teste de autenticacao"}'),
    ("GET", "/memoria/buscar?consulta=teste", None),
    ("GET", "/seguranca/auditoria", None),
    ("GET", "/ferramentas", None),
)


def bater(base: str, metodo: str, caminho: str, corpo: bytes | None, token: str | None) -> int:
    """Codigo HTTP da resposta. 0 quando a conexao nem aconteceu."""
    pedido = urllib.request.Request(f"{base}{caminho}", data=corpo, method=metodo)
    pedido.add_header("Content-Type", "application/json")
    if token is not None:
        pedido.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(pedido, timeout=TIMEOUT) as r:
            return int(r.status)
    except urllib.error.HTTPError as erro:
        return int(erro.code)
    except OSError as erro:
        print(f"    conexao falhou: {erro}")
        return 0


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    base = sys.argv[1].rstrip("/")
    print(f"Conferindo {base}\n")

    vivo = bater(base, "GET", "/health/live", None, None)
    print(f"  GET  /health/live (publica de proposito)      HTTP {vivo}")
    if vivo != 200:
        print("\n  FALHOU: o Core nao respondeu. Confira a URL e se o deploy subiu.")
        return 1

    abertas: list[str] = []
    for metodo, caminho, corpo in PRIVADAS:
        for rotulo, token in (("sem token", None), ("token invalido", TOKEN_FALSO)):
            codigo = bater(base, metodo, caminho, corpo, token)
            veredito = "ok, negado" if codigo in (401, 403) else "ABERTA"
            print(f"  {metodo:<4} {caminho:<38} {rotulo:<15} HTTP {codigo}  {veredito}")
            if codigo not in (401, 403, 0):
                abertas.append(f"{metodo} {caminho} ({rotulo})")

    print()
    if abertas:
        print("  API ABERTA. Estas rotas responderam sem autenticacao valida:")
        for rota in abertas:
            print(f"    - {rota}")
        print("\n  Configure OPTMUS_API_TOKEN no painel e reinicie o servico.")
        return 1

    print("  Todas as rotas privadas exigiram autenticacao.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
