"""Persistencia: migrations, auditoria append-only, retencao e vetores."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.config import Settings
from memory.store import Store, StoreError


async def test_migrations_criam_as_tabelas(store: Store) -> None:
    linhas = await store.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    nomes = {linha["name"] for linha in linhas}
    assert {"events", "memories", "devices", "audit_log", "schema_migrations"} <= nomes


async def test_migrate_e_idempotente(store: Store) -> None:
    antes = store.status().applied_migrations
    await store.migrate()
    assert store.status().applied_migrations == antes


async def test_store_nao_conectado_falha_explicitamente(tmp_path: Path) -> None:
    st = Store(tmp_path / "x.db")
    with pytest.raises(StoreError):
        await st.fetchall("SELECT 1")


async def test_audit_log_e_append_only(store: Store) -> None:
    await store.append_audit(
        actor="voz",
        tool="enviar_mensagem",
        risk="EXTERNO",
        decision="confirmado",
        params={"destino": "+55..."},
        origin_command="manda mensagem pro Joao",
        result="enviado",
    )
    linha = await store.fetchone("SELECT * FROM audit_log")
    assert linha is not None and linha["risk"] == "EXTERNO"

    with pytest.raises(sqlite3.IntegrityError):
        await store.execute("UPDATE audit_log SET result = 'adulterado'")
    with pytest.raises(sqlite3.IntegrityError):
        await store.execute("DELETE FROM audit_log")


async def test_risco_invalido_e_rejeitado(store: Store) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await store.append_audit(actor="x", tool="y", risk="TALVEZ", decision="permitido")


async def test_memoria_versiona_em_vez_de_sobrescrever(store: Store) -> None:
    async def gravar(conteudo: str) -> int:
        await store.execute(
            """
            INSERT INTO memories (layer, content, source, confidence, created_at, last_access)
            VALUES ('semantica', ?, 'conversa', 0.8, datetime('now'), datetime('now'))
            """,
            (conteudo,),
        )
        linha = await store.fetchone("SELECT MAX(id) AS id FROM memories")
        assert linha is not None
        return int(linha["id"])

    antigo = await gravar("mora em Sao Paulo")
    novo = await gravar("mora no Rio de Janeiro")
    await store.execute(
        "UPDATE memories SET superseded_by = ?, superseded_at = datetime('now') WHERE id = ?",
        (novo, antigo),
    )

    vigentes = await store.fetchall("SELECT content FROM memories WHERE superseded_by IS NULL")
    assert [linha["content"] for linha in vigentes] == ["mora no Rio de Janeiro"]
    # o fato antigo continua no banco: historico e valioso
    assert await store.fetchone("SELECT id FROM memories WHERE id = ?", (antigo,)) is not None


async def test_purge_respeita_a_retencao(store: Store) -> None:
    await store.insert_event(
        event_id="antigo",
        type_="sistema.teste",
        source="teste",
        payload={},
        correlation_id=None,
        created_at="2000-01-01T00:00:00.000+00:00",
    )
    await store.insert_event(
        event_id="novo",
        type_="sistema.teste",
        source="teste",
        payload={},
        correlation_id=None,
        created_at="2999-01-01T00:00:00.000+00:00",
    )

    assert await store.purge_expired_events(0) == 0
    assert await store.purge_expired_events(30) == 1
    restantes = {linha["id"] for linha in await store.recent_events(limit=10)}
    assert restantes == {"novo"}


async def test_busca_vetorial_ou_degradacao_declarada(store: Store, settings: Settings) -> None:
    """Com sqlite-vec presente a tabela vetorial existe; sem ela, fica pendente."""
    status = store.status()
    if status.vector_search_available:
        assert 2 in status.applied_migrations
        linha = await store.fetchone(
            "SELECT name FROM sqlite_master WHERE name = 'memory_vectors'"
        )
        assert linha is not None
    else:
        assert 2 in status.pending_migrations
        assert status.vec_error is not None
