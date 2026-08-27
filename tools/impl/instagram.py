"""Ferramentas de leitura do Instagram.

Risco **LEITURA** nas duas: nada sai da conta, nada e publicado, nada e
seguido. Por isso nao passam pelo portao de confirmacao - exigir autorizacao
para ler o proprio perfil ensinaria a confirmar por reflexo, e o reflexo e o
que quebra o portao quando ele importar.

## O que estas ferramentas nao fazem, e nao e por preguica

Nao dizem **quem** te seguiu, nao mostram foto nem nome de seguidor novo, e nao
seguem ninguem. O caminho oficial nao tem esses endpoints: `followers_count` e
um numero, nao ha webhook de seguidor novo, e follow/unfollow nao existe na API.
O texto devolvido ao modelo diz isso explicitamente para ele nao preencher a
lacuna sozinho.
"""

from __future__ import annotations

from typing import Any, ClassVar

from core.config import Settings
from core.logging import get_logger
from integrations.instagram import InstagramClient, InstagramError
from memory.store import Store
from security.policy import RiskLevel
from tools.registry import Tool, ToolResult

log = get_logger("tools.instagram")

# O que aparece no lugar de um numero que a Meta nao devolveu. Zero seria uma
# afirmacao - "seu alcance foi zero" - que ninguem mediu.
AUSENTE = "—"

ROTULOS: dict[str, str] = {
    "reach": "alcance",
    "views": "visualizacoes",
    "total_interactions": "interacoes",
    "follows_and_unfollows": "seguidas e deixadas de seguir",
}


def _numero(valor: int | None) -> str:
    return AUSENTE if valor is None else str(valor)


class _FerramentaInstagram(Tool):
    """Base com a configuracao e o cliente compartilhados."""

    risk = RiskLevel.LEITURA
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(
        self, settings: Settings, store: Store, client: InstagramClient | None = None
    ) -> None:
        self._settings = settings
        self.client = client or InstagramClient(settings, store)

    async def available(self) -> bool:
        if not self.client.configurado:
            log.info(
                "instagram.indisponivel",
                motivo="falta OPTMUS_INSTAGRAM_TOKEN ou OPTMUS_INSTAGRAM_ACCOUNT_ID",
                acao="docs/INSTAGRAM.md",
            )
            return False
        return True


class InstagramResumoTool(_FerramentaInstagram):
    """Perfil, variacao de seguidores e metricas do dia."""

    name = "instagram_resumo"
    description = (
        "Le o estado da conta de Instagram do usuario: numero de seguidores, "
        "quantos entraram desde a ultima checagem, e as metricas do dia "
        "(alcance, visualizacoes, interacoes). NAO diz QUEM seguiu - a API "
        "oficial do Instagram nao expoe a lista de seguidores, so o total. "
        "Nao publica nada e nao segue ninguem."
    )

    async def execute(self, **_: Any) -> ToolResult:
        try:
            # Manutencao primeiro: se o token estiver perto do fim, esta e a
            # oportunidade de estender. Falha aqui nao interrompe a leitura.
            renovacao = await self.client.renovar_se_preciso()
            perfil = await self.client.perfil()
            seguidores = int(perfil.get("followers_count") or 0)
            variacao = await self.client.variacao_de_seguidores(seguidores)
            metricas = await self.client.insights()
        except InstagramError as exc:
            log.warning("instagram.resumo_falhou", erro=str(exc))
            return ToolResult.erro(f"Nao consegui ler o Instagram: {exc}")

        linhas = [
            f"Instagram @{perfil.get('username', AUSENTE)}",
            f"seguidores: {seguidores}",
        ]
        if variacao["delta"] is None:
            # Primeira leitura. Dizer "+0" afirmaria que ninguem entrou, e a
            # verdade e que nao havia com o que comparar.
            linhas.append("variacao: primeira leitura, sem base de comparacao")
        else:
            sinal = "+" if variacao["delta"] >= 0 else ""
            linhas.append(f"variacao: {sinal}{variacao['delta']} desde {variacao['desde']}")
        linhas.append(f"segue: {perfil.get('follows_count', AUSENTE)}")
        linhas.append(f"publicacoes: {perfil.get('media_count', AUSENTE)}")
        linhas += [f"{ROTULOS.get(k, k)}: {_numero(v)}" for k, v in metricas.items()]
        linhas.append(
            "(a API oficial nao informa quem seguiu, nem permite seguir de volta)"
        )

        dias = await self.client.dias_restantes()
        if dias is not None and dias <= 10:
            linhas.append(f"AVISO: o token do Instagram vence em {dias} dias.")

        return ToolResult(
            content="\n".join(linhas),
            metadata={
                "seguidores": seguidores,
                "delta": variacao["delta"],
                "metricas": metricas,
                "token_dias_restantes": dias,
                "token_renovado": renovacao.get("renovado"),
            },
        )


class InstagramComentariosTool(_FerramentaInstagram):
    """Comentarios recentes nas ultimas publicacoes."""

    name = "instagram_comentarios"
    description = (
        "Le os comentarios recentes nas ultimas publicacoes do Instagram do "
        "usuario, com autor, texto e data. Use quando ele perguntar se tem "
        "comentario novo ou o que andaram comentando. So le - nao responde "
        "nem apaga comentario."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "publicacoes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "Quantas publicacoes recentes varrer. Padrao 3.",
            }
        },
        "required": [],
        "additionalProperties": False,
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        quantas = int(kwargs.get("publicacoes") or 3)
        try:
            await self.client.renovar_se_preciso()
            midias = await self.client.midias(limite=quantas)
        except InstagramError as exc:
            log.warning("instagram.comentarios_falhou", erro=str(exc))
            return ToolResult.erro(f"Nao consegui ler o Instagram: {exc}")

        if not midias:
            return ToolResult(content="Nenhuma publicacao encontrada na conta.")

        blocos: list[str] = []
        total = 0
        for midia in midias:
            try:
                comentarios = await self.client.comentarios(str(midia.get("id")))
            except InstagramError as exc:
                # Uma publicacao que falha nao derruba as outras: meia leitura
                # com o buraco marcado vale mais que erro total.
                blocos.append(f"- {midia.get('permalink', '?')}: nao consegui ler ({exc})")
                continue

            if not comentarios:
                continue
            total += len(comentarios)
            cabeca = (midia.get("caption") or "sem legenda").split("\n")[0][:60]
            blocos.append(f"- \"{cabeca}\" ({midia.get('permalink', '')}):")
            blocos += [
                f"    @{c.get('username', AUSENTE)}: {c.get('text', '')} "
                f"[{str(c.get('timestamp', ''))[:10]}]"
                for c in comentarios
            ]

        if not blocos:
            return ToolResult(
                content=f"Nenhum comentario nas ultimas {len(midias)} publicacoes.",
                metadata={"comentarios": 0, "publicacoes": len(midias)},
            )

        return ToolResult(
            content="\n".join(blocos),
            metadata={"comentarios": total, "publicacoes": len(midias)},
        )
