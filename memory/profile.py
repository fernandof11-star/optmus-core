"""Perfil vivo (``perfil.md``).

Preferencias, pessoas importantes, projetos ativos, rotina. E injetado no
prompt de sistema a cada conversa - e o que faz o Optmus soar como quem conhece
o usuario, sem pagar uma busca de memoria por turno.

**Atualizado por ferramenta explicita, nunca implicitamente.** O consolidador
noturno grava fatos na memoria semantica, mas nao toca aqui. A razao e simples:
o perfil entra em TODO prompt. Um fato errado numa camada vetorial atrapalha uma
busca; um fato errado no perfil contamina todas as conversas seguintes, e o
usuario nao tem como saber por que o assistente comecou a errar. Escrita aqui e
sempre deliberada, e sempre auditavel no arquivo.

Markdown por opcao: da para abrir, ler e corrigir com qualquer editor, sem o
Optmus no meio.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from core.logging import get_logger

log = get_logger("memory.profile")

SECOES_PADRAO: Final[tuple[str, ...]] = (
    "Quem e",
    "Preferencias",
    "Pessoas importantes",
    "Projetos ativos",
    "Rotina",
)

MODELO = """# Perfil

<!-- Arquivo do Optmus. Editavel a mao. Atualizado por ferramenta explicita,
     nunca automaticamente - o que esta aqui entra em toda conversa. -->

{secoes}
"""


class LivingProfile:
    """Leitura e escrita do ``perfil.md``, com cache em memoria."""

    def __init__(self, caminho: Path, *, max_chars: int = 4000) -> None:
        self._caminho = caminho
        self._max_chars = max_chars
        self._cache: str | None = None

    @property
    def caminho(self) -> Path:
        return self._caminho

    @property
    def existe(self) -> bool:
        return self._caminho.exists()

    async def ensure(self) -> None:
        """Cria o arquivo com as secoes vazias, se ainda nao existir."""
        if self._caminho.exists():
            return
        self._caminho.parent.mkdir(parents=True, exist_ok=True)
        corpo = MODELO.format(secoes="\n\n".join(f"## {s}\n\n-" for s in SECOES_PADRAO))
        await asyncio.to_thread(self._caminho.write_text, corpo, "utf-8")
        log.info("perfil.criado", caminho=str(self._caminho))

    async def read(self, *, use_cache: bool = True) -> str:
        if use_cache and self._cache is not None:
            return self._cache
        if not self._caminho.exists():
            self._cache = ""
            return ""
        texto = await asyncio.to_thread(self._caminho.read_text, "utf-8")
        self._cache = texto
        return texto

    async def for_prompt(self) -> str:
        """Versao enxuta para o prompt de sistema: sem comentario, sem vazio."""
        texto = await self.read()
        if not texto.strip():
            return ""
        sem_comentario = re.sub(r"<!--.*?-->", "", texto, flags=re.DOTALL)
        linhas = [
            linha.rstrip()
            for linha in sem_comentario.splitlines()
            if linha.strip() and linha.strip() not in {"-", "*"}
        ]
        return "\n".join(linhas)[: self._max_chars].strip()

    async def update_section(self, secao: str, conteudo: str) -> None:
        """Substitui uma secao inteira. Chamada so por ferramenta explicita."""
        await self.ensure()
        texto = await self.read(use_cache=False)
        bloco = f"## {secao}\n\n{conteudo.strip()}\n"
        padrao = re.compile(rf"^## {re.escape(secao)}\s*$.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL)

        if padrao.search(texto):
            novo = padrao.sub(bloco + "\n", texto)
        else:
            novo = f"{texto.rstrip()}\n\n{bloco}"
        novo = novo.rstrip() + f"\n\n<!-- atualizado em {datetime.now(UTC).isoformat()} -->\n"
        novo = re.sub(r"\n<!-- atualizado em [^>]*-->\n(?=[\s\S]*<!-- atualizado em )", "\n", novo)

        await asyncio.to_thread(self._caminho.write_text, novo, "utf-8")
        self._cache = novo
        log.info("perfil.secao_atualizada", secao=secao, caracteres=len(conteudo))

    def invalidate(self) -> None:
        """Descarta o cache - use depois de editar o arquivo a mao."""
        self._cache = None
