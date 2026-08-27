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

import hmac
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from core.config import Settings
from core.logging import get_logger
from memory.store import Store
from security.dispositivos import ORIGEM_DESCONHECIDA, ORIGEM_VOZ, origem_atual

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
    origem: str = ORIGEM_DESCONHECIDA
    """Quem pediu. E contra isto que a confirmacao e conferida depois."""

    def expirado(self, ttl_s: float) -> bool:
        return (datetime.now(UTC) - self.criado_em).total_seconds() > ttl_s

    def confirmavel_por(self, dispositivo: str) -> bool:
        """Quem pode autorizar esta acao.

        A regra e "o mesmo que pediu", com **uma abertura declarada**: pedido
        vindo da voz nao tem dispositivo que assine - o microfone nao produz
        HMAC - e hoje nao existe confirmacao falada, so a tela. Fechar isso
        tornaria toda acao externa pedida por voz impossivel de autorizar, que
        e pior que a abertura. Pedido sem origem identificada cai no mesmo caso.

        A abertura e para dispositivo **registrado**: quem tem so o token da
        API continua de fora. E a auditoria grava qual deles autorizou.
        """
        if self.origem in (ORIGEM_VOZ, ORIGEM_DESCONHECIDA):
            return True
        return hmac.compare_digest(self.origem, dispositivo)


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

        if ferramenta in self._settings.dev_sem_portao and risco is RiskLevel.EXTERNO:
            # Dispensa de confirmacao, decidida pelo usuario e registrada em
            # docs/MODO_DESENVOLVEDOR.md. Vale SO para EXTERNO: DESTRUTIVO
            # continua exigindo frase-codigo, porque o que foi revogado foi o
            # portao do deploy - nao o de apagar historico.
            #
            # A acao continua auditada como qualquer outra. Dispensar o portao
            # nao dispensa o registro; e justamente sem humano no caminho que a
            # trilha passa a ser a unica forma de saber o que aconteceu.
            log.info("politica.portao_dispensado", ferramenta=ferramenta)
            return Decisao(permitido=True, motivo="confirmacao dispensada por decisao do usuario")

        pendente = Pendente(
            token=secrets.token_urlsafe(8),
            ferramenta=ferramenta,
            parametros=parametros,
            risco=risco,
            resumo=resumo or ferramenta,
            correlation_id=correlation_id,
            # Carimbada no nascimento, e nao na confirmacao: depois que a acao
            # esta pendente nao ha mais como saber de onde ela veio.
            origem=origem_atual(),
        )
        self._pendentes[pendente.token] = pendente
        log.info(
            "politica.confirmacao_exigida",
            ferramenta=ferramenta,
            risco=risco.value,
            token=pendente.token,
            origem=pendente.origem,
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

    def confirmar(
        self, token: str, *, frase: str | None = None, dispositivo: str | None = None
    ) -> Pendente:
        """Valida a confirmacao e devolve a acao liberada.

        Lanca ``PermissionError`` em token invalido, expirado, frase errada ou
        dispositivo sem direito - e o pendente e descartado de qualquer forma,
        para nao virar um alvo que se pode tentar adivinhar varias vezes.

        Este metodo responde "voce pode confirmar ISTO?". Ele nao responde
        "voce e quem diz ser" - essa e a pergunta do
        :class:`~security.dispositivos.RegistroDeDispositivos`, e ela ja foi
        respondida quando a execucao chega aqui. Separadas de proposito: uma
        precisa do banco, a outra e pura e da para testar sem I/O.
        """
        pendente = self._pendentes.pop(token, None)
        if pendente is None:
            raise PermissionError("token de confirmacao invalido ou ja usado")
        if pendente.expirado(self.TTL_CONFIRMACAO_S):
            raise PermissionError("confirmacao expirada: peca a acao de novo")

        if dispositivo is not None and not pendente.confirmavel_por(dispositivo):
            log.warning(
                "politica.confirmacao_de_outro_dispositivo",
                ferramenta=pendente.ferramenta,
                pediu=pendente.origem,
                confirmou=dispositivo,
            )
            raise PermissionError(
                "esta acao foi pedida em outro dispositivo e so pode ser "
                "confirmada la"
            )

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

    def pendentes(self, *, para: str | None = None) -> list[dict[str, Any]]:
        """As pendencias que ``para`` pode confirmar.

        Filtrado, e nao a lista toda: um cartao que o dispositivo nao consegue
        autorizar so serve para a pessoa clicar e receber erro - e para mostrar
        a um aparelho o que o outro esta fazendo.
        """
        return [
            {
                "token": p.token,
                "ferramenta": p.ferramenta,
                "risco": p.risco.value,
                "resumo": p.resumo,
                "criado_em": p.criado_em.isoformat(),
                "expirado": p.expirado(self.TTL_CONFIRMACAO_S),
                "origem": p.origem,
            }
            for p in self._pendentes.values()
            if para is None or p.confirmavel_por(para)
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
