"""Camada 1 - memoria de trabalho.

O contexto da conversa em andamento. Volatil, em RAM, com TTL de 30 minutos:
passou o tempo sem falar, a conversa acabou e a proxima comeca limpa.

Por que TTL e nao "ate reiniciar": um assistente sempre ligado acumula contexto
o dia inteiro. Sem expiracao, a pergunta das 22h chega ao modelo carregando a
conversa do cafe da manha - caro, confuso e quase sempre irrelevante. O que
importava daquela conversa nao se perde: virou episodio no SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from core.logging import get_logger

log = get_logger("memory.working")


@dataclass(slots=True)
class Turn:
    role: str
    content: str
    quando: datetime = field(default_factory=lambda: datetime.now(UTC))


class WorkingMemory:
    """Janela deslizante da conversa atual."""

    def __init__(self, *, ttl_minutes: int = 30, max_turns: int = 12) -> None:
        self._ttl = timedelta(minutes=ttl_minutes)
        self._max_turns = max_turns
        self._turns: list[Turn] = []
        self._ultimo: datetime | None = None
        self.expiracoes = 0

    @property
    def expirada(self) -> bool:
        if self._ultimo is None:
            return False
        return datetime.now(UTC) - self._ultimo > self._ttl

    def _expirar_se_preciso(self) -> None:
        if self.expirada:
            self.expiracoes += 1
            log.info("memoria.trabalho_expirada", turnos_descartados=len(self._turns))
            self._turns.clear()

    def add(self, role: str, content: str) -> None:
        self._expirar_se_preciso()
        if not content.strip():
            return
        self._turns.append(Turn(role=role, content=content))
        self._ultimo = datetime.now(UTC)
        excedente = len(self._turns) - self._max_turns * 2
        if excedente > 0:
            del self._turns[:excedente]

    def add_exchange(self, usuario: str, assistente: str) -> None:
        if not assistente.strip():
            return
        self.add("user", usuario)
        self.add("assistant", assistente)

    def messages(self) -> list[dict[str, Any]]:
        """Historico no formato que o cliente de LLM espera."""
        self._expirar_se_preciso()
        return [{"role": t.role, "content": t.content} for t in self._turns]

    def reset(self) -> None:
        self._turns.clear()
        self._ultimo = None

    def __len__(self) -> int:
        return len(self._turns)

    def stats(self) -> dict[str, Any]:
        return {
            "turnos": len(self._turns),
            "expirada": self.expirada,
            "expiracoes": self.expiracoes,
            "ultimo_uso": self._ultimo.isoformat() if self._ultimo else None,
        }
