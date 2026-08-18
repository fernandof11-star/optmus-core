"""Persistencia do Core: SQLite + extensao sqlite-vec.

Um arquivo, zero servidor, zero Docker. Backup e copiar o arquivo.

Concorrencia: uma unica conexao, todo acesso serializado por um
``asyncio.Lock`` e executado em thread separada (``asyncio.to_thread``),
para nao bloquear o event loop. Um usuario, escrita de baixa frequencia:
serializar e mais simples e mais seguro que um pool.

Degradacao graciosa: se a extensao vetorial nao carregar, o Store sobe
mesmo assim, marca ``vector_search_available = False`` e a migration
vetorial fica pendente ate a extensao existir. O Optmus nunca morre por
dependencia - ele reduz capacidade e avisa.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Self

from core.logging import get_logger

log = get_logger("memory.store")

SQLParams = Sequence[Any] | dict[str, Any]

SCHEMA_TABLE: Final[str] = "schema_migrations"


class StoreError(RuntimeError):
    """Falha de persistencia."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    requires_vec: bool = False


@dataclass(slots=True)
class StoreStatus:
    """Retrato do estado da persistencia, exposto no /health."""

    connected: bool
    path: str
    vector_search_available: bool
    embedding_dim: int
    applied_migrations: list[int] = field(default_factory=list)
    pending_migrations: list[int] = field(default_factory=list)
    vec_error: str | None = None


# --------------------------------------------------------------------------
# Migrations. Nunca edite uma migration ja aplicada: acrescente outra.
# --------------------------------------------------------------------------
def _migrations(embedding_dim: int) -> tuple[Migration, ...]:
    return (
        Migration(
            version=1,
            name="init",
            statements=(
                # ---- log de eventos: fonte de verdade do sistema ----
                """
                CREATE TABLE IF NOT EXISTS events (
                    id             TEXT PRIMARY KEY,
                    type           TEXT NOT NULL,
                    source         TEXT NOT NULL,
                    payload        TEXT NOT NULL DEFAULT '{}',
                    correlation_id TEXT,
                    created_at     TEXT NOT NULL
                )
                """,
                "CREATE INDEX IF NOT EXISTS idx_events_created ON events (created_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_events_type ON events (type, created_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_events_corr ON events (correlation_id)",
                # ---- memoria: episodica / semantica / procedural ----
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    layer          TEXT NOT NULL
                                   CHECK (layer IN ('episodica','semantica','procedural')),
                    content        TEXT NOT NULL,
                    source         TEXT NOT NULL,
                    confidence     REAL NOT NULL DEFAULT 0.5
                                   CHECK (confidence >= 0.0 AND confidence <= 1.0),
                    metadata       TEXT NOT NULL DEFAULT '{}',
                    created_at     TEXT NOT NULL,
                    last_access    TEXT NOT NULL,
                    access_count   INTEGER NOT NULL DEFAULT 0,
                    -- fato novo NAO sobrescreve o antigo: marca como superado
                    superseded_by  INTEGER REFERENCES memories (id),
                    superseded_at  TEXT
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_memories_layer
                    ON memories (layer, created_at DESC)
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_memories_vigentes
                    ON memories (layer, last_access DESC) WHERE superseded_by IS NULL
                """,
                # ---- frota de dispositivos ----
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id           TEXT PRIMARY KEY,
                    apelido      TEXT NOT NULL UNIQUE,
                    plataforma   TEXT NOT NULL CHECK (plataforma IN ('android','ios')),
                    conexao      TEXT NOT NULL CHECK (conexao IN ('usb','tcp')),
                    status       TEXT NOT NULL DEFAULT 'offline'
                                 CHECK (status IN ('online','offline','ocupado')),
                    capacidades  TEXT NOT NULL DEFAULT '[]',
                    grupos       TEXT NOT NULL DEFAULT '[]',
                    endereco     TEXT,
                    last_seen    TEXT,
                    created_at   TEXT NOT NULL
                )
                """,
                # ---- auditoria append-only (secao 9) ----
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at     TEXT NOT NULL,
                    actor          TEXT NOT NULL,
                    origin_command TEXT,
                    tool           TEXT NOT NULL,
                    params         TEXT NOT NULL DEFAULT '{}',
                    risk           TEXT NOT NULL CHECK (
                                       risk IN ('LEITURA','ESCRITA','EXTERNO','DESTRUTIVO')
                                   ),
                    decision       TEXT NOT NULL CHECK (
                                       decision IN
                                           ('permitido','negado','confirmado','cancelado')
                                   ),
                    result         TEXT,
                    correlation_id TEXT
                )
                """,
                "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log (created_at DESC)",
                # append-only de verdade: o banco recusa alteracao e remocao
                """
                CREATE TRIGGER IF NOT EXISTS audit_log_no_update
                BEFORE UPDATE ON audit_log
                BEGIN
                    SELECT RAISE(ABORT, 'audit_log e append-only');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
                BEFORE DELETE ON audit_log
                BEGIN
                    SELECT RAISE(ABORT, 'audit_log e append-only');
                END
                """,
            ),
        ),
        Migration(
            version=2,
            name="vetores",
            requires_vec=True,
            statements=(
                # Trocar embedding_dim depois exige reindexar tudo.
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors USING vec0 (
                    memory_id INTEGER PRIMARY KEY,
                    embedding FLOAT[{embedding_dim}]
                )
                """,
            ),
        ),
        Migration(
            version=3,
            name="consolidacao",
            statements=(
                # Chave/valor do proprio banco: dimensao dos vetores, provedor
                # de embedding, marca do ultimo consolidador. Sem isso, trocar
                # de modelo de embedding corrompe a busca em silencio.
                """
                CREATE TABLE IF NOT EXISTS meta (
                    chave      TEXT PRIMARY KEY,
                    valor      TEXT NOT NULL,
                    atualizado TEXT NOT NULL
                )
                """,
                # O consolidador noturno so processa episodio ainda nao digerido.
                "ALTER TABLE memories ADD COLUMN consolidated_at TEXT",
                """
                CREATE INDEX IF NOT EXISTS idx_memories_pendentes
                    ON memories (layer, created_at) WHERE consolidated_at IS NULL
                """,
            ),
        ),
    )


class Store:
    """Fachada assincrona sobre o SQLite do Core."""

    def __init__(self, db_path: Path, *, embedding_dim: int = 1024) -> None:
        self._db_path = db_path
        self._embedding_dim = embedding_dim
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._vector_ok = False
        self._vec_error: str | None = None
        self._applied: list[int] = []
        self._pending: list[int] = []

    # ------------------------------------------------------------- ciclo
    async def connect(self) -> Self:
        if self._conn is not None:
            return self
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await asyncio.to_thread(self._open)
        log.info(
            "store.conectado",
            path=str(self._db_path),
            vector_search=self._vector_ok,
            vec_error=self._vec_error,
        )
        return self

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        self._load_vec(conn)
        return conn

    def _load_vec(self, conn: sqlite3.Connection) -> None:
        """Carrega sqlite-vec. Falha aqui nao derruba o Store."""
        try:
            import sqlite_vec

            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            (versao,) = conn.execute("SELECT vec_version()").fetchone()
            self._vector_ok = True
            self._vec_error = None
            log.debug("store.sqlite_vec_carregado", versao=versao)
        except Exception as exc:  # noqa: BLE001 - degradacao graciosa e intencional
            self._vector_ok = False
            self._vec_error = f"{type(exc).__name__}: {exc}"
            log.warning(
                "store.sqlite_vec_indisponivel",
                erro=self._vec_error,
                impacto="busca vetorial desligada; memoria semantica cai para busca textual",
            )

    async def close(self) -> None:
        if self._conn is None:
            return
        conn, self._conn = self._conn, None
        async with self._lock:
            await asyncio.to_thread(conn.close)
        log.info("store.desconectado", path=str(self._db_path))

    async def __aenter__(self) -> Self:
        return await self.connect()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ---------------------------------------------------------- primitivas
    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError("Store nao conectado: chame connect() antes")
        return self._conn

    async def execute(self, sql: str, params: SQLParams = ()) -> int:
        """Executa uma escrita e devolve o numero de linhas afetadas."""
        conn = self._require_conn()

        def _run() -> int:
            cur = conn.execute(sql, params)
            try:
                return cur.rowcount
            finally:
                cur.close()

        async with self._lock:
            return await asyncio.to_thread(_run)

    async def executemany(self, sql: str, seq: Iterable[SQLParams]) -> None:
        conn = self._require_conn()
        linhas = list(seq)

        def _run() -> None:
            with conn:
                conn.executemany(sql, linhas)

        async with self._lock:
            await asyncio.to_thread(_run)

    async def fetchall(self, sql: str, params: SQLParams = ()) -> list[dict[str, Any]]:
        conn = self._require_conn()

        def _run() -> list[dict[str, Any]]:
            cur = conn.execute(sql, params)
            try:
                return [dict(row) for row in cur.fetchall()]
            finally:
                cur.close()

        async with self._lock:
            return await asyncio.to_thread(_run)

    async def fetchone(self, sql: str, params: SQLParams = ()) -> dict[str, Any] | None:
        linhas = await self.fetchall(sql, params)
        return linhas[0] if linhas else None

    # ---------------------------------------------------------- migrations
    async def migrate(self) -> StoreStatus:
        conn = self._require_conn()

        def _run() -> tuple[list[int], list[int]]:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {SCHEMA_TABLE} (
                    version    INTEGER PRIMARY KEY,
                    name       TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            aplicadas = {
                int(row["version"]) for row in conn.execute(f"SELECT version FROM {SCHEMA_TABLE}")
            }
            pendentes: list[int] = []
            for mig in _migrations(self._embedding_dim):
                if mig.version in aplicadas:
                    continue
                if mig.requires_vec and not self._vector_ok:
                    # Nao registra: aplica sozinha quando a extensao existir.
                    pendentes.append(mig.version)
                    log.warning(
                        "store.migration_adiada",
                        version=mig.version,
                        name=mig.name,
                        motivo="sqlite-vec indisponivel",
                    )
                    continue
                with conn:  # transacao por migration
                    for stmt in mig.statements:
                        conn.execute(stmt)
                    conn.execute(
                        f"INSERT INTO {SCHEMA_TABLE} (version, name, applied_at) VALUES (?,?,?)",
                        (mig.version, mig.name, _agora()),
                    )
                aplicadas.add(mig.version)
                log.info("store.migration_aplicada", version=mig.version, name=mig.name)
            return sorted(aplicadas), pendentes

        async with self._lock:
            self._applied, self._pending = await asyncio.to_thread(_run)
        return self.status()

    def status(self) -> StoreStatus:
        return StoreStatus(
            connected=self._conn is not None,
            path=str(self._db_path),
            vector_search_available=self._vector_ok,
            embedding_dim=self._embedding_dim,
            applied_migrations=list(self._applied),
            pending_migrations=list(self._pending),
            vec_error=self._vec_error,
        )

    @property
    def vector_search_available(self) -> bool:
        return self._vector_ok

    # ------------------------------------------------------------- eventos
    async def insert_event(
        self,
        *,
        event_id: str,
        type_: str,
        source: str,
        payload: dict[str, Any],
        correlation_id: str | None,
        created_at: str,
    ) -> None:
        await self.execute(
            """
            INSERT OR IGNORE INTO events
                (id, type, source, payload, correlation_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_id, type_, source, json.dumps(payload, ensure_ascii=False),
             correlation_id, created_at),
        )

    async def recent_events(
        self, *, limit: int = 50, type_: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events"
        params: list[Any] = []
        if type_:
            sql += " WHERE type = ?"
            params.append(type_)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        linhas = await self.fetchall(sql, params)
        for linha in linhas:
            linha["payload"] = json.loads(linha["payload"])
        return linhas

    async def count_events(self) -> int:
        linha = await self.fetchone("SELECT COUNT(*) AS n FROM events")
        return int(linha["n"]) if linha else 0

    async def purge_expired_events(self, retention_days: int) -> int:
        """Expurga eventos antigos. ``0`` desliga a retencao."""
        if retention_days <= 0:
            return 0
        removidos = await self.execute(
            "DELETE FROM events WHERE created_at < datetime('now', ?)",
            (f"-{retention_days} days",),
        )
        if removidos:
            log.info("store.eventos_expurgados", removidos=removidos, retencao_dias=retention_days)
        return removidos

    # ----------------------------------------------------------- auditoria
    async def append_audit(
        self,
        *,
        actor: str,
        tool: str,
        risk: str,
        decision: str,
        params: dict[str, Any] | None = None,
        origin_command: str | None = None,
        result: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        """Escreve no log de auditoria. Tabela append-only por trigger."""
        await self.execute(
            """
            INSERT INTO audit_log
                (created_at, actor, origin_command, tool, params, risk, decision,
                 result, correlation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _agora(),
                actor,
                origin_command,
                tool,
                json.dumps(params or {}, ensure_ascii=False),
                risk,
                decision,
                result,
                correlation_id,
            ),
        )


    # ---------------------------------------------------------------- meta
    async def meta_get(self, chave: str) -> str | None:
        linha = await self.fetchone("SELECT valor FROM meta WHERE chave = ?", (chave,))
        return str(linha["valor"]) if linha else None

    async def meta_set(self, chave: str, valor: str) -> None:
        await self.execute(
            """
            INSERT INTO meta (chave, valor, atualizado) VALUES (?, ?, ?)
            ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor,
                                             atualizado = excluded.atualizado
            """,
            (chave, valor, _agora()),
        )

    # ------------------------------------------------------------- memoria
    async def insert_memory(
        self,
        *,
        layer: str,
        content: str,
        source: str,
        confidence: float = 0.5,
        metadata: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> int:
        agora = created_at or _agora()
        conn = self._require_conn()

        def _run() -> int:
            cur = conn.execute(
                """
                INSERT INTO memories
                    (layer, content, source, confidence, metadata,
                     created_at, last_access, access_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (layer, content, source, confidence,
                 json.dumps(metadata or {}, ensure_ascii=False), agora, agora),
            )
            try:
                return int(cur.lastrowid or 0)
            finally:
                cur.close()

        async with self._lock:
            return await asyncio.to_thread(_run)

    async def upsert_vector(self, memory_id: int, embedding: Sequence[float]) -> bool:
        """Grava o vetor da memoria. ``False`` quando a busca vetorial esta fora."""
        if not self._vector_ok:
            return False
        if len(embedding) != self._embedding_dim:
            raise StoreError(
                f"dimensao incompativel: vetor tem {len(embedding)}, "
                f"banco espera {self._embedding_dim}"
            )
        import sqlite_vec

        dados = sqlite_vec.serialize_float32(list(embedding))
        await self.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (memory_id,))
        await self.execute(
            "INSERT INTO memory_vectors (memory_id, embedding) VALUES (?, ?)",
            (memory_id, dados),
        )
        return True

    async def vector_search(
        self, embedding: Sequence[float], *, k: int = 20, layers: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """KNN sobre os vetores, ja unido aos campos da memoria.

        A tabela vec0 usa distancia L2. Como todo vetor entra normalizado,
        ``dist^2 = 2 - 2*cos``, entao a similaridade sai de volta exata.
        """
        if not self._vector_ok:
            return []
        import sqlite_vec

        filtro = ""
        params: list[Any] = [sqlite_vec.serialize_float32(list(embedding)), k]
        if layers:
            filtro = f" AND m.layer IN ({','.join('?' * len(layers))})"
            params.extend(layers)

        return await self.fetchall(
            f"""
            SELECT m.*, v.distance AS distance
            FROM (
                SELECT memory_id, distance FROM memory_vectors
                WHERE embedding MATCH ? AND k = ?
            ) AS v
            JOIN memories m ON m.id = v.memory_id
            WHERE m.superseded_by IS NULL{filtro}
            ORDER BY v.distance
            """,
            params,
        )

    async def lexical_search(
        self, termos: Sequence[str], *, k: int = 20, layers: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """Degradacao quando nao ha vetores: casamento por termo no conteudo."""
        if not termos:
            return []
        onde = " OR ".join("lower(content) LIKE ?" for _ in termos)
        params: list[Any] = [f"%{t.lower()}%" for t in termos]
        filtro = ""
        if layers:
            filtro = f" AND layer IN ({','.join('?' * len(layers))})"
            params.extend(layers)
        params.append(k)
        return await self.fetchall(
            f"SELECT * FROM memories WHERE superseded_by IS NULL AND ({onde}){filtro} "
            f"ORDER BY last_access DESC LIMIT ?",
            params,
        )

    async def touch_memories(self, ids: Sequence[int]) -> None:
        """Marca acesso: alimenta o fator de frequencia do score de relevancia."""
        if not ids:
            return
        marcas = ",".join("?" * len(ids))
        await self.execute(
            f"UPDATE memories SET last_access = ?, access_count = access_count + 1 "
            f"WHERE id IN ({marcas})",
            [_agora(), *ids],
        )

    async def supersede_memory(self, antigo: int, novo: int) -> None:
        """Fato novo nao apaga o antigo: versiona. Historico e valioso."""
        await self.execute(
            "UPDATE memories SET superseded_by = ?, superseded_at = ? WHERE id = ?",
            (novo, _agora(), antigo),
        )

    async def get_memory(self, memory_id: int) -> dict[str, Any] | None:
        return await self.fetchone("SELECT * FROM memories WHERE id = ?", (memory_id,))

    async def list_memories(
        self,
        *,
        layer: str | None = None,
        vigentes: bool = True,
        pendentes_de_consolidacao: bool = False,
        desde: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clausulas: list[str] = []
        params: list[Any] = []
        if layer:
            clausulas.append("layer = ?")
            params.append(layer)
        if vigentes:
            clausulas.append("superseded_by IS NULL")
        if pendentes_de_consolidacao:
            clausulas.append("consolidated_at IS NULL")
        if desde:
            clausulas.append("created_at >= ?")
            params.append(desde)
        onde = f" WHERE {' AND '.join(clausulas)}" if clausulas else ""
        params.append(limit)
        return await self.fetchall(
            f"SELECT * FROM memories{onde} ORDER BY created_at DESC LIMIT ?", params
        )

    async def mark_consolidated(self, ids: Sequence[int]) -> None:
        if not ids:
            return
        marcas = ",".join("?" * len(ids))
        await self.execute(
            f"UPDATE memories SET consolidated_at = ? WHERE id IN ({marcas})",
            [_agora(), *ids],
        )

    async def count_memories(self) -> dict[str, int]:
        linhas = await self.fetchall(
            "SELECT layer, COUNT(*) AS n FROM memories WHERE superseded_by IS NULL GROUP BY layer"
        )
        contagem = {linha["layer"]: int(linha["n"]) for linha in linhas}
        superadas = await self.fetchone(
            "SELECT COUNT(*) AS n FROM memories WHERE superseded_by IS NOT NULL"
        )
        contagem["superadas"] = int(superadas["n"]) if superadas else 0
        return contagem


def _agora() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")
