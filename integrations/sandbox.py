"""Onde o código escrito pelo modelo roda — e por que não é na sua máquina.

Decidido em 26/08/2026: **o modo dev exige Docker**. Com o daemon parado, as
ferramentas de teste e publicação somem do schema, exatamente como o WhatsApp
sem sessão pareada.

A alternativa seria um subprocesso com timeout. Isso limita o *tempo*, e nada
mais: o teste recém-escrito pelo modelo rodaria com os seus privilégios, no seu
disco, com a sua rede — livre para ler o `.env`, apagar arquivo fora do projeto
ou mandar o que achasse para fora. Chamar aquilo de sandbox seria mentira, e
uma mentira sobre isolamento é pior que não ter isolamento, porque você
confiaria nela.

Três propriedades do contêiner, e cada uma existe por um motivo:

- ``--network=none``: teste não fala com o mundo. Corta exfiltração e corta a
  possibilidade de o "teste" instalar algo.
- **Montagem somente-leitura**: o teste não altera o repositório que ele está
  testando. Sem isso, um teste poderia consertar a si mesmo e ficar verde.
- ``--rm`` e timeout: nada sobrevive à execução, nem em disco nem em processo.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from core.logging import get_logger

log = get_logger("integrations.sandbox")

TIMEOUT_PADRAO_S: Final[float] = 300.0
# Tetos de recurso: um teste em laço infinito consumindo toda a RAM da máquina
# derrubaria o Core junto, e o Core é quem está supervisionando.
MEMORIA: Final[str] = "2g"
CPUS: Final[str] = "2"


@dataclass(slots=True)
class Resultado:
    ok: bool
    codigo: int
    saida: str
    motivo: str = ""

    def resumo(self, limite: int = 2000) -> str:
        """Cauda da saída, que é onde pytest põe o que falhou."""
        texto = self.saida.strip()
        return texto if len(texto) <= limite else "[...]\n" + texto[-limite:]


async def _rodar(*args: str, timeout: float) -> tuple[int, str]:
    processo = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        bruto, _ = await asyncio.wait_for(processo.communicate(), timeout=timeout)
    except TimeoutError:
        processo.kill()
        await processo.wait()
        raise
    return processo.returncode or 0, bruto.decode("utf-8", errors="replace")


async def docker_vivo(timeout: float = 20.0) -> tuple[bool, str]:
    """O daemon responde? Não basta o binário existir.

    O CLI do Docker fica instalado mesmo com o Docker Desktop fechado, e nesse
    estado todo comando falha com erro de pipe. Checar o binário diria "sim" e
    a primeira execução real diria "não" — depois de o modelo já ter escrito
    código contando com o teste.
    """
    try:
        codigo, saida = await _rodar("docker", "info", "--format", "{{.ServerVersion}}",
                                     timeout=timeout)
    except FileNotFoundError:
        return False, "docker nao instalado"
    except TimeoutError:
        return False, f"docker nao respondeu em {timeout:.0f}s"
    if codigo != 0:
        return False, "daemon do docker parado (abra o Docker Desktop)"
    return True, saida.strip()


async def rodar_testes(
    raiz: Path, comando: str, imagem: str, *, timeout: float = TIMEOUT_PADRAO_S
) -> Resultado:
    """Roda a suíte do projeto isolada. Verde aqui é pré-requisito de publicar."""
    if not comando.strip():
        return Resultado(False, -1, "", "projeto sem comando de teste declarado")

    vivo, detalhe = await docker_vivo()
    if not vivo:
        return Resultado(False, -1, "", detalhe)

    args = [
        "docker", "run", "--rm",
        "--network=none",
        f"--memory={MEMORIA}",
        f"--cpus={CPUS}",
        # :ro é o que impede um teste de se consertar sozinho para ficar verde.
        "-v", f"{raiz}:/trabalho:ro",
        "-w", "/trabalho",
        imagem,
        "sh", "-lc", comando,
    ]
    try:
        codigo, saida = await _rodar(*args, timeout=timeout)
    except TimeoutError:
        return Resultado(False, -1, "", f"os testes passaram de {timeout:.0f}s e foram mortos")
    except OSError as exc:
        return Resultado(False, -1, "", f"nao consegui rodar o docker: {exc}")

    ok = codigo == 0
    log.info("sandbox.testes", ok=ok, codigo=codigo, imagem=imagem)
    return Resultado(ok, codigo, saida, "" if ok else f"testes falharam (codigo {codigo})")
