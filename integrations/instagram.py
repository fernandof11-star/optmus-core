"""Cliente da Instagram Platform API (caminho oficial).

Usa o caminho **Instagram API with Instagram Login** (`graph.instagram.com`),
nao o com Facebook Login: aquele exige uma Pagina do Facebook com a conta
vinculada, e essa e uma peca a mais para quebrar sem dar nada em troca aqui.

## O prazo de validade que mata a integracao

O token longo vale **60 dias**. Renova-lo exige que ele tenha pelo menos 24 h de
idade e **ainda nao tenha expirado** - token vencido nao ressuscita, so refazendo
o OAuth na mao. Duas consequencias praticas:

- Sem renovacao automatica, o Instagram morre no dia 61 sem aviso.
- Com o Core desligado 60 dias corridos, morre igual - e nenhum codigo aqui
  pode evitar isso. Por isso ``dias_restantes()`` existe: o ``/health`` mostra o
  prazo enquanto ainda da tempo de agir.

A renovacao e **preguicosa**, feita no uso, e nao por agendador: o Optmus so
precisa do token quando vai ler algo, e um agendador seria mais uma peca viva
para manter de pe.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from core.config import Settings
from core.logging import get_logger
from memory.store import Store

log = get_logger("integrations.instagram")

BASE_URL: Final[str] = "https://graph.instagram.com"

# Sem versao fixada no caminho, e isso e uma escolha com custo conhecido.
#
# Fixar `/v23.0/` protegeria de mudanca de contrato, mas exigiria fixar uma
# versao que eu nao consegui confirmar como vigente - e uma versao inventada
# quebra tudo hoje, nao daqui a um ano. Sem versao, a Meta resolve, e o preco
# e que uma depreciacao futura aparece como erro de campo. Os erros da Meta sao
# repassados literalmente (ver `_falha`), entao o sintoma fica legivel.
TIMEOUT_S: Final[float] = 20.0
TENTATIVAS: Final[int] = 3

# Renova com folga: se esperasse o ultimo dia, um Core desligado numa semana
# qualquer perderia a janela e o token morreria sem chance de renovacao.
DIAS_PARA_RENOVAR: Final[int] = 10
VALIDADE_PADRAO_DIAS: Final[int] = 60

CHAVE_TOKEN: Final[str] = "instagram:token"
CHAVE_EXPIRA: Final[str] = "instagram:token_expira_em"
CHAVE_SEGUIDORES: Final[str] = "instagram:seguidores"
CHAVE_SEGUIDORES_EM: Final[str] = "instagram:seguidores_em"

CAMPOS_PERFIL: Final[str] = (
    "user_id,username,name,account_type,profile_picture_url,"
    "followers_count,follows_count,media_count"
)
CAMPOS_MIDIA: Final[str] = (
    "id,caption,media_type,permalink,timestamp,like_count,comments_count"
)
CAMPOS_COMENTARIO: Final[str] = "id,text,username,timestamp,like_count"

# `impressions` foi removida de vez em 21/04/2025; `views` e a substituta.
# Usar a metrica velha devolve erro, nao zero - e um zero silencioso seria pior.
METRICAS_PADRAO: Final[tuple[str, ...]] = (
    "reach",
    "views",
    "total_interactions",
    "follows_and_unfollows",
)


class InstagramError(RuntimeError):
    """A Meta recusou, ou nao respondeu."""


class InstagramNaoConfigurado(InstagramError):
    """Falta OPTMUS_INSTAGRAM_TOKEN ou OPTMUS_INSTAGRAM_ACCOUNT_ID."""


def _agora() -> datetime:
    return datetime.now(UTC)


class InstagramClient:
    """Leitura da propria conta: perfil, metricas e comentarios."""

    def __init__(self, settings: Settings, store: Store) -> None:
        self._settings = settings
        # O store guarda o token renovado. Settings e `frozen`, e tem que ser:
        # um token que se reescreve sozinho na configuracao seria um segredo
        # mutando por baixo de todo mundo que o leu.
        self._store = store

    @property
    def configurado(self) -> bool:
        return (
            self._settings.instagram_token is not None
            and bool(self._settings.instagram_account_id)
        )

    def _exigir_configuracao(self) -> None:
        if not self.configurado:
            raise InstagramNaoConfigurado(
                "Instagram nao configurado. Veja docs/INSTAGRAM.md: precisa de "
                "OPTMUS_INSTAGRAM_TOKEN e OPTMUS_INSTAGRAM_ACCOUNT_ID."
            )

    # -------------------------------------------------------------- token
    async def _token(self) -> str:
        """O token renovado tem precedencia sobre o do `.env`.

        Depois da primeira renovacao o token do `.env` esta velho. Ler dele
        seria usar credencial vencida tendo uma valida guardada ao lado.
        """
        self._exigir_configuracao()
        guardado = await self._store.meta_get(CHAVE_TOKEN)
        if guardado:
            return guardado
        token = self._settings.instagram_token
        assert token is not None  # garantido por _exigir_configuracao
        return token.get_secret_value()

    async def dias_restantes(self) -> int | None:
        """Dias ate o token vencer, ou ``None`` se nunca foi renovado.

        ``None`` e informacao, nao ausencia dela: significa que o prazo do token
        do `.env` e desconhecido - a Meta nao diz a validade de um token que ela
        nao acabou de emitir. Mostrar 60 ali seria inventar.
        """
        bruto = await self._store.meta_get(CHAVE_EXPIRA)
        if not bruto:
            return None
        return (datetime.fromisoformat(bruto) - _agora()).days

    async def renovar_se_preciso(self) -> dict[str, Any]:
        """Estende o token quando ele esta perto do fim.

        Nunca levanta excecao: renovacao e manutencao, e derrubar uma leitura
        de comentarios porque a manutencao falhou inverteria a prioridade.
        """
        self._exigir_configuracao()
        restantes = await self.dias_restantes()
        if restantes is not None and restantes > DIAS_PARA_RENOVAR:
            return {"renovado": False, "motivo": "ainda longe do vencimento",
                    "dias_restantes": restantes}

        try:
            dados = await self._chamar(
                "/refresh_access_token",
                {"grant_type": "ig_refresh_token"},
                autenticado=True,
            )
        except InstagramError as exc:
            # O caso normal aqui e "token com menos de 24 h", que acontece
            # logo depois de configurar. Nao e problema: a proxima leitura
            # tenta de novo.
            log.warning("instagram.renovacao_falhou", erro=str(exc), dias_restantes=restantes)
            return {"renovado": False, "motivo": str(exc), "dias_restantes": restantes}

        novo = dados.get("access_token")
        if not novo:
            return {"renovado": False, "motivo": "resposta sem access_token"}

        segundos = int(dados.get("expires_in") or VALIDADE_PADRAO_DIAS * 86400)
        expira = _agora() + timedelta(seconds=segundos)
        await self._store.meta_set(CHAVE_TOKEN, novo)
        await self._store.meta_set(CHAVE_EXPIRA, expira.isoformat())
        log.info("instagram.token_renovado", expira_em=expira.date().isoformat())
        return {"renovado": True, "dias_restantes": (expira - _agora()).days}

    # --------------------------------------------------------------- HTTP
    def _falha(self, corpo: dict[str, Any], status: int) -> InstagramError:
        """Repassa a mensagem da Meta literalmente.

        "Invalid OAuth access token" e "(#100) Tried accessing nonexisting
        field" dizem o que consertar. "HTTP 400" nao diz nada, e e justamente
        o que uma mudanca de contrato produz.
        """
        erro = corpo.get("error") or {}
        detalhe = erro.get("message") or f"HTTP {status}"
        codigo = erro.get("code")
        return InstagramError(f"{detalhe}" + (f" (code {codigo})" if codigo else ""))

    async def _chamar(
        self, caminho: str, params: dict[str, Any], *, autenticado: bool = True
    ) -> dict[str, Any]:
        import httpx

        consulta = dict(params)
        if autenticado:
            consulta["access_token"] = await self._token()
        url = f"{BASE_URL}{caminho}"

        for tentativa in range(TENTATIVAS):
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT_S) as http:
                    resposta = await http.get(url, params=consulta)
            except Exception as exc:  # rede instavel merece retry, nao falha
                if tentativa == TENTATIVAS - 1:
                    raise InstagramError(f"{type(exc).__name__}: {exc}") from exc
                await asyncio.sleep(0.5 * (2**tentativa))
                continue

            corpo: dict[str, Any] = resposta.json()
            if resposta.status_code == 429 or (corpo.get("error") or {}).get("code") == 4:
                if tentativa == TENTATIVAS - 1:
                    raise self._falha(corpo, resposta.status_code)
                await asyncio.sleep(2.0 * (tentativa + 1))
                continue
            if resposta.status_code >= 400 or "error" in corpo:
                raise self._falha(corpo, resposta.status_code)
            return corpo

        raise InstagramError("Instagram nao respondeu apos as tentativas")

    # ------------------------------------------------------------ leituras
    async def perfil(self) -> dict[str, Any]:
        return await self._chamar("/me", {"fields": CAMPOS_PERFIL})

    async def variacao_de_seguidores(self, atual: int) -> dict[str, Any]:
        """Quantos seguidores entraram desde a ultima checagem.

        E o mais perto de "chegou seguidor novo" que o caminho oficial chega:
        a API devolve o TOTAL, nunca a lista. Nome, foto e vinculo nao existem
        em endpoint nenhum, entao esta funcao responde "quantos", jamais "quem"
        - e quem chama nao deve sugerir o contrario.
        """
        anterior = await self._store.meta_get(CHAVE_SEGUIDORES)
        desde = await self._store.meta_get(CHAVE_SEGUIDORES_EM)
        await self._store.meta_set(CHAVE_SEGUIDORES, str(atual))
        await self._store.meta_set(CHAVE_SEGUIDORES_EM, _agora().isoformat())

        if anterior is None:
            # Primeira leitura: nao ha com o que comparar. Devolver 0 seria
            # afirmar que nada mudou, que e uma afirmacao que nao temos.
            return {"delta": None, "desde": None}
        return {"delta": atual - int(anterior), "desde": desde}

    async def insights(self, metricas: tuple[str, ...] = METRICAS_PADRAO) -> dict[str, Any]:
        """Metricas do dia.

        A Meta devolve **conjunto vazio** quando a metrica nao existe ou ainda
        nao tem dado - nao devolve 0. As duas coisas sao diferentes e o chamador
        precisa distinguir: ausente vira travessao, zero vira zero.
        """
        dados = await self._chamar(
            "/me/insights",
            {"metric": ",".join(metricas), "period": "day", "metric_type": "total_value"},
        )
        fora: dict[str, int | None] = dict.fromkeys(metricas, None)
        for item in dados.get("data", []):
            nome = item.get("name")
            if nome not in fora:
                continue
            valor = (item.get("total_value") or {}).get("value")
            fora[nome] = int(valor) if valor is not None else None
        return fora

    async def midias(self, limite: int = 5) -> list[dict[str, Any]]:
        dados = await self._chamar("/me/media", {"fields": CAMPOS_MIDIA, "limit": limite})
        return list(dados.get("data", []))

    async def comentarios(self, media_id: str, limite: int = 10) -> list[dict[str, Any]]:
        dados = await self._chamar(
            f"/{media_id}/comments", {"fields": CAMPOS_COMENTARIO, "limit": limite}
        )
        return list(dados.get("data", []))
