"""Alvos que o Optmus pode abrir no PC.

## Por que uma lista, se ja existe o portao

No Windows, ``os.startfile`` e ``ShellExecute``: **abrir um arquivo e
indistinguivel de executar codigo**. Um ``.lnk`` roda o que apontar, com os
argumentos que carregar; um ``.docm`` roda macro; um ``.bat`` e codigo puro.
O ``PATHEXT`` desta maquina lista treze extensoes que o Windows executa direto.

O portao de confirmacao nao cobre isso, e o motivo e sutil: a frase que a pessoa
le diria *"abrir relatorio.docx"* - e ela **nao tem como distinguir** isso de um
``.docm`` com macro ou de um atalho apontando para outro lugar. Autorizar algo
que nao da para avaliar nao e autorizar; e assinar em branco.

Entao a lista nao substitui o portao, ela o torna avaliavel: o que chega a
confirmacao ja e garantidamente um item que voce registrou, ou um arquivo comum
dentro de uma pasta que voce registrou.

## O contrato: id, nunca caminho

O modelo **nunca** recebe nem envia um caminho. Ele escolhe um ``id`` de uma
lista que o proprio Core produziu, e o Core resolve o id relistando os alvos e
procurando o que bate. Nao existe forma de expressar ``C:\\x.exe`` nesse
contrato - uma instrucao injetada que peca isso nao tem onde escrever.

O id e deterministico (hash do alvo), entao nao ha estado de sessao para
sincronizar nem id que expira no meio de um gesto.

## O arquivo

``data/alvos.json``, fora do controle de versao - sao caminhos da sua maquina.

    {
      "apps":   {"vscode":   {"nome": "VS Code", "caminho": "C:/.../Code.exe"}},
      "pastas": {"projetos": {"nome": "Projetos", "caminho": "C:/Users/.../Documentos"}}
    }

**Apps** sao executaveis que voce escolheu, um por um. **Pastas** sao lugares de
onde arquivos comuns podem ser abertos - e so arquivos comuns: dentro delas, o
que o Windows executa continua barrado (ver :data:`EXTENSOES_PERIGOSAS`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from core.logging import get_logger

log = get_logger("integrations.alvos")

# PATHEXT desta maquina, mais o que o shell executa por associacao. Nao e lista
# de "arquivos suspeitos": e a lista do que ABRIR significa RODAR.
EXTENSOES_PERIGOSAS: Final[frozenset[str]] = frozenset({
    # PATHEXT
    ".com", ".exe", ".bat", ".cmd", ".vbs", ".vbe", ".js", ".jse",
    ".wsf", ".wsh", ".msc", ".py", ".pyw", ".cpl",
    # Executam por associacao ou carregam codigo junto
    ".lnk", ".ps1", ".psm1", ".msi", ".reg", ".scr", ".hta", ".jar",
    ".dll", ".docm", ".xlsm", ".pptm", ".xlam", ".url",
})

# Teto por pasta. Uma pasta com dez mil arquivos viraria um prompt gigante e uma
# tela impossivel de apontar com a mao.
MAXIMO_POR_PASTA: Final[int] = 40


class AlvoDesconhecido(LookupError):
    """O id nao corresponde a nenhum alvo registrado agora."""


class ListaInvalida(ValueError):
    """O arquivo existe mas nao da para confiar nele."""


@dataclass(frozen=True, slots=True)
class Alvo:
    id: str
    nome: str
    tipo: str
    """``app``, ``pasta`` ou ``arquivo``."""

    caminho: Path

    def visivel(self) -> dict[str, str]:
        """O que sai para a tela e para o modelo. **Sem o caminho.**

        O caminho e detalhe da maquina; quem escolhe precisa do nome. Mandar o
        caminho junto so daria ao modelo material para tentar construir outro.
        """
        return {"id": self.id, "nome": self.nome, "tipo": self.tipo}


def identificar(tipo: str, caminho: Path) -> str:
    """Id estavel e deterministico de um alvo.

    Deterministico de proposito: sem estado de sessao para sincronizar entre o
    HUD e o Core, e sem id que expira no meio de um gesto. O mesmo arquivo tem
    o mesmo id agora e daqui a uma hora.
    """
    cru = f"{tipo}|{caminho.resolve().as_posix().lower()}"
    return hashlib.sha256(cru.encode("utf-8")).hexdigest()[:12]


def perigosa(caminho: Path) -> bool:
    return caminho.suffix.lower() in EXTENSOES_PERIGOSAS


def carregar(caminho: Path) -> dict[str, list[Alvo]]:
    """Le o registro. Arquivo ausente devolve vazio, nao erro."""
    if not caminho.exists():
        return {"apps": [], "pastas": []}

    try:
        bruto = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ListaInvalida(f"{caminho.name} nao e JSON valido: {exc}") from exc
    if not isinstance(bruto, dict):
        raise ListaInvalida(f"{caminho.name} precisa ser um objeto com 'apps' e 'pastas'")

    saida: dict[str, list[Alvo]] = {"apps": [], "pastas": []}
    for secao in ("apps", "pastas"):
        for apelido, dados in (bruto.get(secao) or {}).items():
            if not isinstance(dados, dict) or "caminho" not in dados:
                raise ListaInvalida(f"{secao}/{apelido}: falta o campo 'caminho'")
            alvo = Path(str(dados["caminho"])).expanduser()
            tipo = "app" if secao == "apps" else "pasta"
            # Erro alto em vez de pular: um alvo descartado em silencio vira
            # "alvo desconhecido" na hora do uso, e voce procuraria o defeito
            # no gesto, no modelo, na tela - menos no arquivo.
            if secao == "pastas" and alvo.exists() and not alvo.is_dir():
                raise ListaInvalida(f"pastas/{apelido}: {alvo} nao e uma pasta")
            saida[secao].append(
                Alvo(
                    id=identificar(tipo, alvo),
                    nome=str(dados.get("nome") or apelido),
                    tipo=tipo,
                    caminho=alvo,
                )
            )
    log.info("alvos.carregados", apps=len(saida["apps"]), pastas=len(saida["pastas"]))
    return saida


def _arquivos_da_pasta(pasta: Alvo) -> list[Alvo]:
    """Arquivos comuns dentro de uma pasta registrada.

    Nao desce em subpastas: profundidade transformaria "uma pasta registrada"
    em "tudo abaixo dela", que e um registro que voce nao fez conscientemente.
    """
    if not pasta.caminho.is_dir():
        return []

    achados: list[Alvo] = []
    for item in sorted(pasta.caminho.iterdir()):
        if len(achados) >= MAXIMO_POR_PASTA:
            break
        if not item.is_file() or item.name.startswith("."):
            continue
        if perigosa(item):
            # Dentro de pasta registrada o que o Windows executa continua fora.
            # Quem quiser rodar um executavel registra ele como app, um por um,
            # de propria mao.
            continue
        achados.append(
            Alvo(id=identificar("arquivo", item), nome=item.name, tipo="arquivo", caminho=item)
        )
    return achados


def listar(registro: dict[str, list[Alvo]], *, pasta_id: str | None = None) -> list[Alvo]:
    """O que pode ser apontado agora.

    Sem ``pasta_id``, devolve o primeiro nivel: os apps e as pastas. Com ele,
    devolve o conteudo daquela pasta - e so se ela estiver registrada.
    """
    if pasta_id is None:
        return [*registro["apps"], *registro["pastas"]]

    for pasta in registro["pastas"]:
        if pasta.id == pasta_id:
            return _arquivos_da_pasta(pasta)
    raise AlvoDesconhecido(f"'{pasta_id}' nao e uma pasta registrada")


def resolver(registro: dict[str, list[Alvo]], alvo_id: str) -> Alvo:
    """Id -> alvo, procurando em tudo que e alcancavel agora.

    Relista para resolver, em vez de guardar um mapa: o registro pode ter
    mudado desde que a tela foi desenhada, e abrir com base num mapa velho
    abriria algo que ja nao esta autorizado.
    """
    pedido = (alvo_id or "").strip()
    if not pedido:
        raise AlvoDesconhecido("nenhum alvo informado")

    for alvo in [*registro["apps"], *registro["pastas"]]:
        if alvo.id == pedido:
            return alvo
    for pasta in registro["pastas"]:
        for arquivo in _arquivos_da_pasta(pasta):
            if arquivo.id == pedido:
                return arquivo

    log.warning("alvos.id_desconhecido", id=pedido[:16])
    raise AlvoDesconhecido(
        f"'{pedido}' nao e um alvo registrado. Use um id que veio de pc_listar - "
        "caminho de arquivo nao e aceito aqui."
    )
