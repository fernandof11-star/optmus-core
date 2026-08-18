"""Motor de politica.

Cada ferramenta declara um risco e o motor decide, antes da execucao, se ela
roda direto, roda e fica registrada, ou precisa de confirmacao humana.

    LEITURA     consultar agenda, buscar web        executa direto
    ESCRITA     registrar gasto, salvar memoria     executa e registra
    EXTERNO     enviar WhatsApp, publicar post      confirmacao obrigatoria
    DESTRUTIVO  apagar dados, transacao financeira  confirmacao + frase + delay

Por que isto existe antes de existir uma ferramenta perigosa: o momento de
construir o freio e antes do carro andar. Quando a F6 conectar o WhatsApp, o
caminho de confirmacao ja vai estar testado - e nao vai haver a tentacao de
"ligar rapidinho e proteger depois".

O LLM nunca decide sozinho executar acao irreversivel. Ele pede; o motor
autoriza ou exige confirmacao.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from core.config import Settings
from core.logging import get_logger
from memory.store import Store

log = get_logger("security.policy")


class RiskLevel(StrEnum):
    LEITURA = "LEITURA"
    ESCRITA = "ESCRITA"
    EXTERNO = "EXTERNO"
    DESTRUTIVO = "DESTRUTIVO"

    @property
    def ordem(self) -> int:
        return {"LEITURA": 0, "ESCRITA": 1, "EXTERNO": 2, "DESTRUTIVO": 3}[self.value]

    def __ge__(self, outro: object) -> bool:  # type: ignore[override]
        if not isinstance(outro, RiskLevel):
            return NotImplemented
        return self.ordem >= outro.ordem


@dataclass(frozen=True, slots=True)
class Decisao:
    permitido: bool
    exige_confirmacao: bool = False
    exige_frase_codigo: bool = False
    delay_s: float = 0.0
    motivo: str | None = None
    token: str | None = None

    @property
    def executa_agora(self) -> bool:
        return self.permitido and not self.exige_confirmacao


@dataclass(slots=True)
class Pendente:
    """Uma acao aguardando confirmacao humana."""

    token: str
    ferramenta: str
    parametros: dict[str, Any]
    risco: RiskLevel
    resumo: str
    criado_em: datetime = field(default_factory=lambda: datetime.now(UTC))
    correlation_id: str | None = None

    def expirado(self, ttl_s: float) -> bool:
        return (datetime.now(UTC) - self.criado_em).total_seconds() > ttl_s


class PolicyEngine:
    """Autoriza execucao de ferramenta conforme o risco declarado."""

    TTL_CONFIRMACAO_S = 120.0

    def __init__(self, settings: Settings, store: Store) -> None:
        self._settings = settings
        self._store = store
        self._pendentes: dict[str, Pendente] = {}

    async def avaliar(
        self,
        *,
        ferramenta: str,
        risco: RiskLevel,
        parametros: dict[str, Any],
        resumo: str = "",
        correlation_id: str | None = None,
    ) -> Decisao:
        if risco.ordem >= RiskLevel.EXTERNO.ordem:
            excedeu = await self._excedeu_limite()
            if excedeu is not None:
                log.warning("politica.rate_limit", ferramenta=ferramenta, teto=excedeu)
                return Decisao(
                    permitido=False,
                    motivo=(
                        f"teto de {excedeu} acoes externas por hora atingido. "
                        "Impede loop descontrolado."
                    ),
                )

        if risco is RiskLevel.LEITURA or risco is RiskLevel.ESCRITA:
            return Decisao(permitido=True)

        pendente = Pendente(
            token=secrets.token_urlsafe(8),
            ferramenta=ferramenta,
            parametros=parametros,
            risco=risco,
            resumo=resumo or ferramenta,
            correlation_id=correlation_id,
        )
        self._pendentes[pendente.token] = pendente
        log.info(
            "politica.confirmacao_exigida",
            ferramenta=ferramenta,
            risco=risco.value,
            token=pendente.token,
        )

        destrutivo = risco is RiskLevel.DESTRUTIVO
        return Decisao(
            permitido=True,
            exige_confirmacao=True,
            exige_frase_codigo=destrutivo,
            delay_s=self._settings.destructive_delay_s if destrutivo else 0.0,
            motivo=f"acao {risco.value.lower()} exige confirmacao explicita",
            token=pendente.token,
        )

    @property
    def delay_destrutivo_s(self) -> float:
        """Janela cancelavel antes de executar acao destrutiva."""
        return self._settings.destructive_delay_s

    def confirmar(self, token: str, *, frase: str | None = None) -> Pendente:
        """Valida a confirmacao e devolve a acao liberada.

        Lanca ``PermissionError`` em token invalido, expirado ou frase errada -
        e o pendente e descartado de qualquer forma, para nao virar um alvo que
        se pode tentar adivinhar varias vezes.
        """
        pendente = self._pendentes.pop(token, None)
        if pendente is None:
            raise PermissionError("token de confirmacao invalido ou ja usado")
        if pendente.expirado(self.TTL_CONFIRMACAO_S):
            raise PermissionError("confirmacao expirada: peca a acao de novo")

        if pendente.risco is RiskLevel.DESTRUTIVO:
            esperada = self._settings.destructive_passphrase
            if esperada is None:
                raise PermissionError(
                    "acao destrutiva sem OPTMUS_DESTRUCTIVE_PASSPHRASE configurada"
                )
            if frase is None or not secrets.compare_digest(
                frase.strip().lower(), esperada.get_secret_value().strip().lower()
            ):
                raise PermissionError("frase-codigo incorreta")
        return pendente

    def cancelar(self, token: str) -> bool:
        return self._pendentes.pop(token, None) is not None

    def pendentes(self) -> list[dict[str, Any]]:
        return [
            {
                "token": p.token,
                "ferramenta": p.ferramenta,
                "risco": p.risco.value,
                "resumo": p.resumo,
                "criado_em": p.criado_em.isoformat(),
                "expirado": p.expirado(self.TTL_CONFIRMACAO_S),
            }
            for p in self._pendentes.values()
        ]

    def limpar_expirados(self) -> int:
        vencidos = [t for t, p in self._pendentes.items() if p.expirado(self.TTL_CONFIRMACAO_S)]
        for token in vencidos:
            del self._pendentes[token]
        return len(vencidos)

    async def _excedeu_limite(self) -> int | None:
        """Conta acoes externas da ultima hora no log de auditoria."""
        teto = self._settings.external_action_rate_limit
        if teto <= 0:
            return None
        desde = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="milliseconds")
        linha = await self._store.fetchone(
            "SELECT COUNT(*) AS n FROM audit_log "
            "WHERE created_at >= ? AND risk IN ('EXTERNO','DESTRUTIVO') "
            "AND decision IN ('permitido','confirmado')",
            (desde,),
        )
        usados = int(linha["n"]) if linha else 0
        return teto if usados >= teto else None
