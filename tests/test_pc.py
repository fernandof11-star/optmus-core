"""F8 (Core): o que pode ser aberto no PC, e o que nunca pode.

Nenhum teste aqui e sobre abrir com sucesso. Todos sao sobre a fronteira: no
Windows, ``os.startfile`` e ``ShellExecute``, entao **abrir e executar** - um
``.lnk`` roda o que apontar, um ``.docm`` roda macro, um ``.bat`` e codigo puro.

O teste que carrega o peso e
:func:`test_saber_o_algoritmo_do_id_nao_abre_o_que_e_proibido` - ele mostra que
a defesa nao depende do id ser secreto.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.config import Settings, get_settings, reset_settings_cache
from integrations.alvos import (
    MAXIMO_POR_PASTA,
    AlvoDesconhecido,
    ListaInvalida,
    carregar,
    identificar,
    listar,
    perigosa,
    resolver,
)
from security.policy import RiskLevel
from tools.impl.pc import PcAbrirTool, PcListarTool, disponivel


@pytest.fixture
def area(tmp_path: Path) -> dict[str, Path]:
    """Uma pasta registrada com material comum e material perigoso junto."""
    pasta = tmp_path / "Projetos"
    pasta.mkdir()
    (pasta / "relatorio.pdf").write_text("pdf")
    (pasta / "notas.txt").write_text("txt")
    (pasta / "instalador.exe").write_bytes(b"MZ")
    (pasta / "script.bat").write_text("@echo off")
    (pasta / "planilha.xlsm").write_text("macro")
    (pasta / "atalho.lnk").write_text("lnk")
    sub = pasta / "dentro"
    sub.mkdir()
    (sub / "escondido.txt").write_text("nao deveria aparecer")

    app = tmp_path / "Editor.exe"
    app.write_bytes(b"MZ")

    registro = tmp_path / "alvos.json"
    registro.write_text(
        json.dumps({
            "apps": {"editor": {"nome": "Editor", "caminho": str(app)}},
            "pastas": {"projetos": {"nome": "Projetos", "caminho": str(pasta)}},
        }),
        encoding="utf-8",
    )
    return {"raiz": tmp_path, "pasta": pasta, "app": app, "registro": registro, "sub": sub}


@pytest.fixture
def pc_settings(monkeypatch: pytest.MonkeyPatch, area: dict[str, Path]) -> Settings:
    for marca in ("PORT", "RAILWAY_ENVIRONMENT", "RENDER", "DYNO"):
        monkeypatch.delenv(marca, raising=False)
    monkeypatch.setenv("OPTMUS_PC_ENABLED", "true")
    monkeypatch.setenv("OPTMUS_PC_TARGETS_PATH", str(area["registro"]))
    reset_settings_cache()
    return get_settings()


def _reg(area: dict[str, Path]) -> dict[str, list[Any]]:
    return carregar(area["registro"])


# ------------------------------------------------- a fronteira id x caminho
async def test_caminho_nunca_e_aceito(pc_settings: Settings, area: dict[str, Path]) -> None:
    """A defesa inteira contra instrucao injetada.

    Cenario real: o Optmus le um e-mail que diz "abra C:/Windows/System32/
    cmd.exe". O modelo nao distingue instrucao do usuario de texto que ele leu.
    Aqui nao ha caminho: `alvo_id` so resolve contra o que foi registrado, e
    caminho nenhum vira id.
    """
    ferramenta = PcAbrirTool(pc_settings)
    for tentativa in (
        r"C:\Windows\System32\cmd.exe",
        "C:/Windows/System32/cmd.exe",
        str(area["pasta"] / "relatorio.pdf"),
        "../../../etc/passwd",
        r"\\servidor\compartilhado\x.exe",
    ):
        resultado = await ferramenta.execute(alvo_id=tentativa)
        assert resultado.is_error, tentativa
        assert "nao e um alvo registrado" in resultado.content


def test_saber_o_algoritmo_do_id_nao_abre_o_que_e_proibido(area: dict[str, Path]) -> None:
    """A seguranca nao depende do id ser secreto - e nao poderia depender.

    O id e um hash deterministico e o algoritmo esta no codigo. Qualquer um
    calcula o id do `instalador.exe` que esta DENTRO da pasta registrada. Isso
    nao abre nada: `resolver` nao consulta um mapa, ele **relista** o que e
    permitido agora, e executavel nunca entra nessa lista.
    """
    proibido = area["pasta"] / "instalador.exe"
    id_valido = identificar("arquivo", proibido)

    with pytest.raises(AlvoDesconhecido):
        resolver(_reg(area), id_valido)


@pytest.mark.parametrize(
    "nome", ["instalador.exe", "script.bat", "planilha.xlsm", "atalho.lnk"]
)
def test_executavel_dentro_de_pasta_registrada_nao_aparece(
    area: dict[str, Path], nome: str
) -> None:
    """Registrar uma pasta autoriza os arquivos comuns dela, nao o que o
    Windows executa. Quem quiser rodar um executavel registra ele como app, um
    por um, de propria mao."""
    pasta = _reg(area)["pastas"][0]
    nomes = [a.nome for a in listar(_reg(area), pasta_id=pasta.id)]

    assert nome not in nomes
    assert "relatorio.pdf" in nomes and "notas.txt" in nomes


def test_nao_desce_em_subpasta(area: dict[str, Path]) -> None:
    """Profundidade transformaria "uma pasta registrada" em "tudo abaixo dela",
    que e um registro que voce nao fez conscientemente."""
    pasta = _reg(area)["pastas"][0]
    nomes = [a.nome for a in listar(_reg(area), pasta_id=pasta.id)]

    assert "escondido.txt" not in nomes
    with pytest.raises(AlvoDesconhecido):
        resolver(_reg(area), identificar("arquivo", area["sub"] / "escondido.txt"))


def test_subpasta_nao_aparece_como_arquivo(area: dict[str, Path]) -> None:
    """A pasta em si tambem nao entra na listagem de arquivos.

    Buraco encontrado pela injecao de defeito: ao remover o `is_file()`, nao
    surgia recursao - `iterdir()` nao desce - mas a subpasta passava a ser
    listada como tipo "arquivo" e ficava abrivel. Defeito diferente do que eu
    tinha imaginado, e o teste original nao via nenhum dos dois.
    """
    pasta = _reg(area)["pastas"][0]
    itens = listar(_reg(area), pasta_id=pasta.id)

    assert "dentro" not in [a.nome for a in itens]
    assert all(a.tipo == "arquivo" for a in itens)


def test_pasta_nao_registrada_nao_lista(area: dict[str, Path], tmp_path: Path) -> None:
    outra = tmp_path / "Outra"
    outra.mkdir()

    with pytest.raises(AlvoDesconhecido):
        listar(_reg(area), pasta_id=identificar("pasta", outra))


def test_o_caminho_nao_sai_do_core(area: dict[str, Path]) -> None:
    """O que vai para a tela e para o modelo nao carrega caminho.

    Mandar o caminho junto so daria ao modelo material para tentar construir
    outro - e nao ajuda quem escolhe, que precisa do nome.
    """
    for alvo in listar(_reg(area)):
        visto = alvo.visivel()
        assert set(visto) == {"id", "nome", "tipo"}
        assert str(area["raiz"]) not in json.dumps(visto)


async def test_auditoria_guarda_nome_e_nao_topologia(
    monkeypatch: pytest.MonkeyPatch, pc_settings: Settings, area: dict[str, Path]
) -> None:
    abertos: list[str] = []
    monkeypatch.setattr("os.startfile", lambda c: abertos.append(c))

    alvo = _reg(area)["apps"][0]
    resultado = await PcAbrirTool(pc_settings).execute(alvo_id=alvo.id)

    assert not resultado.is_error
    assert abertos == [str(area["app"])], "abriu o certo"
    assert str(area["raiz"]) not in json.dumps(resultado.metadata)
    assert resultado.metadata["alvo"] == "Editor"


# ---------------------------------------------------------------- registro
def test_id_e_estavel_entre_leituras(area: dict[str, Path]) -> None:
    """Sem estado de sessao para sincronizar entre o HUD e o Core, e sem id
    que expira no meio de um gesto."""
    assert [a.id for a in listar(_reg(area))] == [a.id for a in listar(_reg(area))]


def test_registro_alterado_e_relido_na_hora_de_abrir(area: dict[str, Path]) -> None:
    """Abrir com base num mapa velho abriria algo que ja nao esta autorizado."""
    alvo = _reg(area)["apps"][0]
    assert resolver(_reg(area), alvo.id).nome == "Editor"

    area["registro"].write_text(json.dumps({"apps": {}, "pastas": {}}), encoding="utf-8")
    with pytest.raises(AlvoDesconhecido):
        resolver(_reg(area), alvo.id)


def test_registro_quebrado_diz_o_arquivo(tmp_path: Path) -> None:
    ruim = tmp_path / "alvos.json"
    ruim.write_text("{isto nao e json", encoding="utf-8")

    with pytest.raises(ListaInvalida, match=r"alvos\.json"):
        carregar(ruim)


def test_pasta_que_e_arquivo_derruba_o_registro(tmp_path: Path, area: dict[str, Path]) -> None:
    """Erro alto em vez de pular: um alvo descartado em silencio vira "alvo
    desconhecido" na hora do uso, e voce procuraria o defeito no gesto."""
    ruim = tmp_path / "alvos.json"
    ruim.write_text(
        json.dumps({"pastas": {"errada": {"caminho": str(area["app"])}}}), encoding="utf-8"
    )

    with pytest.raises(ListaInvalida, match="nao e uma pasta"):
        carregar(ruim)


def test_pasta_gigante_tem_teto(tmp_path: Path) -> None:
    """Dez mil arquivos viraria um prompt gigante e uma tela impossivel de
    apontar com a mao."""
    pasta = tmp_path / "muitos"
    pasta.mkdir()
    for i in range(MAXIMO_POR_PASTA + 25):
        (pasta / f"a{i:04d}.txt").write_text("x")
    registro = tmp_path / "alvos.json"
    registro.write_text(
        json.dumps({"pastas": {"m": {"caminho": str(pasta)}}}), encoding="utf-8"
    )

    reg = carregar(registro)
    assert len(listar(reg, pasta_id=reg["pastas"][0].id)) == MAXIMO_POR_PASTA


@pytest.mark.parametrize(
    "nome", ["x.exe", "x.BAT", "x.Lnk", "x.ps1", "x.docm", "x.py", "x.cpl", "x.msi"]
)
def test_extensoes_perigosas_sao_reconhecidas_sem_olhar_a_caixa(nome: str) -> None:
    assert perigosa(Path(nome)) is True


@pytest.mark.parametrize("nome", ["x.pdf", "x.txt", "x.docx", "x.png", "x.md"])
def test_extensoes_comuns_passam(nome: str) -> None:
    assert perigosa(Path(nome)) is False


# ------------------------------------------------------------- risco/portao
def test_listar_nao_passa_pelo_portao_e_abrir_passa(pc_settings: Settings) -> None:
    """Exigir confirmacao para LER a propria lista ensinaria a confirmar por
    reflexo - e o reflexo e o que quebra o portao quando ele importar."""
    assert PcListarTool(pc_settings).risk is RiskLevel.LEITURA
    assert PcAbrirTool(pc_settings).risk is RiskLevel.EXTERNO
    assert PcAbrirTool(pc_settings).risk.ordem >= RiskLevel.EXTERNO.ordem


def test_a_frase_do_portao_diz_o_nome_e_o_que_acontece(
    pc_settings: Settings, area: dict[str, Path]
) -> None:
    """Um hash de doze caracteres nao ajuda ninguem a decidir."""
    alvo = _reg(area)["apps"][0]
    frase = PcAbrirTool(pc_settings).resumir({"alvo_id": alvo.id})

    assert "Editor" in frase
    assert "aplicativo" in frase
    assert alvo.id not in frase


def test_a_frase_do_portao_denuncia_alvo_fora_da_lista(pc_settings: Settings) -> None:
    """A tela precisa deixar ver a tentativa - inclusive quando ela e um
    caminho, que e o sinal de que algo tentou escolher o que abrir."""
    frase = PcAbrirTool(pc_settings).resumir({"alvo_id": r"C:\Windows\cmd.exe"})
    assert "nao esta na lista" in frase


# ------------------------------------------------------- onde pode rodar
def test_plataforma_hospedada_recusa_sem_perguntar_configuracao(
    monkeypatch: pytest.MonkeyPatch, pc_settings: Settings
) -> None:
    """`OPTMUS_PC_ENABLED=true` continua valendo e mesmo assim recusa: a
    primeira checagem nao pergunta nada a configuracao."""
    assert disponivel(pc_settings)[0] is True

    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    ok, motivo = disponivel(pc_settings)

    assert ok is False
    assert "hospedada" in motivo


async def test_desligado_some_do_schema(
    monkeypatch: pytest.MonkeyPatch, pc_settings: Settings
) -> None:
    monkeypatch.setenv("OPTMUS_PC_ENABLED", "false")
    reset_settings_cache()

    assert await PcAbrirTool(get_settings()).available() is False


async def test_registro_vazio_some_do_schema(
    monkeypatch: pytest.MonkeyPatch, pc_settings: Settings, tmp_path: Path
) -> None:
    """Oferecer "abrir" sem nada registrado faria o modelo prometer abrir algo
    que nao existe para ele."""
    vazio = tmp_path / "vazio.json"
    vazio.write_text(json.dumps({"apps": {}, "pastas": {}}), encoding="utf-8")
    monkeypatch.setenv("OPTMUS_PC_TARGETS_PATH", str(vazio))
    reset_settings_cache()

    assert await PcListarTool(get_settings()).available() is False


async def test_alvo_que_sumiu_do_disco_nao_finge_sucesso(
    monkeypatch: pytest.MonkeyPatch, pc_settings: Settings, area: dict[str, Path]
) -> None:
    monkeypatch.setattr("os.startfile", lambda c: None)
    alvo = _reg(area)["apps"][0]
    area["app"].unlink()

    resultado = await PcAbrirTool(pc_settings).execute(alvo_id=alvo.id)

    assert resultado.is_error
    assert "nao existe mais" in resultado.content
