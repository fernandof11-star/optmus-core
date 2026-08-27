"""Modo desenvolvedor: o Optmus programa nos seus projetos.

Seis ferramentas, com riscos deliberadamente diferentes:

===================  ==========  ===============================================
``dev_listar``       LEITURA     que projetos existem, e o que mudou neles
``dev_ler``          LEITURA     conteúdo de um arquivo dentro da superfície
``dev_escrever``     ESCRITA     grava, só dentro da superfície
``dev_testar``       ESCRITA     roda a suíte em contêiner isolado
``dev_publicar``     EXTERNO     commit + push. **Portão dispensado** (26/08)
``dev_reverter``     EXTERNO     desfaz o último deploy autônomo
===================  ==========  ===============================================

## A dispensa do portão, e o que ela não dispensa

Você revogou a trava humana no caminho do deploy: "controladamente" significa
que **você** controla a frequência, não que existe aprovação técnica no meio.

O que continua valendo, sem exceção:

- **Sandbox.** ``dev_publicar`` exige testes verdes rodados em contêiner com
  ``--network=none`` e montagem somente-leitura. Docker parado = sem publicar.
- **Auditoria.** Toda ação vira linha na trilha. Sem humano no caminho, o
  registro passa a ser a única forma de saber o que aconteceu.
- **Limite de superfície.** Escrita e leitura só dentro dos projetos
  registrados, e nunca em ``.git/``, ``.env*`` ou ``data/``.

## Por que o projeto não é parâmetro do publicar

Ele vem da mesma chamada que escreveu os arquivos, e a checagem final compara o
que está para ser publicado com o projeto pedido. Uma instrução injetada num
comentário de código não consegue redirecionar um push para outro repositório -
não há onde escrever isso, do mesmo jeito que o Telegram não aceita
destinatário.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from core.config import Settings
from core.logging import get_logger
from integrations import git_dev
from integrations.projetos import (
    ForaDaSuperficie,
    ListaInvalida,
    Projeto,
    ProjetoDesconhecido,
    carregar,
    conferir_tamanho,
    resolver_caminho,
    resolver_projeto,
)
from integrations.sandbox import docker_vivo, rodar_testes
from memory.store import Store
from security.api_auth import hospedado
from security.policy import RiskLevel
from tools.registry import Tool, ToolResult

log = get_logger("tools.dev")

CHAVE_DEPLOY = "dev:ultimo_deploy:{projeto}"
LINHAS_NO_LISTAR = 60


def disponivel(settings: Settings) -> tuple[bool, str]:
    """Mesma ordem do WhatsApp e do F8: plataforma primeiro, config depois.

    ``hospedado()`` é observação; uma flag dizendo "estou local" é crença. E um
    modo dev rodando num container de servidor editaria um clone efêmero que
    ninguém vê, com credencial de deploy na mão.
    """
    if hospedado():
        return False, "plataforma hospedada: o modo dev e so local"
    if not settings.dev_enabled:
        return False, "OPTMUS_DEV_ENABLED=false"
    return True, "ok"


class _FerramentaDev(Tool):
    def __init__(self, settings: Settings, store: Store | None = None) -> None:
        self._settings = settings
        self._store = store

    def _projetos(self) -> dict[str, Projeto]:
        try:
            return carregar(Path(self._settings.dev_projects_path))
        except ListaInvalida as exc:
            log.error("dev.registro_invalido", erro=str(exc))
            return {}

    def _projeto(self, pid: str) -> Projeto:
        return resolver_projeto(self._projetos(), pid)

    async def available(self) -> bool:
        ok, motivo = disponivel(self._settings)
        if not ok:
            log.info("dev.indisponivel", motivo=motivo)
            return False
        if not self._projetos():
            log.info(
                "dev.indisponivel",
                motivo=f"nenhum projeto em {self._settings.dev_projects_path}",
            )
            return False
        return True


_PROJETO_ID: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 60,
    "description": "Id de um projeto registrado, vindo de dev_listar.",
}
_RELATIVO: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 400,
    "description": (
        "Caminho RELATIVO a raiz do projeto (ex: src/app.py). Caminho absoluto "
        "nao e aceito."
    ),
}


class DevListarTool(_FerramentaDev):
    """Projetos registrados e o que mudou em cada um."""

    name = "dev_listar"
    risk = RiskLevel.LEITURA
    description = (
        "Lista os projetos em que o usuario autorizou o Optmus a programar, e "
        "o que mudou na arvore de cada um. Sem projeto_id, lista todos. Nao "
        "escreve nada."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"projeto_id": {**_PROJETO_ID, "description": "Opcional."}},
        "required": [],
        "additionalProperties": False,
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        projetos = self._projetos()
        pedido = str(kwargs.get("projeto_id") or "").strip()

        if pedido:
            try:
                escolhidos = [self._projeto(pedido)]
            except ProjetoDesconhecido as exc:
                return ToolResult.erro(str(exc))
        else:
            escolhidos = list(projetos.values())

        if not escolhidos:
            return ToolResult(content="Nenhum projeto registrado para o modo dev.")

        linhas: list[str] = []
        for projeto in escolhidos:
            if not await git_dev.e_repositorio(projeto.raiz):
                linhas.append(f"- [{projeto.id}] {projeto.nome} — nao e repositorio git")
                continue
            saldo = await git_dev.mudancas(projeto.raiz)
            branch = await git_dev.branch_atual(projeto.raiz)
            linhas.append(
                f"- [{projeto.id}] {projeto.nome} (branch {branch}): {saldo.resumo()}"
            )
            for arquivo in (saldo.novos + saldo.modificados + saldo.apagados)[:LINHAS_NO_LISTAR]:
                linhas.append(f"    {arquivo}")

        return ToolResult(
            content="\n".join(linhas),
            dados={"projetos": [p.visivel() for p in escolhidos]},
        )


class DevLerTool(_FerramentaDev):
    """Conteúdo de um arquivo, dentro da superfície."""

    name = "dev_ler"
    risk = RiskLevel.LEITURA
    description = (
        "Le um arquivo de um projeto registrado. O caminho e relativo a raiz do "
        "projeto. Arquivos de segredo (.env) e o diretorio .git sao inacessiveis."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"projeto_id": _PROJETO_ID, "arquivo": _RELATIVO},
        "required": ["projeto_id", "arquivo"],
        "additionalProperties": False,
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            projeto = self._projeto(str(kwargs.get("projeto_id", "")))
            alvo = resolver_caminho(projeto, str(kwargs.get("arquivo", "")))
        except (ProjetoDesconhecido, ForaDaSuperficie) as exc:
            return ToolResult.erro(str(exc))

        if not alvo.is_file():
            return ToolResult.erro(f"{kwargs.get('arquivo')} nao existe em {projeto.nome}.")
        try:
            conteudo = alvo.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return ToolResult.erro(f"nao consegui ler: {exc}")

        return ToolResult(
            content=conteudo,
            metadata={"projeto": projeto.id, "arquivo": str(kwargs.get("arquivo")),
                      "bytes": len(conteudo.encode("utf-8"))},
        )


class DevEscreverTool(_FerramentaDev):
    """Grava um arquivo, só dentro da superfície."""

    name = "dev_escrever"
    risk = RiskLevel.ESCRITA
    description = (
        "Escreve (ou sobrescreve) um arquivo num projeto registrado. Caminho "
        "relativo a raiz. Nao commita nem publica - para isso existe "
        "dev_publicar. Nao alcanca .git, .env nem data/."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "projeto_id": _PROJETO_ID,
            "arquivo": _RELATIVO,
            "conteudo": {"type": "string", "description": "Conteudo COMPLETO do arquivo."},
        },
        "required": ["projeto_id", "arquivo", "conteudo"],
        "additionalProperties": False,
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        conteudo = str(kwargs.get("conteudo", ""))
        try:
            projeto = self._projeto(str(kwargs.get("projeto_id", "")))
            alvo = resolver_caminho(projeto, str(kwargs.get("arquivo", "")))
            conferir_tamanho(conteudo)
        except (ProjetoDesconhecido, ForaDaSuperficie) as exc:
            log.warning("dev.escrita_barrada", motivo=str(exc))
            return ToolResult.erro(str(exc))

        existia = alvo.is_file()
        try:
            alvo.parent.mkdir(parents=True, exist_ok=True)
            alvo.write_text(conteudo, encoding="utf-8")
        except OSError as exc:
            return ToolResult.erro(f"nao consegui escrever: {exc}")

        log.info("dev.escrito", projeto=projeto.id, arquivo=str(kwargs.get("arquivo")))
        return ToolResult(
            content=(
                f"{'Atualizei' if existia else 'Criei'} "
                f"{kwargs.get('arquivo')} em {projeto.nome}."
            ),
            metadata={
                # Caminho relativo, nunca a raiz: a trilha e permanente e nao
                # precisa guardar a topologia do disco.
                "projeto": projeto.id,
                "arquivo": str(kwargs.get("arquivo")),
                "bytes": len(conteudo.encode("utf-8")),
                "novo": not existia,
            },
        )


class DevTestarTool(_FerramentaDev):
    """Roda a suíte do projeto em contêiner isolado."""

    name = "dev_testar"
    risk = RiskLevel.ESCRITA
    description = (
        "Roda os testes do projeto num conteiner isolado, sem rede e com o "
        "codigo montado como somente-leitura. Testes verdes sao pre-requisito "
        "para publicar."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"projeto_id": _PROJETO_ID},
        "required": ["projeto_id"],
        "additionalProperties": False,
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            projeto = self._projeto(str(kwargs.get("projeto_id", "")))
        except ProjetoDesconhecido as exc:
            return ToolResult.erro(str(exc))

        resultado = await rodar_testes(projeto.raiz, projeto.testes, projeto.imagem)
        if not resultado.ok:
            return ToolResult.erro(
                f"Testes de {projeto.nome} nao passaram: {resultado.motivo}\n\n"
                f"{resultado.resumo()}"
            )
        return ToolResult(
            content=f"Testes de {projeto.nome} passaram.\n\n{resultado.resumo(600)}",
            metadata={"projeto": projeto.id, "codigo": resultado.codigo},
        )


class DevPublicarTool(_FerramentaDev):
    """Commit + push. Sem portão, com sandbox e auditoria."""

    name = "dev_publicar"
    risk = RiskLevel.EXTERNO
    description = (
        "Commita o que mudou no projeto e envia para o repositorio remoto - o "
        "que dispara o deploy. Exige testes verdes; se falharem, nao publica. "
        "Use so quando o usuario pedir para publicar."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "projeto_id": _PROJETO_ID,
            "mensagem": {
                "type": "string",
                "minLength": 8,
                "maxLength": 2000,
                "description": "Mensagem de commit: o que mudou e por que.",
            },
        },
        "required": ["projeto_id", "mensagem"],
        "additionalProperties": False,
    }

    def resumir(self, parametros: dict[str, Any]) -> str:
        alvo = str(parametros.get("projeto_id", "?"))
        return f'publicar em {alvo}: "{str(parametros.get("mensagem", ""))[:100]}"'

    async def execute(self, **kwargs: Any) -> ToolResult:
        mensagem = str(kwargs.get("mensagem", "")).strip()
        try:
            projeto = self._projeto(str(kwargs.get("projeto_id", "")))
        except ProjetoDesconhecido as exc:
            return ToolResult.erro(str(exc))

        if not await git_dev.e_repositorio(projeto.raiz):
            return ToolResult.erro(f"{projeto.nome} nao e um repositorio git.")

        # 1. Sandbox ANTES de qualquer coisa: sem Docker vivo nao ha publicacao,
        #    e descobrir isso depois de commitar deixaria o repositorio sujo.
        vivo, detalhe = await docker_vivo()
        if not vivo:
            return ToolResult.erro(
                f"Nao publiquei: a sandbox exige Docker rodando ({detalhe})."
            )
        testes = await rodar_testes(projeto.raiz, projeto.testes, projeto.imagem)
        if not testes.ok:
            return ToolResult.erro(
                f"Nao publiquei: os testes de {projeto.nome} nao passaram "
                f"({testes.motivo}).\n\n{testes.resumo(800)}"
            )

        try:
            saldo = await git_dev.mudancas(projeto.raiz)
            if saldo.total == 0:
                return ToolResult.erro(f"Nada mudou em {projeto.nome}: nao ha o que publicar.")

            antes = await git_dev.sha_atual(projeto.raiz)
            await git_dev.preparar_tudo(projeto.raiz)

            # 2. Limiar de delecao, lido do INDICE - e ele que vira commit.
            apagados = await git_dev.apagados_no_indice(projeto.raiz)
            teto = self._settings.dev_max_delecoes
            if len(apagados) > teto:
                # Desfaz o `add` para nao deixar o indice preparado com uma
                # remocao em massa esperando o proximo commit de alguem.
                await git_dev._git(projeto.raiz, "reset")
                log.warning("dev.delecao_em_massa", projeto=projeto.id, quantos=len(apagados))
                return ToolResult.erro(
                    f"Nao publiquei: o commit removeria {len(apagados)} arquivos, "
                    f"acima do teto de {teto}. Se for proposital, o usuario "
                    f"precisa aumentar OPTMUS_DEV_MAX_DELECOES."
                )

            depois = await git_dev.commitar(projeto.raiz, mensagem, autor="Optmus")
            await git_dev.enviar(projeto.raiz, projeto.branch)
        except git_dev.GitFalhou as exc:
            log.warning("dev.publicacao_falhou", projeto=projeto.id, erro=str(exc))
            return ToolResult.erro(f"Nao consegui publicar: {exc}")

        if self._store is not None:
            # Guardado DEPOIS do push confirmado: registrar antes deixaria o
            # reverter apontando para um deploy que nunca chegou ao remoto.
            await self._store.meta_set(
                CHAVE_DEPLOY.format(projeto=projeto.id), f"{antes}|{depois}"
            )

        log.info("dev.publicado", projeto=projeto.id, sha=depois[:8], branch=projeto.branch)
        return ToolResult(
            content=(
                f"Publiquei em {projeto.nome} ({projeto.branch}): {saldo.resumo()}. "
                f"Commit {depois[:8]}. O deploy comeca sozinho a partir do push."
            ),
            metadata={
                "projeto": projeto.id,
                "sha_antes": antes[:12],
                "sha_depois": depois[:12],
                "arquivos": saldo.total,
                "apagados": len(apagados),
            },
        )


class DevReverterTool(_FerramentaDev):
    """Desfaz o último deploy autônomo, criando um commit de reversão."""

    name = "dev_reverter"
    risk = RiskLevel.EXTERNO
    description = (
        "Desfaz a ultima publicacao feita pelo Optmus naquele projeto, criando "
        "um commit de reversao e enviando - o que dispara um novo deploy com o "
        "codigo anterior. Use quando algo publicado quebrou."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"projeto_id": _PROJETO_ID},
        "required": ["projeto_id"],
        "additionalProperties": False,
    }

    def resumir(self, parametros: dict[str, Any]) -> str:
        return f"desfazer a ultima publicacao em {parametros.get('projeto_id', '?')}"

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            projeto = self._projeto(str(kwargs.get("projeto_id", "")))
        except ProjetoDesconhecido as exc:
            return ToolResult.erro(str(exc))

        if self._store is None:
            return ToolResult.erro("sem memoria para saber qual foi a ultima publicacao")
        bruto = await self._store.meta_get(CHAVE_DEPLOY.format(projeto=projeto.id))
        if not bruto or "|" not in bruto:
            return ToolResult.erro(
                f"Nao ha publicacao do Optmus registrada em {projeto.nome} para desfazer."
            )
        _, sha = bruto.split("|", 1)

        try:
            novo = await git_dev.reverter(projeto.raiz, sha)
            await git_dev.enviar(projeto.raiz, projeto.branch)
        except git_dev.GitFalhou as exc:
            return ToolResult.erro(f"Nao consegui reverter: {exc}")

        # Some o registro: revertido uma vez, nao ha o que reverter de novo -
        # e um segundo revert do mesmo SHA falharia de um jeito confuso.
        await self._store.meta_set(CHAVE_DEPLOY.format(projeto=projeto.id), "")
        log.info("dev.revertido", projeto=projeto.id, de=sha[:8], para=novo[:8])
        return ToolResult(
            content=(
                f"Desfiz {sha[:8]} em {projeto.nome}. O deploy do codigo anterior "
                f"comeca sozinho."
            ),
            metadata={"projeto": projeto.id, "revertido": sha[:12], "commit": novo[:12]},
        )
