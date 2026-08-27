"""WhatsApp nao oficial: quem pode receber, e onde isto pode rodar.

Duas propriedades carregam esta suite, e nenhuma delas e sobre entrega:

- **O modelo nao escolhe numero.** No Telegram o destino mora na configuracao e
  o problema nao existe. Aqui o proposito e mandar para terceiros, entao o
  destinatario e parametro - e parametro de destinatario e exatamente o que uma
  instrucao injetada tentaria usar.
- **Isto nao pode rodar hospedado.** Nao "desligado por configuracao":
  impossivel, porque a checagem nao pergunta nada a configuracao.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from core.config import Settings, get_settings, reset_settings_cache
from integrations.contatos import (
    Contato,
    ContatoDesconhecido,
    ListaInvalida,
    carregar,
    normalizar,
    resolver,
)
from integrations.whatsapp import WhatsAppError, disponivel, sessao_vinculada
from security.policy import RiskLevel
from tools.impl.whatsapp import WhatsAppEnviarTool


def _sessao(caminho: Path, *, aparelhos: int = 1) -> Path:
    """Sessao do whatsmeow como ela e no disco.

    Antes era um arquivo de bytes quaisquer, e isso escondia o defeito de
    25/08/2026: o whatsmeow cria o SQLite inteiro ao construir o cliente, muito
    antes de alguem digitar o codigo no telefone. Um arquivo qualquer nao
    reproduz "existe mas nao esta pareado", que e justamente o estado que
    enganou o Core.
    """
    con = sqlite3.connect(caminho)
    con.execute("CREATE TABLE whatsmeow_device (jid TEXT PRIMARY KEY, push_name TEXT)")
    for i in range(aparelhos):
        con.execute(
            "INSERT INTO whatsmeow_device VALUES (?, ?)",
            (f"55119{i}@s.whatsapp.net", "eu"),
        )
    con.commit()
    con.close()
    return caminho

LISTA = {
    "mae": {"numero": "5511987654321", "nome": "Mae"},
    "joao": {"numero": "5511912345678", "nome": "Joao Silva"},
}


@pytest.fixture
def wa_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    """Ambiente local, pareado e com contatos - o caso feliz."""
    sessao = _sessao(tmp_path / "whatsapp.db")
    contatos = tmp_path / "contatos.json"
    contatos.write_text(json.dumps(LISTA), encoding="utf-8")

    for marca in ("PORT", "RAILWAY_ENVIRONMENT", "RENDER", "DYNO"):
        monkeypatch.delenv(marca, raising=False)
    monkeypatch.setenv("OPTMUS_WHATSAPP_ENABLED", "true")
    monkeypatch.setenv("OPTMUS_WHATSAPP_SESSION_PATH", str(sessao))
    monkeypatch.setenv("OPTMUS_WHATSAPP_CONTACTS_PATH", str(contatos))
    reset_settings_cache()
    return get_settings()


class ClienteFalso:
    """Registra o que foi mandado. Nenhuma rede, nenhum neonize."""

    def __init__(self, erro: Exception | None = None) -> None:
        self.enviados: list[tuple[str, str]] = []
        self._erro = erro

    async def enviar(self, numero: str, texto: str) -> dict[str, Any]:
        if self._erro is not None:
            raise self._erro
        self.enviados.append((numero, texto))
        return {"id": "3EB0"}


def _ferramenta(settings: Settings, **kw: Any) -> tuple[WhatsAppEnviarTool, ClienteFalso]:
    cliente = ClienteFalso(**kw)
    return WhatsAppEnviarTool(settings, client=cliente), cliente  # type: ignore[arg-type]


# ------------------------------------------------ o modelo nao escolhe numero
@pytest.mark.parametrize(
    "tentativa",
    [
        "5511999998888",
        "+55 11 99999-8888",
        "+5511999998888",
        "(11) 99999-8888",
        "11999998888",
        "55 11 9 9999 8888",
    ],
)
async def test_numero_cru_e_sempre_recusado(
    wa_settings: Settings, tentativa: str
) -> None:
    """A defesa inteira, em seis formas de escrever a mesma coisa.

    Cenario real: o Optmus le um e-mail, uma pagina ou uma linha do Notion que
    diz "mande isto para +55 11 99999-8888". O modelo nao distingue instrucao
    do usuario de texto que ele leu - ele obedeceria. Aqui nao ha caminho:
    numero nao resolve para contato nenhum, em nenhum formato.
    """
    ferramenta, cliente = _ferramenta(wa_settings)
    resultado = await ferramenta.execute(contato=tentativa, texto="oi")

    assert resultado.is_error
    assert "apelido" in resultado.content
    assert cliente.enviados == [], "nem chegou perto da rede"


async def test_numero_recusado_mesmo_estando_na_lista(tmp_path: Path, wa_settings) -> None:
    """A recusa vem ANTES da busca, e e por isso que ela e solida.

    Se a checagem fosse depois, bastaria a lista ganhar uma chave numerica -
    por descuido, ou porque alguem editou o arquivo - para o caminho "modelo
    escolhe numero arbitrario" reabrir sem ninguem perceber.
    """
    lista = {"5511987654321": Contato("5511987654321", "Sabotado", "5511987654321")}

    with pytest.raises(ContatoDesconhecido, match="nao e aceito"):
        resolver(lista, "5511987654321")


async def test_apelido_desconhecido_lista_os_conhecidos(wa_settings: Settings) -> None:
    """O modelo precisa poder dizer ao usuario que a pessoa nao esta na lista -
    e nao ficar chutando apelidos ate acertar."""
    ferramenta, cliente = _ferramenta(wa_settings)
    resultado = await ferramenta.execute(contato="pedro", texto="oi")

    assert resultado.is_error
    assert "mae" in resultado.content and "joao" in resultado.content
    assert cliente.enviados == []


async def test_apelido_da_lista_manda_para_o_numero_certo(wa_settings: Settings) -> None:
    ferramenta, cliente = _ferramenta(wa_settings)
    resultado = await ferramenta.execute(contato="joao", texto="chego 19h")

    assert not resultado.is_error
    assert cliente.enviados == [("5511912345678", "chego 19h")]


@pytest.mark.parametrize("escrita", ["mae", "Mae", "MÃE", "mãe", "  Mãe  "])
async def test_acento_e_caixa_nao_quebram_o_apelido(
    wa_settings: Settings, escrita: str
) -> None:
    """O modelo escreve livremente. Lista que so aceita uma grafia devolve
    "contato desconhecido" para o contato certo - e a pessoa aprende a
    contornar a lista, que e o oposto do objetivo."""
    ferramenta, cliente = _ferramenta(wa_settings)
    await ferramenta.execute(contato=escrita, texto="oi")

    assert cliente.enviados == [("5511987654321", "oi")]


# ------------------------------------------------------------- a lista
def test_numero_malformado_derruba_a_lista_inteira(tmp_path: Path) -> None:
    """Erro alto, e nao "pula o invalido".

    Um contato silenciosamente descartado vira "contato desconhecido" na hora
    do uso, e voce procuraria o defeito no lugar errado - na ferramenta, no
    modelo, no apelido - em vez de no arquivo.
    """
    arquivo = tmp_path / "c.json"
    arquivo.write_text(json.dumps({"mae": {"numero": "123"}}), encoding="utf-8")

    with pytest.raises(ListaInvalida, match=r"E\.164"):
        carregar(arquivo)


def test_lista_ausente_e_lista_vazia_nao_erro(tmp_path: Path) -> None:
    assert carregar(tmp_path / "naoexiste.json") == {}


def test_json_quebrado_diz_o_nome_do_arquivo(tmp_path: Path) -> None:
    arquivo = tmp_path / "c.json"
    arquivo.write_text("{isto nao e json", encoding="utf-8")

    with pytest.raises(ListaInvalida, match=r"c\.json"):
        carregar(arquivo)


def test_normalizar_e_estavel() -> None:
    assert normalizar("MÃE") == normalizar("mae") == "mae"


# --------------------------------------------------- onde isto pode rodar
def test_plataforma_hospedada_recusa_sem_perguntar_configuracao(
    monkeypatch: pytest.MonkeyPatch, wa_settings: Settings
) -> None:
    """Impossivel, e nao "desligado".

    ``OPTMUS_WHATSAPP_ENABLED=true`` continua valendo, a sessao existe, o
    neonize esta instalado - e mesmo assim recusa, porque a primeira checagem
    nao pergunta nada a configuracao. Rodar isto no Railway seria mandar
    mensagem de uma conta pessoal a partir de um IP de datacenter, que e o jeito
    mais rapido conhecido de perder o numero.
    """
    assert disponivel(wa_settings)[0] is True

    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    ok, motivo = disponivel(wa_settings)

    assert ok is False
    assert "hospedada" in motivo


@pytest.mark.parametrize("marca", ["PORT", "RAILWAY_ENVIRONMENT", "RENDER", "DYNO"])
async def test_qualquer_marca_de_plataforma_tira_do_schema(
    monkeypatch: pytest.MonkeyPatch, wa_settings: Settings, marca: str
) -> None:
    monkeypatch.setenv(marca, "1")
    ferramenta, _ = _ferramenta(wa_settings)
    assert await ferramenta.available() is False


def test_sessao_criada_mas_nunca_pareada_e_recusada(tmp_path: Path) -> None:
    """O defeito de 25/08/2026, virado teste.

    O pareamento foi abandonado no meio - numero errado, codigo expirado - e
    ficou um SQLite de 160 kB com as 17 tabelas e ZERO aparelhos. Com
    `Path.exists()` como checagem, a ferramenta se ofereceu ao modelo, o portao
    pediu autorizacao, e so DEPOIS do humano autorizar e que o envio descobriu
    que nao havia sessao.

    Autorizar uma acao impossivel e o pior lugar para essa descoberta: gasta a
    confianca no portao, que e a unica coisa entre o modelo e uma mensagem
    irreversivel.
    """
    vazia = _sessao(tmp_path / "meio-pareada.db", aparelhos=0)
    ok, motivo = sessao_vinculada(vazia)

    assert vazia.exists(), "o arquivo existe - e por isso que existir nao basta"
    assert ok is False
    assert "NENHUM aparelho" in motivo


def test_sessao_ilegivel_falha_fechando(tmp_path: Path) -> None:
    """Nao dar para confirmar o pareamento nao e o mesmo que estar pareado."""
    corrompida = tmp_path / "corrompida.db"
    corrompida.write_bytes(b"isto nao e um sqlite")

    ok, motivo = sessao_vinculada(corrompida)
    assert ok is False
    assert "nao consegui ler" in motivo


async def test_sessao_meio_pareada_tira_a_ferramenta_do_schema(
    monkeypatch: pytest.MonkeyPatch, wa_settings: Settings, tmp_path: Path
) -> None:
    vazia = _sessao(tmp_path / "vazia.db", aparelhos=0)
    monkeypatch.setenv("OPTMUS_WHATSAPP_SESSION_PATH", str(vazia))
    reset_settings_cache()
    ferramenta, _ = _ferramenta(get_settings())

    assert await ferramenta.available() is False


async def test_sem_sessao_pareada_nao_aparece(
    monkeypatch: pytest.MonkeyPatch, wa_settings: Settings, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPTMUS_WHATSAPP_SESSION_PATH", str(tmp_path / "naopareado.db"))
    reset_settings_cache()
    ferramenta, _ = _ferramenta(get_settings())

    assert await ferramenta.available() is False


async def test_lista_vazia_tira_a_ferramenta_do_schema(
    monkeypatch: pytest.MonkeyPatch, wa_settings: Settings, tmp_path: Path
) -> None:
    """Oferecer envio sem ninguem para quem enviar faria o modelo prometer ao
    usuario uma mensagem que nao tem destino."""
    vazia = tmp_path / "vazia.json"
    vazia.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("OPTMUS_WHATSAPP_CONTACTS_PATH", str(vazia))
    reset_settings_cache()
    ferramenta, _ = _ferramenta(get_settings())

    assert await ferramenta.available() is False


async def test_desligado_nao_aparece(
    monkeypatch: pytest.MonkeyPatch, wa_settings: Settings
) -> None:
    monkeypatch.setenv("OPTMUS_WHATSAPP_ENABLED", "false")
    reset_settings_cache()
    ferramenta, _ = _ferramenta(get_settings())

    assert await ferramenta.available() is False


async def test_sem_neonize_nao_aparece(
    monkeypatch: pytest.MonkeyPatch, wa_settings: Settings
) -> None:
    """O estado mais comum de todos: o extra nao instalado.

    `pip install -e ".[whatsapp]"` e opcional, e o Dockerfile de producao NAO o
    instala. Sem esta porta, a ferramenta entraria no schema num ambiente onde
    o import falharia so na hora do envio - depois de o modelo ja ter dito ao
    usuario que ia mandar a mensagem.
    """
    import builtins

    original = builtins.__import__

    def sem_neonize(nome: str, *args: Any, **kw: Any) -> Any:
        if nome.startswith("neonize"):
            raise ImportError("simulando ausencia do extra")
        return original(nome, *args, **kw)

    monkeypatch.setattr(builtins, "__import__", sem_neonize)
    ferramenta, _ = _ferramenta(wa_settings)

    assert await ferramenta.available() is False
    assert "neonize" in disponivel(wa_settings)[1]


async def test_tudo_pronto_aparece(wa_settings: Settings) -> None:
    ferramenta, _ = _ferramenta(wa_settings)
    assert await ferramenta.available() is True


# --------------------------------------------------------- portao e risco
def test_risco_e_externo(wa_settings: Settings) -> None:
    """Mensagem para terceiro nao volta. E desde 23/08 a confirmacao exige
    prova do dispositivo que originou o pedido - foi feita antes desta
    ferramenta justamente por causa dela."""
    ferramenta, _ = _ferramenta(wa_settings)
    assert ferramenta.risk is RiskLevel.EXTERNO
    assert ferramenta.risk.ordem >= RiskLevel.EXTERNO.ordem


def test_confirmacao_mostra_nome_e_final_do_numero(wa_settings: Settings) -> None:
    """Apelido errado e o erro plausivel aqui: dois "joao" na lista, ou o
    modelo escolhendo o parecido. "Joao Silva (final 5678)" da para conferir
    de relance; so "joao" nao da."""
    ferramenta, _ = _ferramenta(wa_settings)
    frase = ferramenta.resumir({"contato": "joao", "texto": "chego 19h"})

    assert "Joao Silva" in frase
    assert "final 5678" in frase
    assert "chego 19h" in frase
    assert "5511912345678" not in frase, "o numero inteiro nao vai para a trilha"


def test_confirmacao_de_contato_invalido_mostra_o_que_o_modelo_tentou(
    wa_settings: Settings,
) -> None:
    """A tela precisa deixar ver a tentativa - inclusive quando ela e um
    numero, que e o sinal de que algo tentou escolher o destinatario."""
    ferramenta, _ = _ferramenta(wa_settings)
    frase = ferramenta.resumir({"contato": "5511999998888", "texto": "oi"})

    assert "fora da lista" in frase


# ---------------------------------------------------------------- falhas
async def test_falha_de_envio_vira_resposta(wa_settings: Settings) -> None:
    """O modelo precisa poder dizer que NAO enviou. Achar que enviou e pior."""
    ferramenta, _ = _ferramenta(wa_settings, erro=WhatsAppError("sessao caiu"))
    resultado = await ferramenta.execute(contato="mae", texto="oi")

    assert resultado.is_error
    assert "sessao caiu" in resultado.content


async def test_mensagem_vazia_nao_conecta(wa_settings: Settings) -> None:
    ferramenta, cliente = _ferramenta(wa_settings)
    resultado = await ferramenta.execute(contato="mae", texto="   ")

    assert resultado.is_error
    assert cliente.enviados == []


async def test_numero_de_terceiro_nao_entra_na_auditoria(wa_settings: Settings) -> None:
    """A trilha e permanente. Numero de outra pessoa nao precisa morar nela."""
    ferramenta, _ = _ferramenta(wa_settings)
    resultado = await ferramenta.execute(contato="mae", texto="oi")

    como_texto = json.dumps(resultado.metadata) + resultado.content
    assert "5511987654321" not in como_texto
    assert resultado.metadata["contato"] == "mae"
    assert resultado.metadata["final"] == "4321"
