"""Guarda contra um bug que nenhum linter do projeto pega.

Reatribuir um parametro DENTRO de um laco destroi o valor que o chamador pediu,
sem erro, sem aviso e sem falha de teste - a funcao simplesmente passa a
responder com o resto da ultima iteracao.

Foi assim que ``GET /notion/alertas/diagnostico?dias=-30`` passou a devolver o
numero de dias da ultima linha lida do Notion, em vez do valor pedido. O teste
unitario que existia nao pegou porque usava uma base vazia: sem linhas, o laco
nao roda e o parametro sobrevive.

``PLR1704`` do ruff cobre alvo de ``for``/``with`` com nome de parametro, e
``PLW2901`` cobre variavel de laco sobrescrita - nenhuma das duas cobre
atribuicao direta. Dai este teste.

Reatribuir parametro FORA de laco continua liberado: ``hoje = hoje or hoje()``
e idioma corrente e inofensivo, porque roda uma vez so.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACOTES = ("core", "memory", "integrations", "tools", "security", "perception", "expression")
RAIZ = Path(__file__).resolve().parent.parent


def _parametros(funcao: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    args = funcao.args
    todos = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg:
        todos.append(args.vararg)
    if args.kwarg:
        todos.append(args.kwarg)
    return {a.arg for a in todos}


def _alvos(no: ast.stmt) -> list[str]:
    """Nomes atribuidos por este statement (sem descer em expressoes)."""
    if isinstance(no, ast.Assign):
        return [t.id for t in no.targets if isinstance(t, ast.Name)]
    if isinstance(no, ast.AugAssign) and isinstance(no.target, ast.Name):
        return [no.target.id]
    if isinstance(no, ast.AnnAssign) and isinstance(no.target, ast.Name) and no.value is not None:
        return [no.target.id]
    return []


def _dentro_de_laco(corpo: list[ast.stmt], parametros: set[str]) -> list[tuple[str, int]]:
    """Percorre um corpo de laco procurando reatribuicao de parametro."""
    achados: list[tuple[str, int]] = []
    for no in corpo:
        # Funcao aninhada tem parametros proprios: nao e o mesmo escopo.
        if isinstance(no, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        for nome in _alvos(no):
            if nome in parametros:
                achados.append((nome, no.lineno))
        for filho in ast.iter_child_nodes(no):
            if isinstance(filho, ast.stmt):
                achados.extend(_dentro_de_laco([filho], parametros))
    return achados


def _violacoes(caminho: Path) -> list[str]:
    arvore = ast.parse(caminho.read_text(encoding="utf-8"))
    try:
        rotulo = str(caminho.relative_to(RAIZ))
    except ValueError:
        rotulo = caminho.name  # arquivo de teste temporario, fora da raiz

    problemas: dict[tuple[int, str], str] = {}
    for no in ast.walk(arvore):
        if not isinstance(no, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        parametros = _parametros(no)
        if not parametros:
            continue
        for filho in ast.walk(no):
            if not isinstance(filho, ast.For | ast.AsyncFor | ast.While):
                continue
            for nome, linha in _dentro_de_laco(filho.body, parametros):
                # Chaveado por (linha, nome): laco aninhado encontra a mesma
                # atribuicao uma vez por nivel, e uma so violacao basta.
                problemas[(linha, nome)] = (
                    f"{rotulo}:{linha}: {no.name}() reatribui o "
                    f"parametro {nome!r} dentro de um laco"
                )
    return [problemas[chave] for chave in sorted(problemas)]


def test_nenhum_parametro_e_reatribuido_dentro_de_laco() -> None:
    problemas: list[str] = []
    for pacote in PACOTES:
        for arquivo in sorted((RAIZ / pacote).rglob("*.py")):
            problemas.extend(_violacoes(arquivo))
    assert not problemas, "\n".join(problemas)


def test_a_checagem_pega_o_padrao_do_bug(tmp_path: Path) -> None:
    """Sem isto, um guarda quebrado passaria despercebido."""
    arquivo = tmp_path / "exemplo.py"
    arquivo.write_text(
        "def f(dias, linhas):\n"
        "    for linha in linhas:\n"
        "        dias = linha\n"
        "    return dias\n",
        encoding="utf-8",
    )
    problemas = _violacoes(arquivo)
    assert len(problemas) == 1
    assert "reatribui o parametro 'dias'" in problemas[0]


def test_reatribuicao_fora_de_laco_e_permitida(tmp_path: Path) -> None:
    """`hoje = hoje or agora()` roda uma vez e nao destroi nada."""
    arquivo = tmp_path / "exemplo.py"
    arquivo.write_text(
        "def f(hoje=None, linhas=()):\n"
        "    hoje = hoje or 1\n"
        "    for linha in linhas:\n"
        "        outro = linha\n"
        "    return hoje, outro\n",
        encoding="utf-8",
    )
    assert _violacoes(arquivo) == []
