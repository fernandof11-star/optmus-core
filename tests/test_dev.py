"""F10: onde o Optmus pode escrever, e o que ele nunca alcanca.

Nenhum teste aqui e sobre escrever com sucesso. Todos sao sobre a fronteira -
porque no modo dev nao ha humano no caminho do deploy, e o que sobra como
defesa e o que o codigo estruturalmente nao consegue fazer.

Os dois que carregam mais peso:
:func:`test_symlink_para_fora_nao_engana_a_contencao` e
:func:`test_git_e_intocavel` - o primeiro porque a fuga mais silenciosa e por
link, o segundo porque `.git/hooks` e execucao de codigo que passa por cima de
sandbox, portao e auditoria de uma vez.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from core.config import Settings, get_settings, reset_settings_cache
from integrations.projetos import (
    BYTES_MAXIMOS,
    NEGADOS,
    ForaDaSuperficie,
    ListaInvalida,
    ProjetoDesconhecido,
    carregar,
    conferir_tamanho,
    negado,
    resolver_caminho,
    resolver_projeto,
)
from integrations.sandbox import MEMORIA, rodar_testes
from security.policy import PolicyEngine, RiskLevel


@pytest.fixture
def area(tmp_path: Path) -> dict[str, Path]:
    raiz = tmp_path / "meu-projeto"
    (raiz / "src").mkdir(parents=True)
    (raiz / ".git" / "hooks").mkdir(parents=True)
    (raiz / "src" / "app.py").write_text("print('oi')", encoding="utf-8")
    (raiz / ".env").write_text("SEGREDO=1", encoding="utf-8")

    fora = tmp_path / "fora"
    fora.mkdir()
    (fora / "segredo.txt").write_text("nao deveria ser alcancavel", encoding="utf-8")

    registro = tmp_path / "projetos.json"
    registro.write_text(
        json.dumps({
            "meu": {"nome": "Meu Projeto", "raiz": str(raiz), "testes": "pytest -q"}
        }),
        encoding="utf-8",
    )
    return {"raiz": raiz, "fora": fora, "registro": registro, "tmp": tmp_path}


def _projeto(area: dict[str, Path]):
    return resolver_projeto(carregar(area["registro"]), "meu")


# ------------------------------------------------------------- contencao
@pytest.mark.parametrize(
    "fuga",
    [
        "../fora/segredo.txt",
        "../../fora/segredo.txt",
        "src/../../fora/segredo.txt",
        "src/../../../Windows/System32/drivers/etc/hosts",
        "./../fora/x.txt",
    ],
)
def test_caminho_relativo_nao_escapa_da_raiz(area: dict[str, Path], fuga: str) -> None:
    """`..` e o jeito obvio de sair, e por isso o primeiro a ser testado."""
    with pytest.raises(ForaDaSuperficie, match="sai da raiz"):
        resolver_caminho(_projeto(area), fuga)


@pytest.mark.parametrize(
    "absoluto",
    [r"C:\Windows\System32\cmd.exe", "C:/Users/x/.ssh/id_rsa", "/etc/passwd"],
)
def test_caminho_absoluto_e_recusado_na_entrada(
    area: dict[str, Path], absoluto: str
) -> None:
    """Recusa na entrada em vez de tentar interpretar.

    Aceitar `C:/...` e depois conferir contencao criaria um empate entre "o que
    o modelo pediu" e "o que o disco tem". Aqui so existe uma forma de pedir.
    """
    with pytest.raises(ForaDaSuperficie, match="absoluto"):
        resolver_caminho(_projeto(area), absoluto)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink exige privilegio no Windows")
def test_symlink_para_fora_nao_engana_a_contencao(area: dict[str, Path]) -> None:
    """A fuga mais silenciosa: um link dentro do projeto apontando para fora.

    Conferir o caminho CRU deixaria passar - "atalho/segredo.txt" parece
    interno. A checagem acontece depois de `resolve()`, que segue o link e
    revela para onde ele vai de verdade.
    """
    (area["raiz"] / "atalho").symlink_to(area["fora"], target_is_directory=True)

    with pytest.raises(ForaDaSuperficie):
        resolver_caminho(_projeto(area), "atalho/segredo.txt")


def test_caminho_dentro_da_raiz_passa(area: dict[str, Path]) -> None:
    alvo = resolver_caminho(_projeto(area), "src/novo.py")
    assert alvo.is_relative_to(area["raiz"])
    assert alvo.name == "novo.py"


# ----------------------------------------------------------- zonas negadas
@pytest.mark.parametrize(
    "proibido",
    [
        ".git/hooks/pre-commit",
        ".git/config",
        ".env",
        ".env.local",
        "data/contatos.json",
        ".venv/lib/x.py",
        "node_modules/pacote/index.js",
    ],
)
def test_git_e_intocavel(area: dict[str, Path], proibido: str) -> None:
    """`.git/hooks/pre-commit` e execucao de codigo no proximo commit.

    E o proximo commit e justamente o que o modo dev roda. Escrever ali seria
    furar sandbox, portao e auditoria de uma vez, sem nunca chamar uma
    ferramenta de deploy. `.env` e `data/` seguem junto: segredos e o estado
    vivo do Core.
    """
    with pytest.raises(ForaDaSuperficie, match="protegida"):
        resolver_caminho(_projeto(area), proibido)


def test_zona_negada_e_reconferida_depois_de_resolver(area: dict[str, Path]) -> None:
    """"docs/../.git/hooks/x" so revela o `.git` DEPOIS de resolvido.

    Sem a segunda conferencia, a primeira olharia "docs/..." e deixaria passar.
    """
    with pytest.raises(ForaDaSuperficie, match="protegida"):
        resolver_caminho(_projeto(area), "src/../.git/hooks/pre-commit")


def test_negado_ignora_caixa_e_barra_invertida() -> None:
    assert negado(".GIT/hooks/x") is True
    assert negado(".git\\config") is True
    assert negado("src/app.py") is False


def test_teto_de_tamanho_por_arquivo() -> None:
    """Um arquivo gigante gerado por engano entope o repositorio e o diff."""
    conferir_tamanho("x" * 100)
    with pytest.raises(ForaDaSuperficie, match="teto"):
        conferir_tamanho("x" * (BYTES_MAXIMOS + 1))


# ------------------------------------------------------------- registro
def test_projeto_fora_da_lista_e_recusado(area: dict[str, Path]) -> None:
    with pytest.raises(ProjetoDesconhecido, match="nao e um projeto registrado"):
        resolver_projeto(carregar(area["registro"]), "montlux")


def test_raiz_inexistente_derruba_o_registro(tmp_path: Path) -> None:
    """Erro alto: raiz ausente viraria "fora da superficie" em toda escrita, e
    voce procuraria o defeito no caminho relativo."""
    ruim = tmp_path / "p.json"
    ruim.write_text(json.dumps({"x": {"raiz": str(tmp_path / "nao-existe")}}), encoding="utf-8")

    with pytest.raises(ListaInvalida, match="nao existe"):
        carregar(ruim)


def test_a_raiz_nao_sai_para_o_modelo(area: dict[str, Path]) -> None:
    visto = _projeto(area).visivel()
    assert set(visto) == {"id", "nome", "branch"}
    assert str(area["raiz"]) not in json.dumps(visto)


def test_lista_ausente_e_lista_vazia(tmp_path: Path) -> None:
    assert carregar(tmp_path / "nao-existe.json") == {}


# --------------------------------------------------------------- sandbox
async def test_sem_docker_nao_ha_teste_verde(
    monkeypatch: pytest.MonkeyPatch, area: dict[str, Path]
) -> None:
    """Docker parado nao vira "testes passaram por omissao".

    Este e o caminho pelo qual a exigencia de sandbox poderia evaporar sem
    ninguem notar: se a ausencia de Docker devolvesse sucesso, publicar
    passaria a nao exigir teste nenhum.
    """
    async def morto(timeout: float = 20.0) -> tuple[bool, str]:
        return False, "daemon do docker parado (abra o Docker Desktop)"

    monkeypatch.setattr("integrations.sandbox.docker_vivo", morto)
    r = await rodar_testes(area["raiz"], "pytest -q", "python:3.12-slim")

    assert r.ok is False
    assert "docker" in r.motivo


async def test_projeto_sem_comando_de_teste_nao_publica(area: dict[str, Path]) -> None:
    """Sem comando declarado nao ha como ter verde - e sem verde nao ha push."""
    r = await rodar_testes(area["raiz"], "   ", "python:3.12-slim")
    assert r.ok is False
    assert "sem comando de teste" in r.motivo


async def test_o_conteiner_e_isolado_e_somente_leitura(
    monkeypatch: pytest.MonkeyPatch, area: dict[str, Path]
) -> None:
    """As tres propriedades que fazem disto uma sandbox, e nao um subprocesso.

    Sem `--network=none` o teste exfiltra. Sem `:ro` o teste conserta a si
    mesmo para ficar verde. Sem `--rm` sobra estado entre execucoes.
    """
    vistos: list[tuple[str, ...]] = []

    async def espiao(*args: str, timeout: float) -> tuple[int, str]:
        vistos.append(args)
        return 0, "ok"

    async def vivo(timeout: float = 20.0) -> tuple[bool, str]:
        return True, "29.6.2"

    monkeypatch.setattr("integrations.sandbox.docker_vivo", vivo)
    monkeypatch.setattr("integrations.sandbox._rodar", espiao)

    await rodar_testes(area["raiz"], "pytest -q", "python:3.12-slim")

    args = vistos[0]
    assert "--network=none" in args
    assert "--rm" in args
    assert f"{area['raiz']}:/trabalho:ro" in args
    assert f"--memory={MEMORIA}" in args


# ------------------------------------------------------- dispensa do portao
async def test_a_dispensa_vale_so_para_o_deploy(settings: Settings, store) -> None:
    """A trava revogada foi a do deploy - nenhuma outra.

    Se a dispensa valesse por risco em vez de por nome de ferramenta, ela
    cobriria camera, WhatsApp e Telegram junto, e o portao inteiro cairia com
    uma linha de configuracao.
    """
    motor = PolicyEngine(settings, store)

    liberado = await motor.avaliar(
        ferramenta="dev_publicar", risco=RiskLevel.EXTERNO, parametros={}, resumo="publicar"
    )
    assert liberado.permitido is True
    assert liberado.exige_confirmacao is False

    for outra in ("olhar", "whatsapp_enviar", "telegram_enviar"):
        decisao = await motor.avaliar(
            ferramenta=outra, risco=RiskLevel.EXTERNO, parametros={}, resumo="x"
        )
        assert decisao.exige_confirmacao is True, outra


async def test_destrutivo_continua_com_portao(settings: Settings, store) -> None:
    """Apagar historico nao e deploy. O que foi revogado foi o portao do
    deploy, e so ele."""
    motor = PolicyEngine(settings, store)
    decisao = await motor.avaliar(
        ferramenta="dev_publicar", risco=RiskLevel.DESTRUTIVO, parametros={}, resumo="force push"
    )

    assert decisao.exige_confirmacao is True
    assert decisao.exige_frase_codigo is True


def test_a_lista_de_dispensa_nao_esconde_ferramenta_perigosa(settings: Settings) -> None:
    """Guarda de configuracao: a lista e curta e visivel de proposito."""
    dispensadas = set(settings.dev_sem_portao)

    assert dispensadas == {"dev_publicar"}
    for perigosa in ("olhar", "whatsapp_enviar", "telegram_enviar", "pc_abrir"):
        assert perigosa not in dispensadas


def test_as_zonas_negadas_cobrem_o_essencial() -> None:
    for essencial in (".git/", ".env", "data/"):
        assert essencial in NEGADOS


async def test_modo_dev_desligado_por_padrao() -> None:
    reset_settings_cache()
    assert get_settings().dev_enabled is False
