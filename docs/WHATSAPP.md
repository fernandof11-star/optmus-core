# WhatsApp — caminho não oficial, local

Ferramenta `whatsapp_enviar`, risco **EXTERNO**. Manda mensagem de texto para
alguém de uma lista de contatos que **você** escreve à mão.

## Leia isto antes de instalar

Isto usa o protocolo do WhatsApp Web por engenharia reversa, via
[neonize](https://github.com/krypton-byte/neonize) (que empacota o whatsmeow, em
Go). Não é suportado pela Meta e viola os termos de uso.

**A conta será banida. A dúvida é quando.** Clientes assim duram tipicamente de
[duas a oito semanas](https://wapisimo.dev/blog/en/whatsapp-unofficial-api-ban-risk);
a detecção é automática e acontece na camada de rede, antes de qualquer mensagem
ser lida por uma pessoa. Não há padrão previsível nem recurso.

Some a isso: em **15/01/2026** a Meta passou a
[bloquear assistentes de IA de propósito geral no WhatsApp](https://chatboq.com/blogs/third-party-ai-chatbots-ban).
A regra é escrita para a Business API oficial e a fonte não é explícita sobre uso
pessoal em conta própria — mas o perfil de comportamento do Optmus é exatamente
o que passou a ser caçado, o que empurra a estimativa para o lado pessimista.

Portanto: **número secundário, sempre.** Nunca o principal.

### Por que não o caminho oficial

A Business API tem janela de 24 horas e mensagens fora dela precisam ser
*templates* aprovados pela Meta. "Bom dia, senhor, o senhor tem prova hoje"
viraria um formulário sem nenhuma das palavras do Optmus. Para avisos proativos
existe o **Telegram**, que é oficial, gratuito e sem janela — ver
`docs/TELEGRAM.md`. Os dois são canais separados de propósito: se este cair, os
avisos continuam.

## Três restrições que não são configuráveis

**1. Só local.** `disponivel()` chama `hospedado()` **antes** de olhar qualquer
configuração. Se detectar as variáveis que Railway, Render, Heroku ou Fly
injetam, a ferramenta se recusa a existir — mesmo com `OPTMUS_WHATSAPP_ENABLED=true`
e sessão pareada. Não é "desligado por configuração", é impossível. Rodar isto
num datacenter seria mandar mensagem de conta pessoal a partir de um IP
compartilhado, o jeito mais rápido conhecido de perder o número.

**2. Nunca responde sozinho.** Nenhum tratador de evento é registrado. O Optmus
manda quando você autoriza, e só. Resposta automática é o comportamento que mais
rápido derruba a conta — e seria também o caminho por onde uma injeção vinda de
uma mensagem *recebida* se propagaria sem ninguém no meio.

**3. O modelo nunca digita um número.** Ver abaixo.

## A lista de contatos — onde mora a defesa

No Telegram o destino fica na configuração, então o modelo escolhe o texto e
nunca para quem. Aqui o propósito é falar com **outras pessoas**, então o
destinatário é parâmetro — e parâmetro de destinatário é exatamente o que uma
instrução injetada tentaria usar.

A solução: o modelo escolhe um **apelido** de uma lista sua. Número cru é
recusado em qualquer formato (`5511999998888`, `+55 11 99999-8888`,
`(11) 99999-8888`…), e a recusa acontece **antes** da busca — se fosse depois,
bastaria a lista ganhar uma chave numérica, por descuido ou edição, para o
caminho "modelo escolhe número arbitrário" reabrir sem ninguém perceber.

`data/contatos.json` (fora do controle de versão — são números de gente real):

```json
{
  "mãe":  {"numero": "5511987654321", "nome": "Mãe"},
  "joão": {"numero": "5511912345678", "nome": "João Silva"}
}
```

Número em **E.164 sem o `+`**: país, DDD, número. Acento e maiúscula no apelido
não importam (`Mãe`, `mae`, `MÃE` são a mesma chave). Um número malformado
derruba a lista inteira com erro — em vez de sumir em silêncio e virar "contato
desconhecido" na hora do uso, mandando você procurar o defeito no lugar errado.

**Sem contatos na lista, a ferramenta não aparece para o modelo.** Oferecer envio
sem ninguém para quem enviar faria ele prometer uma mensagem sem destino.

## Instalar

```
pip install -e ".[whatsapp]"
python scripts/whatsapp_parear.py +55DDNUMERO
```

Troque `+55DDNUMERO` pelo **seu número secundário de verdade**, com país e DDD.
Não é figura de linguagem: em 25/08/2026 este exemplo era um número plausível,
foi copiado literalmente, e o pareamento falhou deixando uma sessão pela metade
que o Core aceitou como válida. Os dois lados disso estão corrigidos — o script
apaga a sessão incompleta e o Core confere aparelho vinculado em vez da
existência do arquivo — mas o número ainda precisa ser o seu.

O script mostra um código de vínculo. No telefone desse número:
**WhatsApp → Aparelhos conectados → Conectar aparelho → Conectar com número de
telefone**, e digite o código.

Depois, no `.env`:

```
OPTMUS_WHATSAPP_ENABLED=true
```

E crie o `data/contatos.json`.

O pareamento fica fora do Core de propósito: se o Core pareasse sozinho, um
código de vínculo apareceria num log — e quem visse o log entraria na conta.

## O portão de confirmação

`whatsapp_enviar` é **EXTERNO**, então passa pela tela de confirmação. Desde
23/08/2026 essa confirmação exige prova do dispositivo que originou o pedido
(`docs/SEGURANCA.md`) — o vínculo de dispositivo foi construído **antes** desta
ferramenta justamente por causa dela: a ação autorizada aqui é mandar mensagem
para outra pessoa, e uma confirmação que qualquer sessão pudesse dar não seria
confirmação.

A frase do portão mostra **nome e os quatro últimos dígitos**:

> mandar no WhatsApp para João Silva (final 5678): "chego 19h"

O final existe porque apelido errado é o erro plausível aqui — dois "joão" na
lista, ou o modelo escolhendo o parecido. O número inteiro não entra: essa frase
vai para a trilha de auditoria permanente, e número de terceiro não precisa
morar nela. Pelo mesmo motivo, o metadado guarda apelido e final, nunca o número.

## Sintomas

| O que você vê | Causa | O que fazer |
|---|---|---|
| A ferramenta não aparece | Uma das quatro portas fechada | O log diz qual: `whatsapp.indisponivel`, campo `motivo` |
| `plataforma hospedada` | Está rodando no Railway/Render | Esperado e correto. Só local |
| `sem sessao pareada` | Nunca pareou, ou apagou o `.db` | `python scripts/whatsapp_parear.py` |
| `NENHUM aparelho esta vinculado` | O pareamento não completou (código expirado, número errado) | Rode o script de novo — ele limpa a sessão pela metade sozinho |
| `nao consegui ler a sessao` | Arquivo corrompido ou travado por outro processo | Veja se sobrou um `whatsapp_parear.py` rodando; senão, apague e pareie |
| `sessao existe mas nao esta logada` | O telefone desvinculou o aparelho | Parear de novo |
| `'x' nao esta na lista de contatos` | Apelido fora da lista | Adicione em `data/contatos.json`. **Não** contorne com número |
| `numero de telefone nao e aceito` | Algo tentou escolher o destinatário | Se você não pediu isso, olhe o que o Optmus leu antes |
| Envio demora ~3 s | Espaçamento mínimo entre mensagens | Proposital: rajada é o sinal de automação mais óbvio |

## O que não foi verificado ponta a ponta

O contrato do neonize foi lido por introspecção da biblioteca instalada, não da
documentação: `NewAClient(name)`, `send_message(to: JID, message: str)`,
`build_jid(phone_number, server='s.whatsapp.net')`, `PairPhone(phone, show_push_notification) -> str`,
e `is_connected`/`is_logged_in` como propriedades.

**O pareamento e o envio real nunca rodaram** — não há como, sem um telefone e um
número secundário. Os testes cobrem resolução de contato, disponibilidade e
tratamento de falha com um cliente falso; a camada que fala com o WhatsApp de
verdade só será exercida quando você parear. É o primeiro lugar a olhar se algo
não funcionar.
