"""Onde o Optmus pode escrever código — e onde não pode, estruturalmente.

## O contrato, igual ao do F8

O modelo **nunca** envia um caminho absoluto. Ele manda um ``projeto_id`` (de
uma lista que você escreveu) e um caminho **relativo**. Quem monta o caminho
final é este módulo, e ele confere que o resultado caiu dentro da raiz.

A conferência é feita **depois** de ``resolve()``, de propósito: isso resolve
``..`` e **segue symlink**. Um link dentro do projeto apontando para
``C:/Users/...ssh`` deixaria de ser projeto e viraria porta dos fundos se a
checagem fosse feita no caminho cru.

## A negação que menos parece importante, e mais importa

``.git/`` é proibido para escrita.

Escrever ``.git/hooks/pre-commit`` é **execução de código arbitrário no próximo
comando git** - e o próximo comando git é o que o próprio modo dev roda para
commitar. Seria um jeito de furar sandbox, portão e auditoria de uma vez, sem
nunca chamar uma ferramenta de deploy. ``.env*`` e ``data/`` seguem junto:
segredos e o estado vivo do Core (contatos, alvos, sessão do WhatsApp).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from core.logging import get_logger

log = get_logger("integrations.projetos")

# Prefixos relativos proibidos, comparados em minúsculas e com "/" normalizado.
NEGADOS: Final[tuple[str, ...]] = (".git/", ".env", "data/", ".venv/", "node_modules/")

# Teto de tamanho por arquivo escrito. Um arquivo de 10 MB gerado por engano
# entope o repositório e o diff, e nenhum arquivo de código legítimo chega perto.
BYTES_MAXIMOS: Final[int] = 512 * 1024


class ProjetoDesconhecido(LookupError):
    """Id fora da lista registrada."""


class ForaDaSuperficie(PermissionError):
    """O caminho resolvido caiu fora da raiz do projeto, ou em zona negada."""


class ListaInvalida(ValueError):
    """O arquivo de projetos existe mas não dá para confiar nele."""


@dataclass(frozen=True, slots=True)
class Projeto:
    id: str
    nome: str
    raiz: Path
    """Comando de teste, rodado DENTRO do contêiner. Sem ele não há publicação."""
    testes: str
    """Imagem Docker que tem as dependências do projeto instaladas."""
    imagem: str
    branch: str

    def visivel(self) -> dict[str, str]:
        """O que sai para o modelo. Sem a raiz - é caminho da máquina."""
        return {"id": self.id, "nome": self.nome, "branch": self.branch}


def carregar(caminho: Path) -> dict[str, Projeto]:
    if not caminho.exists():
        return {}
    try:
        bruto = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ListaInvalida(f"{caminho.name} nao e JSON valido: {exc}") from exc
    if not isinstance(bruto, dict):
        raise ListaInvalida(f"{caminho.name} precisa ser um objeto de id -> projeto")

    projetos: dict[str, Projeto] = {}
    for pid, dados in bruto.items():
        if not isinstance(dados, dict) or "raiz" not in dados:
            raise ListaInvalida(f"projeto '{pid}': falta o campo 'raiz'")
        raiz = Path(str(dados["raiz"])).expanduser()
        if not raiz.is_dir():
            # Erro alto: uma raiz que nao existe viraria "fora da superficie"
            # em toda escrita, e voce procuraria o defeito no caminho relativo.
            raise ListaInvalida(f"projeto '{pid}': raiz {raiz} nao existe")
        if not dados.get("testes"):
            # Sem comando de teste nao ha como ter teste verde, e sem teste
            # verde nao ha publicacao. Um projeto assim so pode ser editado.
            log.warning("projetos.sem_testes", projeto=pid)
        projetos[pid] = Projeto(
            id=pid,
            nome=str(dados.get("nome") or pid),
            raiz=raiz.resolve(),
            testes=str(dados.get("testes") or ""),
            imagem=str(dados.get("imagem") or "python:3.12-slim"),
            branch=str(dados.get("branch") or "main"),
        )
    log.info("projetos.carregados", quantidade=len(projetos))
    return projetos


def resolver_projeto(projetos: dict[str, Projeto], pid: str) -> Projeto:
    projeto = projetos.get((pid or "").strip())
    if projeto is None:
        conhecidos = ", ".join(sorted(projetos)) or "(nenhum)"
        raise ProjetoDesconhecido(
            f"'{pid}' nao e um projeto registrado. Disponiveis: {conhecidos}"
        )
    return projeto


def negado(relativo: str) -> bool:
    """Zona proibida dentro do próprio projeto.

    O prefixo ``./`` é removido por fatia, e **não** com ``lstrip("./")``:
    ``lstrip`` recebe um CONJUNTO de caracteres, então ele comeria também o
    ponto de ``.git`` e ``.env``. A primeira versão fazia exatamente isso, e o
    efeito era que a negação mais importante do F10 não negava nada — um teste
    de ``.git/hooks/pre-commit`` passou direto.
    """
    limpo = relativo.replace("\\", "/").lower()
    while limpo.startswith("./"):
        limpo = limpo[2:]
    return any(limpo == n.rstrip("/") or limpo.startswith(n) for n in NEGADOS)


def resolver_caminho(projeto: Projeto, relativo: str) -> Path:
    """Caminho relativo -> absoluto, **dentro** da raiz. Ou erro.

    Recusa caminho absoluto na entrada em vez de tentar interpretá-lo: aceitar
    ``C:/...`` e depois checar contenção convidaria a um empate entre "o que o
    modelo pediu" e "o que o disco tem". Aqui só existe uma forma de pedir.
    """
    pedido = (relativo or "").strip().replace("\\", "/")
    if not pedido:
        raise ForaDaSuperficie("nenhum arquivo informado")
    # `is_absolute()` no Windows exige letra de drive: "/etc/passwd" passaria.
    # Por isso a barra inicial e a letra de drive são checadas à mão.
    if Path(pedido).is_absolute() or pedido.startswith("/") or (
        len(pedido) > 1 and pedido[1] == ":"
    ):
        raise ForaDaSuperficie(
            "caminho absoluto nao e aceito. Use um caminho relativo a raiz do projeto."
        )
    if negado(pedido):
        raise ForaDaSuperficie(
            f"'{pedido}' esta em zona protegida ({', '.join(NEGADOS)}). "
            "Escrever em .git/hooks seria execucao de codigo no proximo commit."
        )

    alvo = (projeto.raiz / pedido).resolve()
    # A checagem vem DEPOIS do resolve: e ele que desfaz ".." e segue symlink.
    # Comparar strings antes disso deixaria passar um link para fora.
    if not alvo.is_relative_to(projeto.raiz):
        log.warning("projetos.fuga_barrada", projeto=projeto.id, pedido=pedido[:80])
        raise ForaDaSuperficie(
            f"'{pedido}' sai da raiz do projeto {projeto.nome}."
        )
    # Reconfere a zona negada no caminho JA resolvido: "docs/../.git/hooks/x"
    # so revela o `.git` depois de resolvido.
    if negado(alvo.relative_to(projeto.raiz).as_posix()):
        raise ForaDaSuperficie(f"'{pedido}' resolve para dentro de zona protegida.")
    return alvo


def conferir_tamanho(conteudo: str) -> None:
    tamanho = len(conteudo.encode("utf-8"))
    if tamanho > BYTES_MAXIMOS:
        raise ForaDaSuperficie(
            f"arquivo de {tamanho // 1024} kB passa do teto de "
            f"{BYTES_MAXIMOS // 1024} kB por escrita"
        )
