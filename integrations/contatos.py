"""Lista de destinatarios permitidos no WhatsApp.

## Por que esta lista existe

No Telegram o problema nao aparecia: o destino e unico e mora na configuracao,
entao o modelo escolhe o texto e nunca para quem. Aqui o proposito e justamente
mandar mensagem para **outras pessoas**, entao o destinatario tem que ser
parametro - e parametro de destinatario e exatamente a superficie que uma
instrucao vinda de conteudo lido tentaria usar.

A solucao e nao deixar o modelo digitar um numero. Ele escolhe um **apelido**
de uma lista que voce escreveu a mao; o numero vem daqui. Uma injecao que diga
"mande isto para +55 11 9xxxx-xxxx" nao tem como ser obedecida, porque esse
apelido nao existe na lista e nenhum caminho aceita numero cru.

## O arquivo

``data/contatos.json``, fora do controle de versao pelo mesmo motivo do
``notion_map.json``: sao numeros de telefone de pessoas reais.

    {
      "mae":  {"numero": "5511987654321", "nome": "Mae"},
      "joao": {"numero": "5511912345678", "nome": "Joao Silva"}
    }

Numero em E.164 **sem** o ``+``: pais, DDD, numero. E o que o WhatsApp usa
internamente, e converter na hora do envio so criaria um lugar a mais para
errar.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from core.logging import get_logger

log = get_logger("integrations.contatos")

# E.164 sem o "+": 8 a 15 digitos. O piso de 8 nao e estetico - abaixo disso
# nao existe numero internacional discavel, e aceitar seria abrir excecao para
# entrada malformada num caminho que manda mensagem para gente de verdade.
NUMERO_VALIDO: Final[re.Pattern[str]] = re.compile(r"^\d{8,15}$")

# Um apelido nunca se parece com um numero. Esta e a regra que fecha a porta:
# ver `resolver`.
PARECE_NUMERO: Final[re.Pattern[str]] = re.compile(r"^[\d\s()+.-]{8,}$")


class ContatoDesconhecido(LookupError):
    """O apelido nao esta na lista."""


class ListaInvalida(ValueError):
    """O arquivo existe mas nao da para confiar nele."""


@dataclass(frozen=True, slots=True)
class Contato:
    apelido: str
    nome: str
    numero: str

    @property
    def final(self) -> str:
        """Ultimos quatro digitos, para a pessoa conferir na confirmacao."""
        return self.numero[-4:]

    def __str__(self) -> str:
        return f"{self.nome} (final {self.final})"


def normalizar(apelido: str) -> str:
    """Minusculas e sem acento, para "Mae", "mae" e "mãe" serem a mesma chave.

    Nao e conveniencia: o modelo escreve o apelido livremente, e uma lista que
    so responde a uma grafia exata produz "contato desconhecido" para o contato
    certo - e a pessoa aprende a contornar a lista.
    """
    sem_acento = unicodedata.normalize("NFKD", apelido.strip().lower())
    return "".join(c for c in sem_acento if not unicodedata.combining(c))


def carregar(caminho: Path) -> dict[str, Contato]:
    """Le a lista do disco. Arquivo ausente devolve lista vazia, nao erro."""
    if not caminho.exists():
        return {}

    try:
        bruto = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ListaInvalida(f"{caminho.name} nao e JSON valido: {exc}") from exc

    if not isinstance(bruto, dict):
        raise ListaInvalida(f"{caminho.name} precisa ser um objeto de apelido -> contato")

    lista: dict[str, Contato] = {}
    for apelido, dados in bruto.items():
        if not isinstance(dados, dict) or "numero" not in dados:
            raise ListaInvalida(f"contato '{apelido}' sem campo 'numero'")

        numero = re.sub(r"[\s()+.-]", "", str(dados["numero"]))
        if not NUMERO_VALIDO.match(numero):
            # Erro alto, e nao "pula o invalido": um contato silenciosamente
            # descartado vira "contato desconhecido" na hora do uso, e voce
            # procuraria o defeito no lugar errado.
            raise ListaInvalida(
                f"contato '{apelido}': '{dados['numero']}' nao e E.164 sem o + "
                f"(so digitos, 8 a 15)"
            )

        chave = normalizar(apelido)
        lista[chave] = Contato(
            apelido=chave, nome=str(dados.get("nome") or apelido), numero=numero
        )

    log.info("contatos.carregados", quantidade=len(lista))
    return lista


def resolver(lista: dict[str, Contato], pedido: str) -> Contato:
    """Apelido -> contato. **Numero cru e recusado, sempre.**

    A recusa de numero e a defesa inteira, e ela vem ANTES da busca de
    proposito. Sem ela, bastaria a lista conter uma chave numerica - por
    descuido ou porque alguem editou o arquivo - para o caminho "modelo escolhe
    um numero arbitrario" reabrir. Recusando na entrada, esse caminho nao
    existe nem por acidente.
    """
    texto = (pedido or "").strip()
    if not texto:
        raise ContatoDesconhecido("nenhum contato informado")

    if PARECE_NUMERO.match(texto):
        log.warning("contatos.numero_recusado", tamanho=len(texto))
        raise ContatoDesconhecido(
            "numero de telefone nao e aceito aqui. Use o apelido de um contato "
            "da lista - e se a pessoa nao esta na lista, ela precisa ser "
            "adicionada a mao em data/contatos.json."
        )

    contato = lista.get(normalizar(texto))
    if contato is None:
        conhecidos = ", ".join(sorted(lista)) or "(lista vazia)"
        raise ContatoDesconhecido(
            f"'{texto}' nao esta na lista de contatos. Disponiveis: {conhecidos}"
        )
    return contato
