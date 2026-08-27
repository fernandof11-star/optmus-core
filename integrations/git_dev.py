"""Operações de git para o modo desenvolvedor.

## Duas regras que valem para toda função aqui

**Nunca shell.** Tudo passa por ``create_subprocess_exec`` com argv explícito.
Uma mensagem de commit escrita pelo modelo contendo ``; rm -rf /`` é apenas uma
mensagem esquisita; pela shell seria um comando. Não há string de comando
montada em lugar nenhum deste arquivo.

**O destino nunca vem do modelo.** Remote e branch saem do registro de projetos,
igual ao destino do Telegram. Uma instrução injetada num comentário de código
não consegue redirecionar um push para outro repositório, porque não existe
parâmetro onde escrever isso.

## O limiar de deleção

O freio estrutural contra "apaga tudo": antes de commitar, conta quantos
arquivos o índice está removendo. Acima do teto, recusa. Não depende de o
código gerado ser bom, nem de alguém revisar — é aritmética sobre o índice.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from core.logging import get_logger

log = get_logger("integrations.git_dev")

TIMEOUT_S: Final[float] = 120.0
# Push pode demorar em repositório grande ou rede ruim.
TIMEOUT_REDE_S: Final[float] = 300.0


class GitFalhou(RuntimeError):
    """O comando git terminou com erro."""


@dataclass(slots=True)
class Mudancas:
    modificados: list[str] = field(default_factory=list)
    novos: list[str] = field(default_factory=list)
    apagados: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.modificados) + len(self.novos) + len(self.apagados)

    def resumo(self) -> str:
        return (
            f"{len(self.novos)} novo(s), {len(self.modificados)} modificado(s), "
            f"{len(self.apagados)} apagado(s)"
        )


async def _git(raiz: Path, *args: str, timeout: float = TIMEOUT_S) -> tuple[int, str]:
    """Roda git com argv explícito. Sem shell, nunca."""
    processo = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(raiz),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        bruto, _ = await asyncio.wait_for(processo.communicate(), timeout=timeout)
    except TimeoutError:
        processo.kill()
        await processo.wait()
        raise GitFalhou(f"git {args[0]} passou de {timeout:.0f}s") from None
    return processo.returncode or 0, bruto.decode("utf-8", errors="replace")


async def _exigir(raiz: Path, *args: str, timeout: float = TIMEOUT_S) -> str:
    codigo, saida = await _git(raiz, *args, timeout=timeout)
    if codigo != 0:
        raise GitFalhou(f"git {' '.join(args[:2])}: {saida.strip()[:400]}")
    return saida


async def e_repositorio(raiz: Path) -> bool:
    codigo, _ = await _git(raiz, "rev-parse", "--is-inside-work-tree")
    return codigo == 0


async def sha_atual(raiz: Path) -> str:
    return (await _exigir(raiz, "rev-parse", "HEAD")).strip()


async def branch_atual(raiz: Path) -> str:
    return (await _exigir(raiz, "rev-parse", "--abbrev-ref", "HEAD")).strip()


async def mudancas(raiz: Path) -> Mudancas:
    """O que mudou na árvore, incluindo o que não está no índice ainda."""
    saida = await _exigir(raiz, "status", "--porcelain", "--untracked-files=all")
    saldo = Mudancas()
    for linha in saida.splitlines():
        if len(linha) < 4:
            continue
        marca, caminho = linha[:2], linha[3:].strip()
        if marca == "??" or "A" in marca:
            saldo.novos.append(caminho)
        elif "D" in marca:
            saldo.apagados.append(caminho)
        else:
            saldo.modificados.append(caminho)
    return saldo


async def apagados_no_indice(raiz: Path) -> list[str]:
    """Arquivos que o índice está REMOVENDO. Base do limiar de deleção.

    Lido do índice, e não da árvore: é o índice que vira commit. Contar a
    árvore deixaria passar um ``git rm --cached`` e barraria um arquivo apagado
    que ninguém preparou para commit.
    """
    saida = await _exigir(raiz, "diff", "--cached", "--name-status", "--diff-filter=D")
    return [linha.split("\t", 1)[-1].strip() for linha in saida.splitlines() if linha.strip()]


async def preparar_tudo(raiz: Path) -> None:
    await _exigir(raiz, "add", "-A")


async def commitar(raiz: Path, mensagem: str, *, autor: str) -> str:
    """Commita o índice e devolve o SHA novo.

    A mensagem entra como argv de ``-m``: git trata o próximo argumento como
    valor literal, então uma mensagem começando com ``--force`` continua sendo
    texto e não vira opção.
    """
    texto = mensagem.strip()
    if not texto:
        raise GitFalhou("mensagem de commit vazia")
    await _exigir(raiz, "-c", f"user.name={autor}", "-c", "user.email=optmus@local",
                  "commit", "-m", texto)
    return await sha_atual(raiz)


async def enviar(raiz: Path, branch: str) -> str:
    """Push para o branch do registro. **Nunca com --force.**

    Force-push reescreve histórico e é DESTRUTIVO - continua exigindo portão e
    frase-código, e por isso não existe caminho para ele aqui dentro.
    """
    return await _exigir(raiz, "push", "origin", branch, timeout=TIMEOUT_REDE_S)


async def reverter(raiz: Path, sha: str) -> str:
    """Desfaz um commit criando outro. Devolve o SHA do revert.

    ``revert`` e não ``reset``: não reescreve histórico, então o desfazer é
    auditável e não precisa de força para chegar ao remoto. Num caminho sem
    humano no deploy, poder voltar sem destruir nada é o que substitui parte da
    função que a confirmação teria tido.
    """
    await _exigir(raiz, "revert", "--no-edit", sha)
    return await sha_atual(raiz)


async def diferenca(raiz: Path, quantas_linhas: int = 200) -> str:
    """Diff resumido, para o modelo ver o que está prestes a publicar."""
    saida = await _exigir(raiz, "diff", "--cached", "--stat")
    linhas = saida.splitlines()
    if len(linhas) <= quantas_linhas:
        return saida
    return "\n".join(linhas[:quantas_linhas]) + f"\n[... mais {len(linhas) - quantas_linhas}]"
