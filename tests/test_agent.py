"""Loop de agente: rodadas de ferramenta, teto e falhas."""

from __future__ import annotations

from typing import Any

import pytest

from core.agent import MARCADOR_IMAGEM, Agent
from core.config import Settings
from core.llm import Imagem, LLMError, LLMTurn
from tests.fakes import FakeLLM, FakeTools, turno_de_ferramenta


class LLMQueQuebra(FakeLLM):
    async def stream_turn(self, **kwargs: object) -> LLMTurn:  # type: ignore[override]
        raise LLMError("connection reset")


async def test_resposta_direta_sem_ferramenta(settings: Settings) -> None:
    agente = Agent(FakeLLM([LLMTurn(text="Sao quatro e vinte.", stop_reason="end_turn")]), settings)
    resultado = await agente.run("que horas sao")
    assert resultado.text == "Sao quatro e vinte."
    assert resultado.rounds == 1
    assert resultado.ok


async def test_deltas_chegam_ao_tts_durante_a_geracao(settings: Settings) -> None:
    recebidos: list[str] = []
    cliente = FakeLLM([LLMTurn(text="uma resposta longa o suficiente", stop_reason="end_turn")])
    await Agent(cliente, settings).run("oi", on_text=lambda d: _guardar(recebidos, d))
    assert len(recebidos) > 1, "resposta deve chegar em pedacos, nao de uma vez"
    assert "".join(recebidos) == "uma resposta longa o suficiente"


async def _guardar(destino: list[str], delta: str) -> None:
    destino.append(delta)


async def test_ferramenta_executa_e_volta_para_o_modelo(settings: Settings) -> None:
    tools = FakeTools({"optmus_web": "Gastou 3200 reais."})
    agente = Agent(
        FakeLLM([
            turno_de_ferramenta("optmus_web", {"dominio": "financeiro"}),
            LLMTurn(text="Tres mil e duzentos esse mes.", stop_reason="end_turn"),
        ]),
        settings,
        tools=tools,
    )
    resultado = await agente.run("quanto gastei esse mes")

    assert tools.executadas == [("optmus_web", {"dominio": "financeiro"})]
    assert resultado.rounds == 2
    assert len(resultado.tool_calls) == 1
    assert resultado.text == "Tres mil e duzentos esse mes."


async def test_resultados_de_ferramenta_vao_numa_mensagem_so(settings: Settings) -> None:
    """Dividir tool_results ensina o modelo a parar de pedir chamadas paralelas."""
    tools = FakeTools({"a": "1", "b": "2"})
    cliente = FakeLLM([
        LLMTurn(
            tool_calls=[
                *turno_de_ferramenta("a", {}, id_="t1").tool_calls,
                *turno_de_ferramenta("b", {}, id_="t2").tool_calls,
            ],
            stop_reason="tool_use",
            assistant_content=[{"type": "tool_use", "id": "t1", "name": "a", "input": {}}],
        ),
        LLMTurn(text="Pronto.", stop_reason="end_turn"),
    ])
    agente = Agent(cliente, settings, tools=tools)
    await agente.run("faz as duas coisas")

    ultima = cliente.chamadas[-1]["messages"][-1]
    assert ultima["role"] == "user"
    assert [b["type"] for b in ultima["content"]] == ["tool_result", "tool_result"]


async def test_ferramenta_que_explode_nao_derruba_o_turno(settings: Settings) -> None:
    class ToolsQuebradas(FakeTools):
        async def execute(self, name, arguments, *, correlation_id=None):  # type: ignore[no-untyped-def]
            raise ValueError("adb morreu")

    agente = Agent(
        FakeLLM([
            turno_de_ferramenta("dispositivo_executar", {}),
            LLMTurn(text="O aparelho nao respondeu.", stop_reason="end_turn"),
        ]),
        settings,
        tools=ToolsQuebradas(),
    )
    resultado = await agente.run("abre o youtube")
    assert resultado.ok
    assert resultado.text == "O aparelho nao respondeu."


async def test_teto_de_rodadas_impede_loop_infinito(settings: Settings) -> None:
    tools = FakeTools({"loop": "de novo"})
    agente = Agent(
        FakeLLM([turno_de_ferramenta("loop", {}) for _ in range(20)]), settings, tools=tools
    )
    resultado = await agente.run("entra em loop")

    assert resultado.truncado
    assert resultado.rounds == settings.llm_max_rounds
    assert len(tools.executadas) == settings.llm_max_rounds


async def test_falha_de_modelo_vira_resposta_falavel(settings: Settings) -> None:
    resultado = await Agent(LLMQueQuebra([]), settings).run("qualquer coisa")
    assert not resultado.ok
    assert "modelo" in resultado.text.lower()
    assert resultado.erro is not None


async def test_recusa_do_modelo_e_tratada(settings: Settings) -> None:
    agente = Agent(FakeLLM([LLMTurn(text="", stop_reason="refusal")]), settings)
    resultado = await agente.run("algo bloqueado")
    assert resultado.stop_reason == "refusal"
    assert resultado.text


async def test_prompt_de_sistema_carrega_a_identidade(settings: Settings) -> None:
    prompt = Agent(FakeLLM([]), settings).system_prompt
    assert settings.user_name in prompt
    assert settings.user_honorific in prompt
    assert "sem markdown" in prompt.lower()


@pytest.mark.parametrize("sem_registro", [True])
async def test_sem_registro_de_ferramentas_o_modelo_recebe_erro(
    settings: Settings, sem_registro: bool
) -> None:
    cliente = FakeLLM([
        turno_de_ferramenta("optmus_web", {}),
        LLMTurn(text="Nao tenho essa capacidade ainda.", stop_reason="end_turn"),
    ])
    resultado = await Agent(cliente, settings).run("quanto gastei")
    resultado_ferramenta = cliente.chamadas[-1]["messages"][-1]["content"][0]
    assert resultado_ferramenta["is_error"] is True
    assert resultado.ok


# ------------------------------------------------------------------- imagem
QUADRO = Imagem(
    dados_b64="/9j/4AAQSkZJRg==",
    media_type="image/jpeg",
    largura=1024,
    altura=576,
    origem="webcam",
)


def _blocos_de_imagem(mensagens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        parte
        for mensagem in mensagens
        if isinstance(mensagem.get("content"), list)
        for bloco in mensagem["content"]
        if isinstance(bloco, dict) and isinstance(bloco.get("content"), list)
        for parte in bloco["content"]
        if isinstance(parte, dict) and parte.get("type") == "image"
    ]


async def test_ferramenta_devolve_imagem_ao_modelo(settings: Settings) -> None:
    """Sem isto uma ferramenta de visao nao existe: o conteudo era so string."""
    tools = FakeTools({"olhar": "Quadro capturado 1024x576."}, imagens={"olhar": QUADRO})
    cliente = FakeLLM([
        turno_de_ferramenta("olhar", {}),
        LLMTurn(text="Uma caneca azul.", stop_reason="end_turn"),
    ])
    await Agent(cliente, settings, tools=tools).run("o que voce esta vendo")

    conteudo = cliente.chamadas[-1]["messages"][-1]["content"][0]["content"]
    assert isinstance(conteudo, list), "com imagem, o tool_result vira lista de blocos"
    assert conteudo[0] == {"type": "text", "text": "Quadro capturado 1024x576."}
    assert conteudo[1] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": "/9j/4AAQSkZJRg==",
        },
    }


async def test_ferramenta_sem_imagem_continua_mandando_string(settings: Settings) -> None:
    """A forma antiga precisa sobreviver: e o que todas as outras usam."""
    tools = FakeTools({"optmus_web": "Gastou 3200 reais."})
    cliente = FakeLLM([
        turno_de_ferramenta("optmus_web", {}),
        LLMTurn(text="Tres mil e duzentos.", stop_reason="end_turn"),
    ])
    await Agent(cliente, settings, tools=tools).run("quanto gastei")

    conteudo = cliente.chamadas[-1]["messages"][-1]["content"][0]["content"]
    assert conteudo == "Gastou 3200 reais."


async def test_imagem_nao_e_reenviada_nas_rodadas_seguintes(settings: Settings) -> None:
    """A imagem vai uma vez; depois vira marcador.

    Com llm_max_rounds=6, um quadro de 1024x576 preso no contexto ate o fim do
    turno seria reenviado ate cinco vezes - ~3.900 tokens jogados fora.
    """
    tools = FakeTools(
        {"olhar": "Quadro capturado 1024x576.", "contas": "42"}, imagens={"olhar": QUADRO}
    )
    cliente = FakeLLM([
        turno_de_ferramenta("olhar", {}, id_="t1"),
        turno_de_ferramenta("contas", {}, id_="t2"),
        LLMTurn(text="Quarenta e dois reais no total.", stop_reason="end_turn"),
    ])
    await Agent(cliente, settings, tools=tools).run("soma o que esta escrito ai")

    ultimas = cliente.chamadas[-1]["messages"]
    assert _blocos_de_imagem(ultimas) == [], "nenhuma imagem sobrevive a rodada seguinte"

    # E o modelo nao fica sem explicacao para o que afirmou antes.
    textos = [
        parte["text"]
        for mensagem in ultimas
        if isinstance(mensagem.get("content"), list)
        for bloco in mensagem["content"]
        if isinstance(bloco, dict) and isinstance(bloco.get("content"), list)
        for parte in bloco["content"]
        if isinstance(parte, dict) and parte.get("type") == "text"
    ]
    assert MARCADOR_IMAGEM in textos
    assert "Quadro capturado 1024x576." in textos, "o texto da ferramenta fica intacto"


async def test_a_imagem_recem_capturada_sobrevive_ate_o_modelo_ver(
    settings: Settings,
) -> None:
    """O descarte roda ANTES de anexar o resultado novo.

    Invertido, a imagem seria apagada na mesma rodada em que chegou e a
    ferramenta de visao nunca funcionaria - o modelo receberia so o marcador.
    """
    tools = FakeTools({"olhar": "Quadro capturado."}, imagens={"olhar": QUADRO})
    cliente = FakeLLM([
        turno_de_ferramenta("olhar", {}),
        LLMTurn(text="Uma caneca.", stop_reason="end_turn"),
    ])
    await Agent(cliente, settings, tools=tools).run("o que voce ve")

    assert len(_blocos_de_imagem(cliente.chamadas[-1]["messages"])) == 1


def test_estimativa_de_tokens_da_imagem() -> None:
    """(largura x altura) / 750 - a conta que a Anthropic usa."""
    assert QUADRO.tokens_estimados == 786
    assert Imagem(dados_b64="x", largura=1280, altura=720).tokens_estimados == 1229
    assert Imagem(dados_b64="x").tokens_estimados == 0, "sem dimensao, nao inventa"
