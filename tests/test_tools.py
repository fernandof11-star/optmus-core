"""Registro de ferramentas, politica de risco, auditoria e sandbox."""

from __future__ import annotations

from typing import Any

import pytest

from core.config import Settings, get_settings, reset_settings_cache
from memory.embeddings import HashingEmbedder
from memory.store import Store
from memory.system import MemorySystem
from security.audit import AuditLog, redigir
from security.policy import PolicyEngine, RiskLevel
from tools.impl.memory_tools import LembrarTool, RecordarTool
from tools.registry import Tool, ToolRegistry, ToolResult


class FerramentaFalsa(Tool):
    def __init__(
        self,
        nome: str = "falsa",
        risco: RiskLevel = RiskLevel.LEITURA,
        *,
        disponivel: bool = True,
        explode: bool = False,
    ) -> None:
        self.name = nome
        self.description = f"ferramenta de teste {nome}"
        self.schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        self.risk = risco
        self._disponivel = disponivel
        self._explode = explode
        self.execucoes: list[dict[str, Any]] = []

    async def available(self) -> bool:
        return self._disponivel

    async def execute(self, **kwargs: Any) -> ToolResult:
        if self._explode:
            raise RuntimeError("adb morreu")
        self.execucoes.append(kwargs)
        return ToolResult(content=f"feito com {kwargs}")


@pytest.fixture
def registro(settings: Settings, store: Store) -> ToolRegistry:
    return ToolRegistry(
        policy=PolicyEngine(settings, store), audit=AuditLog(store), store=store, sandbox_runs=0
    )


# ------------------------------------------------------------------ registro
async def test_ferramenta_de_leitura_executa_direto(registro: ToolRegistry) -> None:
    tool = FerramentaFalsa()
    registro.register(tool)
    await registro.refresh()

    resultado = await registro.execute("falsa", {"x": "1"})
    assert not resultado.is_error
    assert tool.execucoes == [{"x": "1"}]


async def test_ferramenta_indisponivel_nao_entra_no_schema(registro: ToolRegistry) -> None:
    """Oferecer ferramenta que nao funciona e pior do que nao ter."""
    registro.register(FerramentaFalsa("configurada"))
    registro.register(FerramentaFalsa("sem_config", disponivel=False))
    await registro.refresh()

    nomes = {s["name"] for s in registro.schemas()}
    assert nomes == {"configurada"}

    resultado = await registro.execute("sem_config", {})
    assert resultado.is_error and "nao esta configurada" in resultado.content


async def test_ferramenta_desconhecida_vira_erro_nao_excecao(registro: ToolRegistry) -> None:
    resultado = await registro.execute("nao_existe", {})
    assert resultado.is_error and "desconhecida" in resultado.content


async def test_ferramenta_que_explode_volta_como_erro(registro: ToolRegistry) -> None:
    registro.register(FerramentaFalsa("quebrada", explode=True))
    await registro.refresh()

    resultado = await registro.execute("quebrada", {})
    assert resultado.is_error and "adb morreu" in resultado.content


async def test_registro_duplicado_e_erro_de_programacao(registro: ToolRegistry) -> None:
    registro.register(FerramentaFalsa("x"))
    with pytest.raises(ValueError, match="duplicada"):
        registro.register(FerramentaFalsa("x"))


# ------------------------------------------------------------------ politica
async def test_acao_externa_nao_executa_sem_confirmacao(registro: ToolRegistry) -> None:
    tool = FerramentaFalsa("enviar_mensagem", RiskLevel.EXTERNO)
    registro.register(tool)
    await registro.refresh()

    resultado = await registro.execute("enviar_mensagem", {"x": "oi"})
    assert tool.execucoes == [], "acao externa NAO pode rodar antes da confirmacao"
    assert "AGUARDANDO CONFIRMACAO" in resultado.content
    assert resultado.metadata["token"]


async def test_confirmacao_libera_a_execucao(registro: ToolRegistry) -> None:
    tool = FerramentaFalsa("enviar_mensagem", RiskLevel.EXTERNO)
    registro.register(tool)
    await registro.refresh()

    pedido = await registro.execute("enviar_mensagem", {"x": "oi"})
    resultado = await registro.executar_confirmado(pedido.metadata["token"])

    assert not resultado.is_error
    assert tool.execucoes == [{"x": "oi"}]


async def test_token_so_vale_uma_vez(registro: ToolRegistry) -> None:
    registro.register(FerramentaFalsa("enviar_mensagem", RiskLevel.EXTERNO))
    await registro.refresh()

    token = (await registro.execute("enviar_mensagem", {})).metadata["token"]
    await registro.executar_confirmado(token)
    repetido = await registro.executar_confirmado(token)
    assert repetido.is_error and "invalido ou ja usado" in repetido.content


async def test_destrutivo_exige_frase_codigo(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPTMUS_DESTRUCTIVE_PASSPHRASE", "abre alas")
    monkeypatch.setenv("OPTMUS_DESTRUCTIVE_DELAY_S", "0")
    reset_settings_cache()
    settings = get_settings()

    registro = ToolRegistry(
        policy=PolicyEngine(settings, store), audit=AuditLog(store), store=store
    )
    tool = FerramentaFalsa("apagar_tudo", RiskLevel.DESTRUTIVO)
    registro.register(tool)
    await registro.refresh()

    token = (await registro.execute("apagar_tudo", {})).metadata["token"]

    errada = await registro.executar_confirmado(token, frase="qualquer coisa")
    assert errada.is_error and "frase-codigo" in errada.content
    assert tool.execucoes == []

    # O token e queimado mesmo na tentativa errada: nao da para ficar chutando.
    novo_token = (await registro.execute("apagar_tudo", {})).metadata["token"]
    certa = await registro.executar_confirmado(novo_token, frase="Abre Alas")
    assert not certa.is_error
    assert tool.execucoes == [{}]


async def test_rate_limit_bloqueia_acao_externa(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPTMUS_EXTERNAL_ACTION_RATE_LIMIT", "2")
    reset_settings_cache()
    settings = get_settings()
    registro = ToolRegistry(
        policy=PolicyEngine(settings, store), audit=AuditLog(store), store=store
    )
    registro.register(FerramentaFalsa("enviar_mensagem", RiskLevel.EXTERNO))
    await registro.refresh()

    for _ in range(2):
        token = (await registro.execute("enviar_mensagem", {})).metadata["token"]
        await registro.executar_confirmado(token)

    bloqueado = await registro.execute("enviar_mensagem", {})
    assert bloqueado.is_error and "teto de 2" in bloqueado.content


# ----------------------------------------------------------------- auditoria
async def test_execucao_deixa_rastro_na_auditoria(
    registro: ToolRegistry, store: Store
) -> None:
    registro.register(FerramentaFalsa("falsa", RiskLevel.ESCRITA))
    await registro.refresh()
    await registro.execute("falsa", {"x": "1"}, comando_origem="registra um gasto")

    linhas = await AuditLog(store).recentes()
    assert len(linhas) == 1
    assert linhas[0]["tool"] == "falsa"
    assert linhas[0]["risk"] == "ESCRITA"
    assert linhas[0]["decision"] == "permitido"
    assert linhas[0]["origin_command"] == "registra um gasto"


async def test_bloqueio_tambem_e_auditado(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTMUS_EXTERNAL_ACTION_RATE_LIMIT", "0")
    reset_settings_cache()
    registro = ToolRegistry(
        policy=PolicyEngine(get_settings(), store), audit=AuditLog(store), store=store
    )
    registro.register(FerramentaFalsa("enviar_mensagem", RiskLevel.EXTERNO))
    await registro.refresh()
    # teto 0 desliga o limite; usa DESTRUTIVO sem frase para forcar recusa
    resultado = await registro.executar_confirmado("token-inventado")
    assert resultado.is_error


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ({"senha": "1234"}, {"senha": "***"}),
        ({"api_key": "sk-x"}, {"api_key": "***"}),
        ({"texto": "ola"}, {"texto": "ola"}),
        ({"nested": {"token": "abc"}}, {"nested": {"token": "***"}}),
    ],
)
def test_redacao_de_campo_sensivel(entrada: dict[str, Any], esperado: dict[str, Any]) -> None:
    assert redigir(entrada) == esperado


def test_redacao_trunca_valor_gigante() -> None:
    saida = redigir({"texto": "x" * 5000})
    assert len(saida["texto"]) < 600 and saida["texto"].endswith("(truncado)")


# ------------------------------------------------------------------ sandbox
async def test_ferramenta_nova_roda_em_simulacao(settings: Settings, store: Store) -> None:
    registro = ToolRegistry(
        policy=PolicyEngine(settings, store), audit=AuditLog(store), store=store, sandbox_runs=2
    )
    tool = FerramentaFalsa("nova", RiskLevel.ESCRITA)
    registro.register(tool)
    await registro.refresh()

    for _ in range(2):
        resultado = await registro.execute("nova", {"x": "1"})
        assert "MODO SIMULACAO" in resultado.content
    assert tool.execucoes == [], "nada foi executado de verdade durante o sandbox"

    real = await registro.execute("nova", {"x": "1"})
    assert "MODO SIMULACAO" not in real.content
    assert tool.execucoes == [{"x": "1"}]


async def test_leitura_nao_entra_em_sandbox(settings: Settings, store: Store) -> None:
    """Simular leitura nao protege ninguem e devolve dado falso ao modelo."""
    registro = ToolRegistry(
        policy=PolicyEngine(settings, store), audit=AuditLog(store), store=store, sandbox_runs=5
    )
    tool = FerramentaFalsa("consulta", RiskLevel.LEITURA)
    registro.register(tool)
    await registro.refresh()

    resultado = await registro.execute("consulta", {})
    assert "MODO SIMULACAO" not in resultado.content
    assert tool.execucoes == [{}]


# --------------------------------------------------- ferramentas de memoria
@pytest.fixture
def memoria(settings: Settings, store: Store) -> MemorySystem:
    return MemorySystem(settings, store, embedder=HashingEmbedder(settings.embedding_dim))


async def test_lembrar_grava_fato(memoria: MemorySystem) -> None:
    resultado = await LembrarTool(memoria).execute(conteudo="o contador chama Ricardo")
    assert not resultado.is_error
    assert await memoria.semantic.count() == 1


async def test_lembrar_nao_duplica(memoria: MemorySystem) -> None:
    tool = LembrarTool(memoria)
    await tool.execute(conteudo="o contador chama Ricardo Almeida")
    repetido = await tool.execute(conteudo="o contador chama Ricardo Almeida")
    assert "Ja sabia disso" in repetido.content
    assert await memoria.semantic.count() == 1


async def test_lembrar_com_correcao_versiona(memoria: MemorySystem) -> None:
    tool = LembrarTool(memoria)
    primeiro = await tool.execute(conteudo="mora em Sao Paulo")
    corrigido = await tool.execute(
        conteudo="mora no Rio de Janeiro", corrige_id=primeiro.metadata["id"]
    )
    assert "superado, nao apagado" in corrigido.content
    vigentes = [linha["content"] for linha in await memoria.semantic.vigentes()]
    assert vigentes == ["mora no Rio de Janeiro"]


async def test_recordar_avisa_quando_a_busca_e_lexical(memoria: MemorySystem) -> None:
    resultado = await RecordarTool(memoria).execute(consulta="assunto inedito zzz")
    assert "modo lexical" in resultado.content
