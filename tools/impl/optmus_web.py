"""Ponte com o Optmus Web - a API que ja existe e guarda os dados no Notion.

O Core **nao** duplica financeiro, tarefas, treino, estudos, investimentos,
trabalho e notas: consulta o Web. Duplicar dado e criar duas verdades e escolher
a errada.

Contrato real (conferido contra a API em producao, nao presumido):

    POST /api/login  {"password": ...}  ->  {"token": ...}
    Authorization: Bearer <token>       em todas as demais chamadas

    token = HMAC-SHA256(chave='jarvis-auth-v1', mensagem=senha) em hex

O token e **estatico**: nao muda por requisicao, nao leva timestamp nem corpo.
Por isso e calculado uma vez, sem round-trip de login. Consequencia de seguranca
que vale ter escrita: sendo estatico, ele equivale a um bearer permanente - sem
protecao contra replay, valido ate a senha mudar.

Dois caminhos de acesso, com riscos diferentes:

- **stats** (``GET /api/stats/*`` e afins) - somente leitura, estruturado,
  barato e rapido. E o caminho preferido: nao aciona modelo nenhum do outro
  lado, entao nao paga latencia de LLM dentro de um turno de voz.
- **chat** (``POST /api/chat``) - o proprio agente do Optmus Web, com as
  ferramentas de Notion dele. Responde qualquer coisa, inclusive o que nao tem
  endpoint de estatistica, e **pode escrever no Notion**. Por isso e ESCRITA,
  nao LEITURA: chamar de leitura uma porta que grava seria mentira de rotulo.

Resiliencia: timeout curto, retry com backoff e circuit breaker. Se o Web cair,
o Core **nao** cai - degrada e avisa. Cold start de serverless e normal, nao
erro; por isso o retry existe.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Final

from core.config import Settings
from core.logging import get_logger
from security.policy import RiskLevel
from tools.registry import Tool, ToolResult

log = get_logger("tools.optmus_web")

# Chave fixa do esquema de auth do Web. NAO e segredo - o segredo e a senha,
# que entra como MENSAGEM do HMAC. Inverter os dois produz um token plausivel
# e completamente errado.
CHAVE_HMAC: Final[bytes] = b"jarvis-auth-v1"

ROTA_LOGIN: Final[str] = "/api/login"
ROTA_CHAT: Final[str] = "/api/chat"

# Indicadores somente-leitura, com nome pensado para o modelo escolher.
INDICADORES: Final[dict[str, str]] = {
    "financeiro_mensal": "/api/stats/monthly",
    "financeiro_semanal": "/api/stats/finance-weekly",
    "gastos_por_categoria": "/api/stats/category-spending",
    "taxa_de_poupanca": "/api/stats/savings-rate",
    "previsao_financeira": "/api/stats/forecast",
    "tarefas": "/api/stats/tasks",
    "trabalho": "/api/work-tasks",
    "estudos": "/api/stats/study",
    "notas_escolares": "/api/stats/grades",
    "treino_frequencia": "/api/stats/workout-frequency",
    "treino_mensal": "/api/stats/workout-monthly",
    "sono": "/api/stats/sleep",
    "alertas": "/api/progress-alerts",
}


class WebIndisponivel(RuntimeError):
    """O Optmus Web nao respondeu de forma utilizavel."""


@dataclass(slots=True)
class CircuitBreaker:
    """Para de bater numa porta fechada.

    Sem isso, cada turno de voz espera o timeout inteiro antes de descobrir que
    o Web esta fora - e o usuario ouve cinco segundos de silencio, toda vez.
    """

    limite_falhas: int = 3
    janela_aberta_s: float = 30.0
    falhas: int = 0
    aberto_ate: float = 0.0
    ultimo_erro: str | None = None

    @property
    def aberto(self) -> bool:
        return time.monotonic() < self.aberto_ate

    def registrar_falha(self, erro: str) -> None:
        self.falhas += 1
        self.ultimo_erro = erro
        if self.falhas >= self.limite_falhas:
            self.aberto_ate = time.monotonic() + self.janela_aberta_s
            log.warning(
                "optmus_web.circuito_aberto",
                falhas=self.falhas,
                segundos=self.janela_aberta_s,
                ultimo_erro=erro,
            )

    def registrar_sucesso(self) -> None:
        self.falhas = 0
        self.aberto_ate = 0.0
        self.ultimo_erro = None

    def stats(self) -> dict[str, Any]:
        return {
            "aberto": self.aberto,
            "falhas_seguidas": self.falhas,
            "ultimo_erro": self.ultimo_erro,
        }


class OptmusWebClient:
    """Cliente HTTP do Optmus Web: auth, retry, circuit breaker."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.breaker = CircuitBreaker()
        self.ultima_requisicao: dict[str, Any] = {}
        self._token: str | None = None

    @property
    def configurado(self) -> bool:
        return bool(self._settings.web_base_url) and self._settings.web_password is not None

    @property
    def token(self) -> str:
        """Token estatico, calculado uma vez a partir da senha."""
        if self._token is None:
            assert self._settings.web_password is not None
            self._token = hmac.new(
                CHAVE_HMAC,
                self._settings.web_password.get_secret_value().encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        return self._token

    def _url(self, rota: str) -> str:
        return f"{(self._settings.web_base_url or '').rstrip('/')}{rota}"

    def _headers(self) -> dict[str, str]:
        if self._settings.web_auth_scheme == "header":
            return {self._settings.web_auth_header: self.token}
        return {"Authorization": f"Bearer {self.token}"}

    async def login(self) -> str:
        """Troca a senha por um token no proprio Web.

        Normalmente desnecessario - o token e derivavel localmente. Existe como
        rede de seguranca: se o esquema do Web mudar, isto continua funcionando
        e o erro aparece como falha de login, nao como 401 misterioso.
        """
        import httpx

        assert self._settings.web_password is not None
        async with httpx.AsyncClient(timeout=self._settings.web_timeout_s) as http:
            resposta = await http.post(
                self._url(ROTA_LOGIN),
                json={"password": self._settings.web_password.get_secret_value()},
            )
        if resposta.status_code != 200:
            raise WebIndisponivel(
                f"login recusado pelo Optmus Web (HTTP {resposta.status_code}). "
                "Confira OPTMUS_WEB_PASSWORD."
            )
        token = str(resposta.json().get("token", ""))
        if not token:
            raise WebIndisponivel("o Optmus Web nao devolveu token no login")
        self._token = token
        return token

    async def _requisitar(
        self, metodo: str, rota: str, *, corpo: dict[str, Any] | None = None
    ) -> Any:
        import httpx

        if not self.configurado:
            raise WebIndisponivel(
                "Optmus Web nao configurado: faltam OPTMUS_WEB_BASE_URL e OPTMUS_WEB_PASSWORD"
            )
        if self.breaker.aberto:
            raise WebIndisponivel(
                f"circuito aberto apos {self.breaker.falhas} falhas seguidas "
                f"({self.breaker.ultimo_erro})"
            )

        url = self._url(rota)
        # Guarda os NOMES dos headers, nunca os valores: o token e credencial.
        self.ultima_requisicao = {
            "metodo": metodo,
            "url": url,
            "corpo": json.dumps(corpo, ensure_ascii=False)[:400] if corpo else None,
            "quando": datetime.now(UTC).isoformat(),
        }

        ultimo_erro = ""
        ja_tentou_login = False
        # 3 tentativas com backoff: cold start de serverless e normal, nao erro.
        for tentativa in range(3):
            try:
                async with httpx.AsyncClient(timeout=self._settings.web_timeout_s) as http:
                    resposta = await http.request(
                        metodo, url, json=corpo, headers=self._headers()
                    )

                if resposta.status_code in (401, 403):
                    if not ja_tentou_login:
                        # Token pode ter mudado de esquema; tenta o login oficial.
                        ja_tentou_login = True
                        await self.login()
                        continue
                    self.breaker.registrar_falha(f"auth recusada (HTTP {resposta.status_code})")
                    raise WebIndisponivel(
                        f"o Optmus Web recusou a autenticacao (HTTP {resposta.status_code}). "
                        "Confira OPTMUS_WEB_PASSWORD."
                    )
                if resposta.status_code >= 500:
                    ultimo_erro = f"HTTP {resposta.status_code}"
                elif resposta.status_code >= 400:
                    self.breaker.registrar_sucesso()  # servidor vivo; o pedido e que errou
                    raise WebIndisponivel(
                        f"o Optmus Web recusou o pedido (HTTP {resposta.status_code}): "
                        f"{resposta.text[:200]}"
                    )
                else:
                    self.breaker.registrar_sucesso()
                    return _corpo_json(resposta)
            except WebIndisponivel:
                raise
            except Exception as exc:  # noqa: BLE001 - timeout, DNS, conexao
                ultimo_erro = f"{type(exc).__name__}: {exc}"

            if tentativa < 2:
                import asyncio

                await asyncio.sleep(0.4 * (2**tentativa))

        self.breaker.registrar_falha(ultimo_erro)
        raise WebIndisponivel(ultimo_erro or "sem resposta")

    async def indicador(self, nome: str) -> Any:
        """GET num endpoint de estatistica. Somente leitura."""
        rota = INDICADORES.get(nome)
        if rota is None:
            raise WebIndisponivel(f"indicador desconhecido: {nome}")
        return await self._requisitar("GET", rota)

    async def chat(self, pergunta: str) -> str:
        """Pergunta ao agente do Optmus Web e devolve o texto da resposta."""
        dados = await self._requisitar(
            "POST", ROTA_CHAT, corpo={"messages": [{"role": "user", "content": pergunta}]}
        )
        return _texto_da_resposta(dados)

    async def diagnostico(self) -> dict[str, Any]:
        """Sonda o Web e mostra o que foi enviado e o que voltou."""
        info: dict[str, Any] = {
            "configurado": self.configurado,
            "base_url": self._settings.web_base_url,
            "auth": "Authorization: Bearer <token estatico>",
            "indicadores": sorted(INDICADORES),
            "circuito": self.breaker.stats(),
        }
        if not self.configurado:
            info["erro"] = "faltam OPTMUS_WEB_BASE_URL e/ou OPTMUS_WEB_PASSWORD"
            return info
        try:
            info["resposta"] = await self.indicador("financeiro_mensal")
            info["ok"] = True
        except WebIndisponivel as exc:
            info["ok"] = False
            info["erro"] = str(exc)
        info["ultima_requisicao"] = self.ultima_requisicao
        return info


def _corpo_json(resposta: Any) -> Any:
    try:
        return resposta.json()
    except Exception:  # noqa: BLE001 - resposta nao-JSON ainda e informacao
        return {"texto": resposta.text[:2000]}


def _texto_da_resposta(dados: Any) -> str:
    """Extrai o texto do assistente do formato de mensagens do Web."""
    mensagens = dados.get("messages", []) if isinstance(dados, dict) else []
    for mensagem in reversed(mensagens):
        if mensagem.get("role") != "assistant":
            continue
        conteudo = mensagem.get("content")
        if isinstance(conteudo, str):
            return conteudo
        if isinstance(conteudo, list):
            return " ".join(
                bloco.get("text", "")
                for bloco in conteudo
                if isinstance(bloco, dict) and bloco.get("type") == "text"
            ).strip()
    return json.dumps(dados, ensure_ascii=False)[:2000]


class OptmusWebTool(Tool):
    """Indicadores do Notion via endpoints de estatistica. Somente leitura."""

    name = "optmus_web"
    risk = RiskLevel.LEITURA
    description = (
        "Consulta os numeros do usuario no Optmus Web (Notion): financas, tarefas, "
        "trabalho, estudos, notas escolares, treino, sono e alertas de prazo. "
        "Use SEMPRE que a pergunta for sobre quanto gastou, quanto poupou, o que "
        "tem para fazer, como esta o treino ou os estudos. Rapido e sem custo. "
        "Se o indicador certo nao existir aqui, use optmus_web_perguntar. "
        "Nao invente numeros: se falhar, diga que nao alcancou os dados."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "indicador": {
                "type": "string",
                "enum": sorted(INDICADORES),
                "description": (
                    "financeiro_mensal = receita/despesa/saldo por mes; "
                    "gastos_por_categoria = onde o dinheiro foi; "
                    "alertas = prazos proximos"
                ),
            }
        },
        "required": ["indicador"],
        "additionalProperties": False,
    }

    def __init__(self, settings: Settings, client: OptmusWebClient | None = None) -> None:
        self._settings = settings
        self.client = client or OptmusWebClient(settings)

    async def available(self) -> bool:
        return self.client.configurado

    def resumir(self, parametros: dict[str, Any]) -> str:
        return f"consultar {parametros.get('indicador', '?')} no Optmus Web"

    async def execute(self, **kwargs: Any) -> ToolResult:
        indicador = str(kwargs.get("indicador", ""))
        if indicador not in INDICADORES:
            return ToolResult.erro(
                f"indicador invalido: {indicador}. Disponiveis: {', '.join(sorted(INDICADORES))}"
            )
        try:
            dados = await self.client.indicador(indicador)
        except WebIndisponivel as exc:
            log.warning("optmus_web.indisponivel", indicador=indicador, erro=str(exc))
            return ToolResult.erro(
                f"Nao consigo alcancar meus dados agora ({exc}). "
                "Diga isso ao usuario sem inventar numeros."
            )
        return ToolResult(
            content=json.dumps(dados, ensure_ascii=False)[:4000],
            dados={"indicador": indicador, "dados": dados},
            metadata={"indicador": indicador},
        )


class OptmusWebChatTool(Tool):
    """Pergunta livre ao agente do Optmus Web - inclusive para registrar."""

    name = "optmus_web_perguntar"
    # ESCRITA, nao LEITURA: /api/chat e o agente do Web com as ferramentas de
    # Notion dele, e pode gravar. Rotular de leitura seria mentira.
    risk = RiskLevel.ESCRITA
    description = (
        "Fala com o agente do Optmus Web em linguagem natural. Ele tem acesso "
        "completo ao Notion do usuario e pode CONSULTAR e REGISTRAR: lancar um "
        "gasto, criar tarefa, anotar treino, salvar nota. Use quando o indicador "
        "pronto do optmus_web nao cobrir a pergunta, ou quando for para gravar "
        "algo. Passe a pergunta ou o pedido completo, como o usuario falou. "
        "Mais lento e mais caro que optmus_web - prefira o indicador quando der."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "pergunta": {
                "type": "string",
                "description": "pergunta ou instrucao completa, em portugues",
            }
        },
        "required": ["pergunta"],
        "additionalProperties": False,
    }

    def __init__(self, settings: Settings, client: OptmusWebClient | None = None) -> None:
        self._settings = settings
        self.client = client or OptmusWebClient(settings)

    async def available(self) -> bool:
        return self.client.configurado

    def resumir(self, parametros: dict[str, Any]) -> str:
        return f"pedir ao Optmus Web: {parametros.get('pergunta', '')}"

    async def execute(self, **kwargs: Any) -> ToolResult:
        pergunta = str(kwargs.get("pergunta", "")).strip()
        if not pergunta:
            return ToolResult.erro("pergunta vazia")
        try:
            texto = await self.client.chat(pergunta)
        except WebIndisponivel as exc:
            log.warning("optmus_web.chat_indisponivel", erro=str(exc))
            return ToolResult.erro(
                f"Nao consigo alcancar meus dados agora ({exc}). "
                "Diga isso ao usuario sem inventar numeros."
            )
        return ToolResult(content=texto[:4000], metadata={"rota": ROTA_CHAT})
