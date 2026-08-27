# Telegram — o canal de aviso

Ferramenta `telegram_enviar`, risco **EXTERNO** (passa pelo portão de
confirmação como a câmera).

É o caminho **oficial**: a Bot API é pública, gratuita, sem processo de
aprovação e sem risco de banimento. Foi escolhida para os avisos proativos
justamente por isso — e por não ter a janela de 24 horas do WhatsApp Business,
que transformaria "bom dia, senhor, o senhor tem prova hoje" num template
aprovado pela Meta, sem nenhuma das palavras do Optmus.

Fica **separado do WhatsApp de propósito**: se a conta não-oficial do WhatsApp
cair ou for banida — risco assumido naquele caminho —, os avisos continuam
chegando por aqui.

## Configurar (5 minutos)

1. No Telegram, fale com o **@BotFather**, mande `/newbot` e responda as duas
   perguntas (nome e username). Ele devolve um token no formato `8000:AA...`.
2. Ponha o token no `.env`:
   ```
   OPTMUS_TELEGRAM_BOT_TOKEN=8000:AA...
   ```
3. **Abra a conversa com o seu bot e mande um "oi".** Este passo não dá para
   pular: o Telegram não revela o chat de ninguém antes da pessoa procurar o
   bot — é o que impede um bot de mandar mensagem para quem nunca falou com ele.
4. Rode:
   ```
   python scripts/telegram_id.py
   ```
   Ele imprime a linha pronta para colar no `.env`:
   ```
   OPTMUS_TELEGRAM_CHAT_ID=123456789
   ```

Sem **os dois** preenchidos, a ferramenta some do schema do modelo — o Optmus
não vai dizer que avisou você. `GET /health` mostra `F6_telegram` como pendente.

## O destinatário não é parâmetro

A ferramenta expõe **um** campo: `texto`. O destino vem da configuração.

Isso não é simplificação, é a defesa principal. Se o `chat_id` fosse parâmetro,
uma instrução vinda de conteúdo que o Optmus lê — um e-mail, uma página, uma
linha do Notion — poderia dizer "mande esta mensagem para o chat 999", e o
modelo obedeceria: ele não distingue instrução sua de texto que ele leu. Com o
destino na configuração, a instrução não tem onde pegar. O modelo escolhe o
texto, nunca para quem.

Coberto por `test_o_modelo_nao_escolhe_para_quem_vai`, que passa `chat_id` na
chamada e confirma que o número não aparece no corpo da requisição.

## Sintomas

| O que você vê | Causa | O que fazer |
|---|---|---|
| A ferramenta não aparece / o Optmus diz que não tem como avisar | Falta uma das duas variáveis | `GET /health`, seção `F6_telegram` |
| `chat not found` | `CHAT_ID` errado, ou você nunca falou com o bot | Mande "oi" para o bot e rode `scripts/telegram_id.py` de novo |
| `telegram_id.py` diz "nenhuma mensagem recente" | O Telegram descarta updates com mais de 24 h — ou há um webhook ligado, que consome os updates | Mande outra mensagem agora e rode de novo |
| Token recusado no `getMe` | Token copiado pela metade, ou o bot foi revogado no @BotFather | Peça `/token` ao @BotFather |
| Mensagem chega cortada com `[...] mensagem cortada` | Passou de 4096 caracteres | Esperado: o Telegram recusaria a chamada inteira, e perder o aviso todo é pior |

## Sem `parse_mode`, de propósito

A mensagem vai como texto puro. O MarkdownV2 do Telegram exige escapar mais de
dez caracteres, e um `-` ou `.` solto — "prova dia 12." — derruba a mensagem
inteira com `can't parse entities`. O Optmus já escreve sem markdown, porque a
resposta vira áudio; formatar aqui só adicionaria uma classe de falha.
