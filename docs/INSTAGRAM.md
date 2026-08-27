# Instagram — leitura da própria conta

Duas ferramentas, ambas de risco **LEITURA** (não passam pelo portão de
confirmação, porque nada sai da conta):

- `instagram_resumo` — perfil, seguidores, variação desde a última checagem,
  métricas do dia.
- `instagram_comentarios` — comentários recentes nas últimas publicações.

## O que a API oficial não faz

Isto não é limitação do Optmus, é da plataforma. Os endpoints **não existem**:

| Você pode querer | Existe? |
|---|---|
| Saber **quantos** seguidores você tem, e quantos entraram | sim (`followers_count`, comparado com a leitura anterior) |
| Saber **quem** te seguiu | **não** — a API devolve o total, nunca a lista |
| Nome, foto ou vínculo de um seguidor novo | **não** |
| Seguir / deixar de seguir alguém | **não** — nenhum endpoint, em nenhuma versão |
| Receber aviso em tempo real de seguidor novo | **não** — os 15 webhooks cobrem comentários, menções, mensagens e stories |

O único caminho para isso é a API privada (não oficial), com risco alto de
banimento — follow/unfollow automatizado é o comportamento que o Instagram mais
pune, e não dá para mitigar com conta secundária, já que a graça é a conta real.

`instagram_resumo` diz isso no próprio texto que o modelo lê. Sem esse aviso o
modelo tem um número na mão, nenhuma indicação de que a lista não existe, e
inventa um nome quando você perguntar "quem me seguiu?".

## Configurar

Caminho usado: **Instagram API with Instagram Login** (`graph.instagram.com`).
Não precisa de Página do Facebook — só o outro caminho (Facebook Login for
Business) exige isso.

1. Sua conta precisa ser **Professional** (Business ou Creator). Conta pessoal
   não acessa a API de jeito nenhum. Instagram → Configurações → Tipo de conta.
2. Em `developers.facebook.com` → **Criar app** → tipo **Business**.
3. Adicione o produto **Instagram** → *API setup with Instagram login*.
4. Em **Instagram → Roles**, adicione a sua conta como **Instagram Tester**, e
   aceite o convite pelo Instagram (Configurações → Apps e sites → Convites).
5. Deixe o app em **Development Mode**. Nesse modo ele funciona para contas com
   papel atribuído — que é o seu caso — e **não precisa de App Review**. O
   review só entra quando outras pessoas conectam as contas delas.
6. Gere o token pelo painel (*Generate token*), autorize, e copie.
7. Descubra o ID: `GET https://graph.instagram.com/me?fields=user_id,username&access_token=SEU_TOKEN`
8. No `.env`:
   ```
   OPTMUS_INSTAGRAM_TOKEN=IGQ...
   OPTMUS_INSTAGRAM_ACCOUNT_ID=17841...   # o user_id, não o @
   ```

Sem as duas variáveis, as ferramentas somem do schema do modelo.

## O prazo de 60 dias — o risco real desta integração

O token longo vale **60 dias**. Renová-lo exige que ele tenha **≥ 24 h de idade**
e **ainda não tenha expirado**. Token vencido **não ressuscita**: a única saída é
refazer o passo 6 na mão.

O Core renova sozinho quando faltam ≤ 10 dias, de forma preguiçosa (no uso, sem
agendador). Duas consequências que nenhum código resolve:

- Se o Core ficar **60 dias corridos desligado**, o token morre.
- Logo depois de configurar, a renovação falha com *"token is not old enough"*.
  Isso é **normal** e não quebra nada — a próxima leitura tenta de novo.

`instagram_resumo` avisa no texto quando faltam ≤ 10 dias, e o prazo é
arredondado **para baixo** de propósito: para um vencimento sem volta,
subestimar faz você agir cedo; superestimar faz agir tarde demais.

Enquanto o token nunca foi renovado, o prazo aparece como desconhecido — a Meta
não informa a validade de um token que ela não acabou de emitir, e mostrar "60
dias" ali seria inventar a data que você usaria para decidir quando agir.

## Sintomas

| O que você vê | Causa | O que fazer |
|---|---|---|
| As ferramentas não aparecem | Falta uma das duas variáveis | `GET /health`, seção `F6_instagram` |
| `Invalid OAuth access token (code 190)` | Token vencido ou revogado | Refaça o passo 6 |
| `token is not old enough` no log | Token com menos de 24 h | Nada — é esperado, resolve sozinho |
| Métrica como `—` | A Meta devolveu conjunto vazio | Esperado: ela não devolve 0, devolve nada. `—` é honesto, `0` seria invenção |
| `(#100) Tried accessing nonexisting field` | A Meta mudou o contrato de campos | Ajuste `CAMPOS_*` em `integrations/instagram.py` |
| "primeira leitura, sem base de comparação" | Ainda não houve leitura anterior | Some sozinho na segunda chamada |

## Contrato verificado — e a parte que não foi

Perfil, insights (métricas, `impressions` → `views`), renovação de token e modo
dev foram conferidos na documentação da Meta em 23/08/2026.

**Comentários e mídias não foram.** A referência devolveu 404 nas duas tentativas,
então os campos em `CAMPOS_MIDIA` e `CAMPOS_COMENTARIO` vêm do contrato
estabelecido, não de leitura da fonte naquele dia. Por isso os erros da Meta são
repassados **literalmente** — se algum campo mudou, o sintoma é
`Tried accessing nonexisting field: <nome>`, e não silêncio. É o primeiro teste
a fazer quando o token existir.
