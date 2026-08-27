"""De onde vem um motivo para falar, e por onde o aviso sai.

Separado de ``core.proatividade`` de proposito: la mora a decisao de **se**
avisa (orcamento, silencio, repeticao), aqui moram as coisas que falam com o
mundo. Misturar as duas faria os freios dependerem do Notion estar de pe.

Toda fonte aqui devolve **fato observado**, nunca opiniao. "prova de biologia
em 14/09, faltam 2 dias" e fato; "voce deveria estudar" nao e - essa parte e o
modelo que escreve, a partir do fato.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.bus import EventBus
from core.config import Settings
from core.llm import LLMClient
from core.logging import get_logger
from core.proatividade import INSTRUCAO, Gatilho, impressao
from memory.procedural import ProceduralMemory

log = get_logger("core.proatividade.fontes")

# Um prazo so vira aviso quando esta perto o bastante para caber numa acao.
# Trinta dias antes nao e aviso, e ansiedade.
DIAS_PARA_AVISAR = 3


class PrazosDoNotion:
    """Prazos vencendo nos proximos dias, lidos da base de verdade."""

    def __init__(self, stats: Any, *, dias: int = DIAS_PARA_AVISAR) -> None:
        self._stats = stats
        self._dias = dias

    async def coletar(self, agora: datetime) -> list[Gatilho]:
        alertas = await self._stats.progress_alerts(hoje=agora.date())

        gatilhos: list[Gatilho] = []
        for alerta in alertas:
            faltam = alerta.get("diasRestantes")
            if faltam is None or faltam > self._dias:
                continue

            titulo = str(alerta.get("titulo") or "").strip()
            quando = str(alerta.get("data") or "")
            # A chave inclui a DATA, nao os dias restantes: "faltam 2" vira
            # "falta 1" amanha e o mesmo prazo seria avisado de novo, todo dia,
            # ate vencer. Com a data, o assunto e um so.
            chave = impressao(str(alerta.get("tipo") or ""), titulo, quando)

            if faltam < 0:
                situacao = f"venceu ha {abs(faltam)} dia(s)"
            elif faltam == 0:
                situacao = "vence HOJE"
            else:
                situacao = f"vence em {faltam} dia(s)"

            detalhe = str(alerta.get("detalhe") or "").strip()
            gatilhos.append(
                Gatilho(
                    chave=chave,
                    assunto=titulo or "prazo",
                    fatos=(
                        f"- {alerta.get('tipo')}: {titulo} ({quando}) {situacao}."
                        + (f" Detalhe: {detalhe}" if detalhe else "")
                    ),
                    # Quanto menos dias, mais urgente. Vencido vale como hoje:
                    # ja passou, e repetir com urgencia crescente nao ajuda.
                    urgencia=3 - max(0, min(3, faltam)),
                )
            )
        return gatilhos


class RotinasDaMemoria:
    """Habitos que o consolidador detectou e que batem com este horario."""

    def __init__(self, procedural: ProceduralMemory) -> None:
        self._procedural = procedural

    async def coletar(self, agora: datetime) -> list[Gatilho]:
        # `para_agora` ja filtra por dia da semana e hora aproximada. Aqui so
        # traduz para gatilho - a deteccao do habito e da memoria procedural,
        # que exige ocorrencias em SEMANAS distintas justamente para nao
        # transformar tres segundas seguidas em rotina.
        pertinentes = await self._procedural.para_agora(agora)
        return [
            Gatilho(
                chave=impressao("rotina", str(linha.get("id") or linha.get("content", ""))),
                assunto="rotina",
                fatos=f"- Habito seu neste horario: {linha.get('content')}",
                urgencia=1,
            )
            for linha in pertinentes
        ]


class CompositorLLM:
    """Escreve a frase do aviso. **Sem ferramenta nenhuma.**

    O agente e construido aqui com ``tools=None``, e isso e a guarda central do
    F7: um aviso proativo nao tem humano esperando para confirmar nada, entao o
    portao de ``EXTERNO`` nao se aplica. Um caminho sem portao nao pode alcancar
    terceiros - e sem ferramentas, o WhatsApp nao esta ao alcance nem por
    engano. E estrutural, nao uma instrucao no prompt que o modelo poderia
    contornar.
    """

    def __init__(self, cliente: LLMClient, settings: Settings) -> None:
        from core.agent import Agent

        self._settings = settings
        self._agente = Agent(cliente, settings, tools=None)

    async def escrever(self, fatos: str) -> str:
        resultado = await self._agente.run(
            INSTRUCAO.format(nome=self._settings.user_name, fatos=fatos)
        )
        return resultado.text or ""


class CanalTelegram:
    """Manda para o proprio dono, no destino fixo da configuracao.

    Nao passa pela ferramenta ``telegram_enviar`` de proposito: aquela e
    ``EXTERNO`` e exige confirmacao humana, que aqui nao existe. Usa o cliente
    direto, e o que a mantem segura e o mesmo que ja mantinha a ferramenta - o
    destino mora na configuracao e nao pode ser escolhido por ninguem, nem pelo
    modelo, nem por texto que ele leu.
    """

    def __init__(self, settings: Settings) -> None:
        from integrations.telegram import TelegramClient

        self._cliente = TelegramClient(settings)

    async def avisar(self, texto: str) -> bool:
        from integrations.telegram import TelegramError

        if not self._cliente.configurado:
            return False
        try:
            await self._cliente.enviar(texto)
        except TelegramError as exc:
            log.warning("proatividade.telegram_falhou", erro=str(exc))
            return False
        return True


class CanalBarramento:
    """Publica no barramento para o HUD mostrar.

    Sempre presente: e o unico canal que funciona sem nenhuma integracao
    configurada, e sem ele um Optmus recem-instalado teria proatividade que nao
    chega a lugar nenhum.
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def avisar(self, texto: str) -> bool:
        await self._bus.emit(
            "proatividade.aviso",
            source="proatividade",
            payload={"texto": texto, "em": datetime.now(UTC).isoformat()},
        )
        return True


class CanalComposto:
    """Tenta todos, e considera entregue se **algum** entregou.

    Nao para no primeiro sucesso: o HUD e o Telegram sao lugares diferentes, e
    quem esta no celular nao ve o navegador. Falha de um nao pode calar o
    outro.
    """

    def __init__(self, canais: list[Any]) -> None:
        self._canais = canais

    async def avisar(self, texto: str) -> bool:
        entregou = False
        for canal in self._canais:
            try:
                entregou = await canal.avisar(texto) or entregou
            except Exception as exc:  # noqa: BLE001 - canal ruim nao cala os outros
                log.warning("proatividade.canal_falhou", canal=type(canal).__name__, erro=str(exc))
        return entregou
