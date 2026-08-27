"""F10: as ferramentas, contra um repositorio git de verdade.

O remoto e um bare repo em ``tmp_path``, entao ``push`` acontece de verdade e
nada sai da maquina. Isso importa: testar publicacao com o git inteiro mockado
provaria que os mocks conversam entre si, e nao que o commit sai correto.

O teste que carrega mais peso e
:func:`test_delecao_em_massa_e_barrada_e_o_indice_fica_limpo` - o unico freio
estrutural que sobra quando nao ha humano no caminho do deploy.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core.config import get_settings, reset_settings_cache
from integrations.sandbox import Resultado
from tools.impl.dev import (
    DevEscreverTool,
    DevLerTool,
    DevListarTool,
    DevPublicarTool,
    DevReverterTool,
)


def _git(raiz: Path, *args: str) -> str:
    saida = subprocess.run(
        ["git", "-C", str(raiz), *args], check=True, capture_output=True, text=True
    )
    return saida.stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    remoto = tmp_path / "remoto.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remoto)], check=True)

    raiz = tmp_path / "proj"
    (raiz / "src").mkdir(parents=True)
    for i in range(12):
        (raiz / "src" / f"m{i}.py").write_text(f"x = {i}\n", encoding="utf-8")

    _git(raiz, "init", "-q", "-b", "main")
    _git(raiz, "config", "user.email", "t@t")
    _git(raiz, "config", "user.name", "teste")
    _git(raiz, "add", "-A")
    _git(raiz, "commit", "-q", "-m", "inicial")
    _git(raiz, "remote", "add", "origin", str(remoto))
    _git(raiz, "push", "-q", "origin", "main")

    registro = tmp_path / "projetos.json"
    registro.write_text(
        json.dumps({
            "p": {"nome": "Projeto", "raiz": str(raiz), "testes": "pytest -q", "branch": "main"}
        }),
        encoding="utf-8",
    )

    for marca in ("PORT", "RAILWAY_ENVIRONMENT", "RENDER", "DYNO"):
        monkeypatch.delenv(marca, raising=False)
    monkeypatch.setenv("OPTMUS_DEV_ENABLED", "true")
    monkeypatch.setenv("OPTMUS_DEV_PROJECTS_PATH", str(registro))
    reset_settings_cache()
    return {"raiz": raiz, "remoto": remoto, "registro": registro}


def _sandbox_verde(monkeypatch: pytest.MonkeyPatch) -> None:
    """Docker vivo e testes passando. O Docker real e exercitado noutro lugar."""

    async def vivo(timeout: float = 20.0) -> tuple[bool, str]:
        return True, "29.6.2"

    async def passou(*a: object, **k: object) -> Resultado:
        return Resultado(True, 0, "3 passed")

    monkeypatch.setattr("tools.impl.dev.docker_vivo", vivo)
    monkeypatch.setattr("tools.impl.dev.rodar_testes", passou)


def _commits(raiz: Path) -> int:
    return len([x for x in _git(raiz, "log", "--oneline").splitlines() if x.strip()])


# -------------------------------------------------- a sandbox e pre-requisito
async def test_sem_docker_nao_publica(
    monkeypatch: pytest.MonkeyPatch, repo: dict[str, Path], store: object
) -> None:
    """E a checagem vem ANTES de commitar.

    Descobrir que a sandbox esta fora depois do commit deixaria o repositorio
    sujo, com uma mudanca preparada que ninguem pediu para guardar.
    """
    async def morto(timeout: float = 20.0) -> tuple[bool, str]:
        return False, "daemon do docker parado"

    monkeypatch.setattr("tools.impl.dev.docker_vivo", morto)
    (repo["raiz"] / "src" / "novo.py").write_text("y = 1\n", encoding="utf-8")

    r = await DevPublicarTool(get_settings(), store).execute(  # type: ignore[arg-type]
        projeto_id="p", mensagem="mudanca qualquer"
    )

    assert r.is_error
    # Assercao ESPECIFICA da checagem antecipada, e nao um "Docker" solto: o
    # daemon e conferido duas vezes (aqui e dentro de rodar_testes), entao
    # procurar so a palavra passava mesmo com a checagem antecipada removida -
    # a segunda barrava e o teste nao via a diferenca.
    assert "a sandbox exige Docker rodando" in r.content
    assert "testes" not in r.content, "quem barrou foi a checagem antecipada"
    assert _commits(repo["raiz"]) == 1, "nao commitou nada"


async def test_teste_vermelho_nao_publica(
    monkeypatch: pytest.MonkeyPatch, repo: dict[str, Path], store: object
) -> None:
    async def vivo(timeout: float = 20.0) -> tuple[bool, str]:
        return True, "29.6.2"

    async def falhou(*a: object, **k: object) -> Resultado:
        return Resultado(False, 1, "1 failed", "testes falharam (codigo 1)")

    monkeypatch.setattr("tools.impl.dev.docker_vivo", vivo)
    monkeypatch.setattr("tools.impl.dev.rodar_testes", falhou)
    (repo["raiz"] / "src" / "novo.py").write_text("y = 1\n", encoding="utf-8")

    r = await DevPublicarTool(get_settings(), store).execute(  # type: ignore[arg-type]
        projeto_id="p", mensagem="mudanca qualquer"
    )

    assert r.is_error
    assert "nao passaram" in r.content
    assert _commits(repo["raiz"]) == 1


# ------------------------------------------------- o freio contra apagar tudo
async def test_delecao_em_massa_e_barrada_e_o_indice_fica_limpo(
    monkeypatch: pytest.MonkeyPatch, repo: dict[str, Path], store: object
) -> None:
    """O unico freio estrutural que sobra sem humano no caminho.

    Nao depende de o codigo gerado ser bom nem de alguem revisar: e aritmetica
    sobre o indice. E desfaz o `add` ao recusar - deixar uma remocao em massa
    preparada seria pior que a propria remocao, porque o proximo commit de
    QUALQUER UM a levaria junto sem ninguem perceber.
    """
    _sandbox_verde(monkeypatch)
    for i in range(12):
        (repo["raiz"] / "src" / f"m{i}.py").unlink()

    r = await DevPublicarTool(get_settings(), store).execute(  # type: ignore[arg-type]
        projeto_id="p", mensagem="limpeza geral"
    )

    assert r.is_error
    assert "removeria 12 arquivos" in r.content
    assert _git(repo["raiz"], "diff", "--cached", "--name-only").strip() == ""
    assert _commits(repo["raiz"]) == 1


async def test_delecao_dentro_do_teto_passa(
    monkeypatch: pytest.MonkeyPatch, repo: dict[str, Path], store: object
) -> None:
    """O teto barra remocao em massa, nao faxina normal."""
    _sandbox_verde(monkeypatch)
    for i in range(3):
        (repo["raiz"] / "src" / f"m{i}.py").unlink()

    r = await DevPublicarTool(get_settings(), store).execute(  # type: ignore[arg-type]
        projeto_id="p", mensagem="remove modulos obsoletos"
    )

    assert not r.is_error
    assert r.metadata["apagados"] == 3


# ------------------------------------------------------ publicar e reverter
async def test_publica_e_registra_para_reverter(
    monkeypatch: pytest.MonkeyPatch, repo: dict[str, Path], store: object
) -> None:
    _sandbox_verde(monkeypatch)
    (repo["raiz"] / "src" / "novo.py").write_text("y = 1\n", encoding="utf-8")

    r = await DevPublicarTool(get_settings(), store).execute(  # type: ignore[arg-type]
        projeto_id="p", mensagem="adiciona modulo novo"
    )

    assert not r.is_error
    assert _commits(repo["raiz"]) == 2
    # Chegou ao remoto: sem isto, "publicado" seria so um commit local.
    assert "novo.py" in _git(repo["remoto"], "ls-tree", "-r", "--name-only", "main")
    assert await store.meta_get("dev:ultimo_deploy:p")  # type: ignore[attr-defined]


async def test_reverter_desfaz_sem_reescrever_historico(
    monkeypatch: pytest.MonkeyPatch, repo: dict[str, Path], store: object
) -> None:
    """`revert` e nao `reset`: adiciona commit em vez de apagar.

    Reescrever historico exigiria force-push, que e DESTRUTIVO e continua com
    portao. Desfazer nao pode depender de uma acao mais perigosa que o erro.
    """
    _sandbox_verde(monkeypatch)
    (repo["raiz"] / "src" / "novo.py").write_text("y = 1\n", encoding="utf-8")
    await DevPublicarTool(get_settings(), store).execute(  # type: ignore[arg-type]
        projeto_id="p", mensagem="adiciona modulo novo"
    )

    r = await DevReverterTool(get_settings(), store).execute(projeto_id="p")  # type: ignore[arg-type]

    assert not r.is_error
    assert not (repo["raiz"] / "src" / "novo.py").exists()
    assert _commits(repo["raiz"]) == 3, "revert ADICIONA commit"
    assert "novo.py" not in _git(repo["remoto"], "ls-tree", "-r", "--name-only", "main")


async def test_reverter_duas_vezes_nao_confunde(
    monkeypatch: pytest.MonkeyPatch, repo: dict[str, Path], store: object
) -> None:
    _sandbox_verde(monkeypatch)
    (repo["raiz"] / "src" / "novo.py").write_text("y = 1\n", encoding="utf-8")
    await DevPublicarTool(get_settings(), store).execute(  # type: ignore[arg-type]
        projeto_id="p", mensagem="adiciona modulo novo"
    )
    await DevReverterTool(get_settings(), store).execute(projeto_id="p")  # type: ignore[arg-type]

    segunda = await DevReverterTool(get_settings(), store).execute(projeto_id="p")  # type: ignore[arg-type]

    assert segunda.is_error
    assert "para desfazer" in segunda.content


async def test_reverter_sem_publicacao_registrada(
    repo: dict[str, Path], store: object
) -> None:
    r = await DevReverterTool(get_settings(), store).execute(projeto_id="p")  # type: ignore[arg-type]
    assert r.is_error
    assert "Nao ha publicacao" in r.content


# ------------------------------------------------------ injecao de argumento
async def test_mensagem_de_commit_nao_vira_opcao_do_git(
    monkeypatch: pytest.MonkeyPatch, repo: dict[str, Path], store: object
) -> None:
    """Mensagem que parece comando continua sendo TEXTO.

    Ela vai como argv de `-m`, e git trata o proximo argumento como valor
    literal. Montada numa string de shell, `; rm -rf /` seria um comando e
    `--force` seria uma opcao. Neste modulo nao existe string de comando.
    """
    _sandbox_verde(monkeypatch)
    (repo["raiz"] / "src" / "novo.py").write_text("y = 1\n", encoding="utf-8")
    perigosa = "--force ; rm -rf / && echo invadido"

    r = await DevPublicarTool(get_settings(), store).execute(  # type: ignore[arg-type]
        projeto_id="p", mensagem=perigosa
    )

    assert not r.is_error
    assert _git(repo["raiz"], "log", "-1", "--pretty=%s").strip() == perigosa
    assert (repo["raiz"] / "src" / "m0.py").exists(), "nada foi apagado"


async def test_nome_de_arquivo_nao_vira_opcao_do_git(
    monkeypatch: pytest.MonkeyPatch, repo: dict[str, Path], store: object
) -> None:
    """Arquivo chamado `--upload-pack=...` e so um nome esquisito."""
    r = await DevEscreverTool(get_settings(), store).execute(  # type: ignore[arg-type]
        projeto_id="p", arquivo="--upload-pack=algo.py", conteudo="x = 1\n"
    )
    assert not r.is_error
    assert (repo["raiz"] / "--upload-pack=algo.py").is_file()


# ---------------------------------------------------------------- superficie
async def test_escrever_fora_da_superficie_e_recusado(
    repo: dict[str, Path], store: object
) -> None:
    r = await DevEscreverTool(get_settings(), store).execute(  # type: ignore[arg-type]
        projeto_id="p", arquivo="../fora.py", conteudo="x"
    )
    assert r.is_error
    assert "sai da raiz" in r.content


async def test_ler_segredo_e_recusado_sem_vazar(
    repo: dict[str, Path], store: object
) -> None:
    """Segredo no contexto do modelo e segredo vazado. Ler conta igual a escrever."""
    (repo["raiz"] / ".env").write_text("TOKEN=abracadabra", encoding="utf-8")

    r = await DevLerTool(get_settings(), store).execute(  # type: ignore[arg-type]
        projeto_id="p", arquivo=".env"
    )

    assert r.is_error
    assert "protegida" in r.content
    assert "abracadabra" not in r.content


async def test_nada_mudou_nao_publica(
    monkeypatch: pytest.MonkeyPatch, repo: dict[str, Path], store: object
) -> None:
    _sandbox_verde(monkeypatch)
    r = await DevPublicarTool(get_settings(), store).execute(  # type: ignore[arg-type]
        projeto_id="p", mensagem="commit vazio"
    )
    assert r.is_error
    assert "Nada mudou" in r.content


async def test_listar_mostra_o_que_mudou(repo: dict[str, Path], store: object) -> None:
    (repo["raiz"] / "src" / "novo.py").write_text("y = 1\n", encoding="utf-8")

    r = await DevListarTool(get_settings(), store).execute()  # type: ignore[arg-type]

    assert "Projeto" in r.content
    assert "novo.py" in r.content
    assert str(repo["raiz"]) not in json.dumps(r.dados), "a raiz nao sai para o modelo"
