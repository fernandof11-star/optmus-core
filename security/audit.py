"""Log de auditoria append-only.

Toda execucao de ferramenta - autorizada, negada ou confirmada - vira uma linha
em ``audit_log``. A tabela recusa UPDATE e DELETE por trigger do SQLite: nao e
convencao de codigo, e o banco que se recusa.

Por que importa: o dia em que o Optmus fizer algo inesperado, a pergunta vai ser
"o que ele executou, com que parametro, autorizado por quem". Sem trilha, a
resposta e um encolher de ombros.

Parametros sao **redigidos** antes de gravar. O log e para reconstruir o que
aconteceu, nao para virar um segundo lugar onde a senha do usuario mora.
"""

from __future__ import annotations

from typing import Any, Final

from core.logging import get_logger
from memory.store import Store
from security.policy import RiskLevel

log = get_logger("security.audit")

CAMPOS_SENSIVEIS: Final[frozenset[str]] = frozenset(
    """senha password token secret api_key chave autorizacao authorization
    passphrase frase_codigo credencial""".split()
)
REDIGIDO: Final[str] = "***"
MAX_VALOR = 500


class AuditLog:
    """Escrita na trilha de auditoria."""

    def __init__(self, store: Store) -> None:
        self._store = store

    async def registrar(
        self,
        *,
        ferramenta: str,
        risco: RiskLevel,
        decisao: str,
        parametros: dict[str, Any] | None = None,
        ator: str = "agente",
        comando_origem: str | None = None,
        resultado: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        await self._store.append_audit(
            actor=ator,
            tool=ferramenta,
            risk=risco.value,
            decision=decisao,
            params=redigir(parametros or {}),
            origin_command=comando_origem,
            result=(resultado or "")[:MAX_VALOR] or None,
            correlation_id=correlation_id,
        )

    async def recentes(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._store.fetchall(
            "SELECT * FROM audit_log ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        )


def redigir(valor: Any, *, _profundidade: int = 0) -> Any:
    """Substitui campo sensivel por ``***`` e trunca valor gigante."""
    if _profundidade > 6:
        return "<profundo demais>"
    if isinstance(valor, dict):
        return {
            chave: (
                REDIGIDO
                if any(marca in str(chave).lower() for marca in CAMPOS_SENSIVEIS)
                else redigir(item, _profundidade=_profundidade + 1)
            )
            for chave, item in valor.items()
        }
    if isinstance(valor, list):
        return [redigir(item, _profundidade=_profundidade + 1) for item in valor[:50]]
    if isinstance(valor, str) and len(valor) > MAX_VALOR:
        return valor[:MAX_VALOR] + "...(truncado)"
    return valor
