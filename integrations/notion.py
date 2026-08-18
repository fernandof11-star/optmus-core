"""Cliente da API do Notion.

REST direto via httpx, sem SDK: a superficie que o Core usa e pequena (consultar
base, ler schema, listar bases) e um SDK a mais e uma dependencia a mais para
manter alinhada.

Duas coisas que a API do Notion exige e que sao faceis de esquecer:

- **Paginacao.** ``/query`` devolve no maximo 100 paginas por vez. Ignorar o
  ``next_cursor`` produz um total silenciosamente truncado - e num relatorio
  financeiro isso e pior do que um erro, porque parece certo.
- **Rate limit.** ~3 requisicoes por segundo, com 429 e ``Retry-After``. Uma
  agregacao de 12 meses faz muitas chamadas seguidas e bate nesse teto.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Final

from core.config import Settings
from core.logging import get_logger

log = get_logger("integrations.notion")

BASE_URL: Final[str] = "https://api.notion.com/v1"
PAGE_SIZE: Final[int] = 100
MAX_PAGINAS: Final[int] = 200  # teto de seguranca: 20 mil linhas por consulta


class NotionError(RuntimeError):
    """Falha ao falar com o Notion."""


class NotionNaoConfigurado(NotionError):
    """Falta OPTMUS_NOTION_TOKEN."""


@dataclass(slots=True)
class DatabaseInfo:
    """Schema de uma base, como o Notion o descreve."""

    id: str
    titulo: str
    propriedades: dict[str, str] = field(default_factory=dict)
    # Opcoes de cada select/status/multi_select. Sem elas a descoberta so diz
    # que uma coluna e "select", e escolher os valores de "concluido" vira
    # chute - chute que falha em silencio, contando de menos e nunca erro.
    opcoes: dict[str, list[str]] = field(default_factory=dict)

    def por_tipo(self, tipo: str) -> list[str]:
        return sorted(nome for nome, t in self.propriedades.items() if t == tipo)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "titulo": self.titulo,
            "propriedades": self.propriedades,
            "opcoes": self.opcoes,
        }


class NotionClient:
    """Acesso somente-leitura ao Notion, com paginacao e respeito a rate limit."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def configurado(self) -> bool:
        return self._settings.notion_token is not None

    def _headers(self) -> dict[str, str]:
        if self._settings.notion_token is None:
            raise NotionNaoConfigurado(
                "OPTMUS_NOTION_TOKEN ausente. Crie uma integracao interna em "
                "notion.so/my-integrations e compartilhe as bases com ela."
            )
        return {
            "Authorization": f"Bearer {self._settings.notion_token.get_secret_value()}",
            "Notion-Version": self._settings.notion_version,
            "Content-Type": "application/json",
        }

    async def _requisitar(
        self, metodo: str, caminho: str, *, corpo: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        import httpx

        url = f"{BASE_URL}{caminho}"
        for tentativa in range(4):
            try:
                async with httpx.AsyncClient(timeout=self._settings.notion_timeout_s) as http:
                    resposta = await http.request(
                        metodo, url, json=corpo, headers=self._headers()
                    )
            except NotionNaoConfigurado:
                raise
            except Exception as exc:
                if tentativa == 3:
                    raise NotionError(f"{type(exc).__name__}: {exc}") from exc
                await asyncio.sleep(0.5 * (2**tentativa))
                continue

            if resposta.status_code == 429:
                # O Notion diz quanto esperar; ignorar isso so gera mais 429.
                espera = float(resposta.headers.get("Retry-After", "1"))
                log.warning("notion.rate_limit", espera_s=espera, caminho=caminho)
                await asyncio.sleep(min(espera, 10.0))
                continue
            if resposta.status_code == 401:
                raise NotionError(
                    "Notion recusou o token (401). Confira OPTMUS_NOTION_TOKEN."
                )
            if resposta.status_code == 404:
                raise NotionError(
                    f"objeto nao encontrado ou nao compartilhado com a integracao: {caminho}. "
                    "No Notion: abra a base -> ... -> Conexoes -> adicione a integracao."
                )
            if resposta.status_code >= 500:
                if tentativa == 3:
                    raise NotionError(f"Notion respondeu HTTP {resposta.status_code}")
                await asyncio.sleep(0.5 * (2**tentativa))
                continue
            if resposta.status_code >= 400:
                raise NotionError(
                    f"Notion recusou o pedido (HTTP {resposta.status_code}): "
                    f"{resposta.text[:300]}"
                )
            return dict(resposta.json())

        raise NotionError("Notion nao respondeu apos as tentativas")

    async def listar_bases(self) -> list[DatabaseInfo]:
        """Bases que a integracao consegue ver.

        Vazio quase sempre significa a mesma coisa: a integracao existe mas
        nenhuma base foi compartilhada com ela.
        """
        bases: list[DatabaseInfo] = []
        cursor: str | None = None
        while True:
            corpo: dict[str, Any] = {
                "filter": {"property": "object", "value": "database"},
                "page_size": PAGE_SIZE,
            }
            if cursor:
                corpo["start_cursor"] = cursor
            dados = await self._requisitar("POST", "/search", corpo=corpo)
            bases.extend(_database_info(item) for item in dados.get("results", []))
            if not dados.get("has_more"):
                return bases
            cursor = dados.get("next_cursor")

    async def schema(self, database_id: str) -> DatabaseInfo:
        return _database_info(await self._requisitar("GET", f"/databases/{database_id}"))

    async def consultar(
        self,
        database_id: str,
        *,
        filtro: dict[str, Any] | None = None,
        ordenacao: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Todas as paginas da base, seguindo o cursor ate o fim."""
        paginas: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_PAGINAS):
            corpo: dict[str, Any] = {"page_size": PAGE_SIZE}
            if filtro:
                corpo["filter"] = filtro
            if ordenacao:
                corpo["sorts"] = ordenacao
            if cursor:
                corpo["start_cursor"] = cursor

            dados = await self._requisitar(
                "POST", f"/databases/{database_id}/query", corpo=corpo
            )
            paginas.extend(dados.get("results", []))
            if not dados.get("has_more"):
                log.debug("notion.consulta", database=database_id[:8], linhas=len(paginas))
                return paginas
            cursor = dados.get("next_cursor")

        log.error("notion.paginacao_estourou", database=database_id[:8], limite=MAX_PAGINAS)
        raise NotionError(
            f"consulta passou de {MAX_PAGINAS} paginas - resultado seria truncado"
        )


def _database_info(bruto: dict[str, Any]) -> DatabaseInfo:
    titulo = "".join(
        t.get("plain_text", "") for t in bruto.get("title", []) if isinstance(t, dict)
    )
    propriedades = {
        nome: str(prop.get("type", "?"))
        for nome, prop in (bruto.get("properties") or {}).items()
        if isinstance(prop, dict)
    }
    opcoes: dict[str, list[str]] = {}
    for nome, prop in (bruto.get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue
        tipo = prop.get("type")
        if tipo not in ("select", "status", "multi_select"):
            continue
        corpo = prop.get(tipo)
        if isinstance(corpo, dict):
            opcoes[nome] = [
                str(o.get("name", ""))
                for o in corpo.get("options", [])
                if isinstance(o, dict)
            ]
    return DatabaseInfo(
        id=str(bruto.get("id", "")),
        titulo=titulo or "(sem titulo)",
        propriedades=propriedades,
        opcoes=opcoes,
    )


# ------------------------------------------------------------------ leitores
# O Notion embrulha todo valor num envelope tipado. Estes leitores desembrulham
# e devolvem None quando o campo esta vazio - o que e diferente de zero, e essa
# diferenca importa numa soma financeira.
def ler_numero(propriedade: dict[str, Any] | None) -> float | None:
    if not propriedade:
        return None
    tipo = propriedade.get("type")
    if tipo == "number":
        valor = propriedade.get("number")
    elif tipo == "formula":
        valor = (propriedade.get("formula") or {}).get("number")
    elif tipo == "rollup":
        valor = (propriedade.get("rollup") or {}).get("number")
    else:
        return None
    return float(valor) if isinstance(valor, int | float) else None


def ler_data(propriedade: dict[str, Any] | None) -> str | None:
    """Devolve a data ISO (YYYY-MM-DD), sem hora."""
    if not propriedade:
        return None
    tipo = propriedade.get("type")
    if tipo == "date":
        bruto = (propriedade.get("date") or {}).get("start")
    elif tipo == "formula":
        bruto = ((propriedade.get("formula") or {}).get("date") or {}).get("start")
    elif tipo == "created_time":
        bruto = propriedade.get("created_time")
    elif tipo == "last_edited_time":
        bruto = propriedade.get("last_edited_time")
    else:
        return None
    return str(bruto)[:10] if bruto else None


def ler_texto(propriedade: dict[str, Any] | None) -> str | None:
    if not propriedade:
        return None
    tipo = propriedade.get("type")
    if tipo in ("title", "rich_text"):
        partes = propriedade.get(tipo) or []
        texto = "".join(p.get("plain_text", "") for p in partes if isinstance(p, dict))
        return texto.strip() or None
    if tipo == "select":
        return ((propriedade.get("select") or {}).get("name") or "").strip() or None
    if tipo == "status":
        return ((propriedade.get("status") or {}).get("name") or "").strip() or None
    if tipo == "multi_select":
        nomes = [s.get("name", "") for s in (propriedade.get("multi_select") or [])]
        return ", ".join(n for n in nomes if n) or None
    if tipo == "formula":
        return str((propriedade.get("formula") or {}).get("string") or "").strip() or None
    if tipo == "checkbox":
        return "true" if propriedade.get("checkbox") else "false"
    return None


def ler_checkbox(propriedade: dict[str, Any] | None) -> bool | None:
    if not propriedade or propriedade.get("type") != "checkbox":
        return None
    return bool(propriedade.get("checkbox"))


def propriedades(pagina: dict[str, Any]) -> dict[str, Any]:
    return dict(pagina.get("properties") or {})
