"""Registro de ferramentas.

Contrato unico para tudo que o Optmus sabe fazer no mundo. Uma ferramenta
declara nome, descricao, schema e **risco**; o registro cuida do resto:
politica, auditoria, sandbox e traducao de erro em texto que o modelo entende.

Duas coisas que o registro faz e que sao faceis de esquecer:

- **A descricao e a interface.** O modelo escolhe a ferramenta lendo o campo
  ``description``, nao o codigo. Descricao vaga produz ferramenta chamada na
  hora errada, e nenhum prompt conserta isso depois.
- **Erro de ferramenta e resposta, nao excecao.** Uma ferramenta que explode
  volta como ``tool_result`` com ``is_error``, para o modelo poder se corrigir.
  Excecao subindo mata o turno e o usuario ouve silencio.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from core.llm import Imagem
from core.logging import get_logger
from memory.store import Store
from security.audit import AuditLog
from security.policy import PolicyEngine, RiskLevel

log = get_logger("tools.registry")

CHAVE_SANDBOX = "sandbox:{nome}"


@dataclass(slots=True)
class ToolResult:
    """O que volta para o modelo.

    ``content`` e sempre texto - e o que a auditoria registra e o que aparece
    no log. Imagem vai separada em ``imagens``, e **nunca** embutida no texto:
    misturar as duas coisas faria uma foto inteira em base64 escorrer para a
    trilha de auditoria pela primeira ferramenta que esquecesse a distincao.
    """

    content: str
    is_error: bool = False
    dados: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    imagens: list[Imagem] = field(default_factory=list)

    @classmethod
    def erro(cls, mensagem: str, **metadata: Any) -> ToolResult:
        return cls(content=mensagem, is_error=True, metadata=metadata)


@dataclass(slots=True)
class ResultadoConfirmado(ToolResult):
    """O que uma acao confirmada produziu, **mais** de quem era a pendencia.

    Subclasse de ``ToolResult`` de proposito: quem so quer o texto continua
    lendo ``.content`` sem saber que isto existe. Os campos extras servem a um
    unico chamador - o que devolve o resultado ao agente - e ele precisa saber
    tres coisas que so a pendencia sabia: qual ferramenta rodou, de onde o
    pedido veio (para decidir se a resposta e falada) e a qual conversa ele
    pertence.
    """

    ferramenta: str = ""
    origem: str = ""
    resumo: str = ""
    correlation_id: str | None = None


class Tool(ABC):
    """Uma capacidade do Optmus.

    Nome, descricao e schema sao atributos de classe: a definicao da ferramenta
    e estatica e fica junto do codigo que a implementa.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    schema: ClassVar[dict[str, Any]]
    risk: ClassVar[RiskLevel] = RiskLevel.LEITURA
    requires_confirmation: ClassVar[bool] = False

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult: ...

    async def available(self) -> bool:
        """Ferramenta sem dependencia satisfeita nao entra no schema do modelo.

        Oferecer uma ferramenta que nao pode funcionar e pior do que nao ter:
        o modelo tenta, falha e improvisa.
        """
        return True

    def resumir(self, parametros: dict[str, Any]) -> str:
        """Frase lida em voz alta antes de confirmar uma acao arriscada."""
        return f"{self.name} com {parametros}"

    def to_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.schema,
        }


class ToolRegistry:
    """Reune as ferramentas e aplica politica, auditoria e sandbox."""

    def __init__(
        self,
        *,
        policy: PolicyEngine,
        audit: AuditLog,
        store: Store,
        sandbox_runs: int = 0,
    ) -> None:
        self._policy = policy
        self._audit = audit
        self._store = store
        self._sandbox_runs = sandbox_runs
        self._tools: dict[str, Tool] = {}
        self._disponiveis: dict[str, bool] = {}

    @property
    def policy(self) -> PolicyEngine:
        return self._policy

    @property
    def audit(self) -> AuditLog:
        """Trilha de auditoria, para quem precisa registrar fora da execucao.

        A recusa de uma acao pendente acontece sem executar ferramenta nenhuma,
        entao nao passa pelo caminho que audita sozinho - e uma negativa e
        justamente o registro que mais falta quando alguem pergunta depois por
        que algo nao aconteceu.
        """
        return self._audit

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"ferramenta duplicada: {tool.name}")
        self._tools[tool.name] = tool
        log.debug("ferramenta.registrada", nome=tool.name, risco=tool.risk.value)

    async def refresh(self) -> None:
        """Reavalia quais ferramentas tem dependencia satisfeita."""
        for nome, tool in self._tools.items():
            try:
                self._disponiveis[nome] = await tool.available()
            except Exception as exc:  # noqa: BLE001
                log.warning("ferramenta.checagem_falhou", nome=nome, erro=str(exc))
                self._disponiveis[nome] = False

    def schemas(self) -> list[dict[str, Any]]:
        """Schemas das ferramentas utilizaveis - o que vai para o modelo."""
        return [
            tool.to_schema()
            for nome, tool in self._tools.items()
            if self._disponiveis.get(nome, True)
        ]

    def get(self, nome: str) -> Tool | None:
        return self._tools.get(nome)

    def listar(self) -> list[dict[str, Any]]:
        return [
            {
                "nome": tool.name,
                "risco": tool.risk.value,
                "disponivel": self._disponiveis.get(nome, True),
                "exige_confirmacao": tool.risk.ordem >= RiskLevel.EXTERNO.ordem,
                "descricao": tool.description.split("\n")[0][:160],
            }
            for nome, tool in sorted(self._tools.items())
        ]

    # ------------------------------------------------------------ execucao
    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        correlation_id: str | None = None,
        comando_origem: str | None = None,
        ignorar_politica: bool = False,
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.erro(f"ferramenta desconhecida: {name}")

        if not self._disponiveis.get(name, True):
            return ToolResult.erro(
                f"{name} nao esta configurada neste ambiente e nao pode ser usada agora."
            )

        if not ignorar_politica:
            decisao = await self._policy.avaliar(
                ferramenta=name,
                risco=tool.risk,
                parametros=arguments,
                resumo=tool.resumir(arguments),
                correlation_id=correlation_id,
            )
            if not decisao.permitido:
                await self._audit.registrar(
                    ferramenta=name,
                    risco=tool.risk,
                    decisao="negado",
                    parametros=arguments,
                    resultado=decisao.motivo,
                    correlation_id=correlation_id,
                    comando_origem=comando_origem,
                )
                return ToolResult.erro(f"Bloqueado: {decisao.motivo}")

            if decisao.exige_confirmacao:
                # Nao executa. Devolve ao modelo o que dizer ao usuario - a
                # confirmacao e do humano, nao do LLM.
                return ToolResult(
                    content=(
                        f"AGUARDANDO CONFIRMACAO. Leia isto ao usuario e espere "
                        f'resposta: "{tool.resumir(arguments)}". '
                        f"Nao repita a chamada; a confirmacao chega por fora."
                    ),
                    metadata={"token": decisao.token, "risco": tool.risk.value},
                )

        return await self._executar_de_fato(
            tool, arguments, correlation_id=correlation_id, comando_origem=comando_origem
        )

    async def executar_confirmado(
        self,
        token: str,
        *,
        frase: str | None = None,
        comando_origem: str | None = None,
        dispositivo: str | None = None,
    ) -> ResultadoConfirmado:
        """Executa uma acao que estava pendente de confirmacao humana.

        ``dispositivo`` e a identidade JA VERIFICADA de quem confirma - a prova
        criptografica foi conferida antes, na borda HTTP. Aqui ele serve para a
        politica decidir se esse dispositivo tem direito a ESTA pendencia.
        """
        try:
            pendente = self._policy.confirmar(token, frase=frase, dispositivo=dispositivo)
        except PermissionError as exc:
            log.warning("politica.confirmacao_recusada", token=token, motivo=str(exc))
            return ResultadoConfirmado(content=str(exc), is_error=True)

        tool = self._tools[pendente.ferramenta]
        if pendente.risco is RiskLevel.DESTRUTIVO and self._policy.delay_destrutivo_s:
            # Janela cancelavel: o "espera, nao" chega depois do "pode".
            await asyncio.sleep(self._policy.delay_destrutivo_s)

        await self._audit.registrar(
            ferramenta=tool.name,
            risco=tool.risk,
            decisao="confirmado",
            # Quem pediu E quem autorizou, os dois na mesma linha: "confirmado"
            # sem dizer por qual dispositivo e o registro que nao responde a
            # pergunta que se faz depois de um incidente.
            parametros={**pendente.parametros, "_origem": pendente.origem},
            correlation_id=pendente.correlation_id,
            comando_origem=comando_origem or dispositivo,
        )
        resultado = await self._executar_de_fato(
            tool,
            pendente.parametros,
            correlation_id=pendente.correlation_id,
            comando_origem=comando_origem,
            ja_auditado=True,
        )
        return ResultadoConfirmado(
            content=resultado.content,
            is_error=resultado.is_error,
            dados=resultado.dados,
            metadata=resultado.metadata,
            imagens=resultado.imagens,
            ferramenta=tool.name,
            origem=pendente.origem,
            resumo=pendente.resumo,
            correlation_id=pendente.correlation_id,
        )

    async def _executar_de_fato(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        *,
        correlation_id: str | None,
        comando_origem: str | None,
        ja_auditado: bool = False,
    ) -> ToolResult:
        simulacao = await self._em_sandbox(tool)
        if simulacao:
            resultado = ToolResult(
                content=(
                    f"MODO SIMULACAO: {tool.name} ainda esta em periodo de teste e "
                    f"NAO foi executada de verdade. Diga isso ao usuario. "
                    f"Chamada que teria sido feita: {tool.resumir(arguments)}"
                ),
                metadata={"dry_run": True},
            )
        else:
            try:
                resultado = await tool.execute(**arguments)
            except TypeError as exc:
                resultado = ToolResult.erro(f"parametros invalidos para {tool.name}: {exc}")
            except Exception as exc:
                log.error(
                    "ferramenta.falhou",
                    nome=tool.name,
                    erro=f"{type(exc).__name__}: {exc}",
                    exc_info=True,
                )
                resultado = ToolResult.erro(f"{tool.name} falhou: {type(exc).__name__}: {exc}")

        if not ja_auditado:
            await self._audit.registrar(
                ferramenta=tool.name,
                risco=tool.risk,
                decisao="permitido",
                parametros=arguments,
                resultado=resultado.content,
                correlation_id=correlation_id,
                comando_origem=comando_origem,
            )
        return resultado

    async def _em_sandbox(self, tool: Tool) -> bool:
        """Ferramenta nova roda em dry-run nas primeiras N execucoes.

        Vale so para o que escreve no mundo: simular uma leitura nao protege
        ninguem e so devolve dado falso ao modelo.
        """
        if self._sandbox_runs <= 0 or tool.risk is RiskLevel.LEITURA:
            return False
        chave = CHAVE_SANDBOX.format(nome=tool.name)
        atual = int(await self._store.meta_get(chave) or 0)
        if atual >= self._sandbox_runs:
            return False
        await self._store.meta_set(chave, str(atual + 1))
        log.warning(
            "ferramenta.sandbox",
            nome=tool.name,
            execucao=atual + 1,
            de=self._sandbox_runs,
            impacto="chamada simulada, nao executada",
        )
        return True

    async def liberar_do_sandbox(self, nome: str) -> None:
        """Encerra o periodo de teste de uma ferramenta."""
        await self._store.meta_set(CHAVE_SANDBOX.format(nome=nome), str(self._sandbox_runs))
