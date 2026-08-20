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

## Pendência aberta — confirmação de ação EXTERNA não tem interface

**Registrada em 2026-08-20. Prioridade a decidir depois da F6 do frontend.**

Ações de risco `EXTERNO` — hoje só a câmera (`olhar`), amanhã WhatsApp e
Instagram na F6 do Core — não executam direto. A política cria uma confirmação
pendente e devolve um token; quem confirma é um humano, por
`POST /seguranca/confirmar`.

**Esse humano não tem por onde confirmar.** O frontend web não tem tela de
confirmação, e o caminho de voz depende do loop local. Consequência prática:

- pelo frontend, `olhar` **nunca executa** — para no portão e fica lá;
- o modelo recebe "AGUARDANDO CONFIRMACAO", diz ao usuário para confirmar, e
  não há como;
- o token expira sem uso.

Hoje isso falha do lado seguro: nada acontece sem autorização, que é o
comportamento correto. Mas é um elo faltante, não uma decisão — e quando a F6
do Core trouxer mensagens para terceiros, "não dá para confirmar" deixa de ser
inconveniente e passa a ser funcionalidade inteira inacessível.

O que falta, quando for a hora:

1. `GET /seguranca/pendentes` já existe e lista o que aguarda.
2. Uma tela que mostre `resumo` (a frase pensada para ser lida em voz alta,
   tipo "ligar a webcam e ler o que estiver na frente da camera") e ofereça
   confirmar ou recusar.
3. `POST /seguranca/confirmar` com o token — e a frase-código, quando o risco
   for `DESTRUTIVO`.

O ponto delicado do desenho: a confirmação precisa acontecer **no dispositivo
de quem autoriza**, não em qualquer sessão autenticada. Um token de
confirmação que qualquer aba com o bearer possa aprovar reduz o portão a um
clique a mais.
