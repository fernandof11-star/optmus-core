"""Camadas de memoria: gravacao, recuperacao, decaimento e versionamento."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.config import Settings
from memory.embeddings import HashingEmbedder, conferir_dimensao
from memory.scoring import fator_frequencia, fator_recencia, similaridade_de_distancia
from memory.store import Store
from memory.system import MemorySystem
from memory.working import WorkingMemory


@pytest.fixture
def memoria(settings: Settings, store: Store) -> MemorySystem:
    return MemorySystem(settings, store, embedder=HashingEmbedder(settings.embedding_dim))


# ------------------------------------------------------------------ trabalho
def test_memoria_de_trabalho_guarda_a_conversa() -> None:
    trabalho = WorkingMemory(ttl_minutes=30)
    trabalho.add_exchange("onde eu moro", "Sao Paulo")
    assert [m["content"] for m in trabalho.messages()] == ["onde eu moro", "Sao Paulo"]


def test_memoria_de_trabalho_expira_por_ttl() -> None:
    trabalho = WorkingMemory(ttl_minutes=30)
    trabalho.add_exchange("assunto do cafe da manha", "certo")
    trabalho._ultimo = datetime.now(UTC) - timedelta(hours=2)

    assert trabalho.expirada
    assert trabalho.messages() == []
    assert trabalho.expiracoes == 1


def test_resposta_vazia_nao_entra_no_historico() -> None:
    trabalho = WorkingMemory()
    trabalho.add_exchange("silencio", "")
    assert len(trabalho) == 0


# ----------------------------------------------------------------- episodica
async def test_episodio_e_recuperado_pelo_conteudo(memoria: MemorySystem) -> None:
    await memoria.episodic.record_exchange(
        "lembra que o contador chama Ricardo", "Anotado."
    )
    await memoria.episodic.record_exchange("abre o youtube", "Abrindo.")

    achados = await memoria.episodic.recall("contador", limit=3)
    assert achados
    assert "Ricardo" in achados[0].content


async def test_turno_grava_trabalho_e_episodio(memoria: MemorySystem) -> None:
    await memoria.record_turn("quem cuida do meu imposto", "O Ricardo.", correlation_id="c1")
    assert len(memoria.working) == 2
    assert await memoria.episodic.count() == 1


async def test_acesso_incrementa_a_frequencia(memoria: MemorySystem) -> None:
    await memoria.episodic.record("o contador chama Ricardo")
    await memoria.episodic.recall("contador")
    await memoria.episodic.recall("contador")

    linha = (await memoria.episodic.store.list_memories(layer="episodica"))[0]
    assert int(linha["access_count"]) == 2


# ----------------------------------------------------------------- semantica
async def test_fato_novo_versiona_o_antigo_sem_apagar(memoria: MemorySystem) -> None:
    antigo = await memoria.semantic.remember("mora em Sao Paulo")
    novo = await memoria.semantic.contradict(antigo.id, "mora no Rio de Janeiro")

    assert novo.corrigiu
    vigentes = [linha["content"] for linha in await memoria.semantic.vigentes()]
    assert vigentes == ["mora no Rio de Janeiro"]

    historico = [linha["content"] for linha in await memoria.semantic.historico()]
    assert "mora em Sao Paulo" in historico, "o fato antigo continua no banco"


async def test_ja_sabe_evita_gravar_o_mesmo_fato_toda_noite(memoria: MemorySystem) -> None:
    await memoria.semantic.remember("o contador se chama Ricardo Almeida")
    assert await memoria.semantic.ja_sabe("o contador se chama Ricardo Almeida") is not None
    assert await memoria.semantic.ja_sabe("gosta de cafe sem acucar") is None


async def test_fato_superado_nao_volta_na_busca(memoria: MemorySystem) -> None:
    antigo = await memoria.semantic.remember("trabalha na empresa antiga")
    await memoria.semantic.contradict(antigo.id, "trabalha na Montlux")

    achados = await memoria.semantic.recall("trabalha", limit=5)
    assert all("antiga" not in h.content for h in achados)


# -------------------------------------------------------------- decaimento
def test_recencia_cai_pela_metade_a_cada_meia_vida() -> None:
    agora = datetime.now(UTC)
    hoje = agora.isoformat()
    quinze_dias = (agora - timedelta(days=15)).isoformat()

    assert fator_recencia(hoje, 15, agora=agora) == pytest.approx(1.0, abs=0.01)
    assert fator_recencia(quinze_dias, 15, agora=agora) == pytest.approx(0.5, abs=0.01)


def test_frequencia_cresce_em_log_nao_linear() -> None:
    assert fator_frequencia(0) == 1.0
    assert 1.0 < fator_frequencia(1) < fator_frequencia(10) < 3.0


def test_similaridade_sai_da_distancia_l2_normalizada() -> None:
    assert similaridade_de_distancia(0.0) == 1.0
    assert similaridade_de_distancia(2.0) == 0.0
    assert similaridade_de_distancia(None) == 0.0


async def test_memoria_recente_ganha_da_antiga_com_mesma_similaridade(
    memoria: MemorySystem, store: Store
) -> None:
    """Sem decaimento, o de seis meses atras compete de igual com o de ontem."""
    conteudo = "reuniao sobre o projeto alfa"
    velho = (datetime.now(UTC) - timedelta(days=120)).isoformat(timespec="milliseconds")
    antigo_id = await store.insert_memory(
        layer="episodica", content=conteudo, source="t", created_at=velho
    )
    await store.upsert_vector(antigo_id, await memoria.embedder.embed_one(conteudo))
    await memoria.episodic.record(conteudo)

    achados = await memoria.episodic.recall("projeto alfa", limit=2)
    assert len(achados) == 2
    assert achados[0].created_at > achados[1].created_at
    assert achados[0].score > achados[1].score


# ------------------------------------------------------------------ contexto
async def test_context_for_monta_bloco_com_perfil_e_memoria(memoria: MemorySystem) -> None:
    await memoria.start()
    await memoria.profile.update_section("Pessoas importantes", "- Ricardo, contador")
    await memoria.semantic.remember("prefere cafe sem acucar")

    contexto = await memoria.context_for("cafe")
    assert "<perfil>" in contexto and "Ricardo" in contexto
    assert "<memoria>" in contexto and "cafe sem acucar" in contexto


async def test_context_for_vazio_quando_nao_ha_nada(memoria: MemorySystem) -> None:
    assert await memoria.context_for("assunto totalmente inedito xyzq") == ""


# ------------------------------------------------------------- embeddings
async def test_embedder_de_hashing_e_deterministico(settings: Settings) -> None:
    embedder = HashingEmbedder(settings.embedding_dim)
    a = await embedder.embed_one("o contador chama Ricardo")
    b = await embedder.embed_one("o contador chama Ricardo")
    assert a == b
    assert sum(x * x for x in a) == pytest.approx(1.0, abs=1e-6)


async def test_troca_de_dimensao_e_denunciada(store: Store, settings: Settings) -> None:
    """Trocar de modelo sem reindexar da resultado errado em silencio."""
    assert await conferir_dimensao(store, HashingEmbedder(settings.embedding_dim)) is None
    aviso = await conferir_dimensao(store, HashingEmbedder(384))
    assert aviso is not None and "reindexe" in aviso.lower()


async def test_reindexar_regrava_os_vetores(memoria: MemorySystem) -> None:
    await memoria.semantic.remember("um fato qualquer")
    await memoria.episodic.record("um episodio qualquer")
    assert await memoria.reindexar() == 2


def test_memoria_de_trabalho_guarda_so_texto() -> None:
    """Guarda de regressao para a ferramenta de visao.

    Hoje ``record_turn`` recebe duas strings e ``messages()`` devolve
    ``content`` como texto, entao imagem nenhuma sobrevive ao fim do turno -
    e por isso um quadro de webcam nao acumula custo entre perguntas.

    Isso e propriedade da arquitetura, nao decisao consciente de ninguem. Um
    refactor razoavel - "guardar os blocos completos para nao perder as
    chamadas de ferramenta" - reintroduziria a acumulacao em silencio, e a
    conta so apareceria na fatura. Este teste transforma esse refactor em
    falha visivel.
    """
    trabalho = WorkingMemory(ttl_minutes=30)
    trabalho.add_exchange("o que voce esta vendo", "Uma caneca azul.")

    for mensagem in trabalho.messages():
        assert isinstance(mensagem["content"], str), (
            "content virou bloco: imagens passariam a acumular entre turnos"
        )
