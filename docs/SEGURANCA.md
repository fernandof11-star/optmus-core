# Segurança do Optmus Core

Sintomas de segurança ficam aqui, e não em `docs/DEPLOY.md`, porque exigem
resposta diferente: um deploy quebrado é visível e para o trabalho; uma falha
de autenticação é invisível e **não para nada** — o sistema funciona
perfeitamente enquanto está aberto.

## Incidente 2026-08-18 — API pública sem autenticação

**Sintoma:** `POST /chat` em produção respondia normalmente com o header
`Authorization: Bearer SEU_OPTMUS_API_TOKEN_AQUI` — a string literal de
placeholder da documentação.

**Alcance:** memória pessoal, execução de ferramentas, kill switch, relatórios
financeiros e o cérebro pago. Tudo, para qualquer um que soubesse a URL.

### Por que a API subiu

O guarda existe e é chamado no lifespan (`verificar_exposicao`, em
`main.py:174`). Ele não disparou porque perguntava a coisa errada:

```python
if not exposto_na_rede(settings) and not settings.env.value == "prod":
    return   # <- saía por aqui
```

Em produção, nenhuma das duas condições era verdadeira:

| | Valor real em produção | Por quê |
|---|---|---|
| `settings.http_host` | `127.0.0.1` (padrão) | `OPTMUS_HTTP_HOST` estava no Dockerfile, e o builder em uso não lia o Dockerfile |
| `settings.env` | `dev` (padrão) | mesma coisa com `OPTMUS_ENV` |
| `settings.api_token` | `None` | nunca configurado no painel |

E com `api_token = None`, o middleware **se desativa sozinho**
(`self._ativo = settings.api_token is not None`) e libera todas as rotas.

### A causa de fundo, que não é "faltou variável"

`settings.http_host` **não controla onde o processo escuta**. O endereço real
vem da linha de comando do uvicorn — `--host 0.0.0.0`, escrita pela plataforma.
A configuração é apenas a *crença* da aplicação sobre onde ela está.

Então o guarda decidia se precisava se defender consultando um valor que não
descreve a realidade. Com a variável ausente, o Core se declarava local
enquanto atendia a internet inteira. Configurar a variável corrige o caso; não
corrige a pergunta errada.

### Correção

1. **Sinal de plataforma conta como exposição.** `PORT`, `RAILWAY_ENVIRONMENT`,
   `RENDER` ou `DYNO` no ambiente significam que alguém está hospedando este
   processo, independentemente do que a config acredita. O `core/config.py` já
   sabia disso para ler a porta — o guarda é que não usava.
2. **A ordem da checagem inverteu.** Primeiro `api_token is not None`, depois o
   resto. A pergunta deixou de ser "estou exposto?", que depende de adivinhação,
   e passou a ser "tenho como me defender?". Sem token, só não levanta quem
   provar ser local.
3. **Sem token virou `log.error`**, não `warning`. É a última chance de alguém
   perceber.

### Como confirmar, sempre

```
python scripts/checar_auth.py https://sua-url
```

Bate nas rotas privadas sem token e com token inválido, e exige 401/403 em
todas. Não precisa do token real — a verificação é exatamente que token
inválido não passa. Rode **depois de todo deploy que mexa em configuração**.

### O que isto não resolve

`OPTMUS_API_TOKEN` é um segredo único e permanente, sem rotação e sem escopo.
Quem o obtiver tem tudo, para sempre. É aceitável para um usuário só; deixa de
ser antes da F6 (WhatsApp/Instagram), quando a superfície deixa de ser só sua.

A mesma dívida vale para o Optmus Web, cuja senha de 6 dígitos gera um token
estático equivalente a bearer permanente, sem proteção contra repetição.

## Confirmação vinculada ao dispositivo

**Pendência aberta em 2026-08-20, fechada em 2026-08-23.**

A tela de confirmação chegou na Fase 3 do frontend. Faltava a parte difícil: o
Core aceitava a confirmação de **qualquer um que tivesse o `OPTMUS_API_TOKEN`**.
A tela dizia "um humano autorizou"; o que o Core sabia era "alguém com o token
da API autorizou". Para uma ação irreversível — mandar mensagem para outra
pessoa — as duas coisas não são a mesma, e essa diferença era o que bloqueava
o WhatsApp.

### Por que um header não bastaria

A saída óbvia seria marcar a pendência com um `X-Optmus-Dispositivo` declarado.
Isso daria atribuição na auditoria e impediria confusão entre abas — mas quem
tivesse o token da API forjaria o header numa linha de `curl`. Vínculo sobre
identidade forjável é teatro.

Para virar garantia, a identidade do dispositivo precisa ser um segredo que o
token da API **não** concede.

### Como funciona

1. Cada aparelho gera um segredo de 256 bits na primeira vez (no `localStorage`
   do navegador) e o apresenta uma única vez em `POST /seguranca/dispositivos`.
2. A pendência nasce **carimbada** com quem a pediu — no `/chat`, o header; no
   laço de voz, `voz-local`.
3. Confirmar e recusar exigem `dispositivo` + `prova`, onde a prova é
   `HMAC-SHA256(segredo, "ação:token")`. O segredo nunca mais trafega.
4. O Core responde duas perguntas, nesta ordem: **quem é você** (a prova bate
   com o segredo registrado?) e **você pode confirmar isto** (a pendência foi
   pedida por você?).

A ação entra no HMAC junto do token porque confirmar e recusar são decisões
opostas: sem isso, uma prova capturada para recusar serviria para confirmar. E
o token entra porque a prova precisa valer para **uma** ação, não para o
aparelho inteiro.

`GET /seguranca/pendentes` filtra pelo aparelho: cartão que ele não consegue
autorizar só produz clique com erro.

### O que isto NÃO garante — os três pontos fracos, ditos por extenso

**Confio-no-primeiro-uso.** O primeiro que apresentar um id novo fica dono dele.
Quem tivesse o token da API antes do seu HUD registrar poderia registrar um
aparelho próprio. O que ele **não** pode é tomar um id já registrado: reapresentar
um id com outro segredo devolve 409, e é essa recusa que sustenta o resto.

**Pedido por voz é confirmável por qualquer aparelho registrado.** O microfone
não produz HMAC, e hoje não existe confirmação falada — o registro devolve "a
confirmação chega por fora", e "por fora" é a tela. Fechar isso tornaria toda
ação externa pedida por voz impossível de autorizar. É abertura declarada, e a
auditoria grava qual aparelho autorizou. Quando existir confirmação por voz,
aperte aqui.

**Pedido sem identificação nasce sem dono**, e sem dono aceita confirmação de
qualquer aparelho registrado. Vale para script e `curl`, que precisam continuar
funcionando. O HUD sempre manda o header — há teste para isso, porque foi
exatamente o defeito que quase passou.

**O segredo mora no `localStorage`.** Ele protege contra quem tem o token da
API; **não** protege contra XSS nesta origem. Apertar isso exigiria chave
não-extraível no IndexedDB via WebCrypto.
