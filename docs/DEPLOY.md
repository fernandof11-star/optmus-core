# Deploy do Optmus Core

## O que sobe e o que não sobe

O Core não é uma aplicação só — são duas naturezas no mesmo repositório, e só
uma delas cabe num container.

| Parte | Onde roda | Por quê |
|---|---|---|
| API, memória, ferramentas, consolidador, cérebro | **Nuvem** (Railway) ou local | É HTTP e estado; não precisa de hardware |
| Wake word, microfone, STT, TTS, HUD, celulares (F5) | **Só na sua máquina** | Nenhum container tem seu microfone, sua placa de som nem os celulares da sua mesa no USB |

A imagem Docker sobe **sem** o extra `[voz]` de propósito. Não é limitação de
build: é que `OPTMUS_VOICE_ENABLED=true` num container abriria um dispositivo de
áudio que não existe.

O desenho que funciona é **cérebro remoto, sentidos locais**: o Core na nuvem
guarda memória e executa ferramentas; a camada de voz roda na sua máquina e
conversa com ele por HTTP.

---

## ⚠ Antes de expor: o token da API

Enquanto o Core escuta em `127.0.0.1`, não ter autenticação é uma escolha
razoável — quem está na máquina já tem tudo. **Exposto na internet, a mesma API
entrega memória pessoal, execução de ferramenta, kill switch e o cérebro pago,
sem senha.**

Por isso o Core **recusa subir** com `OPTMUS_ENV=prod` ou host diferente de
`127.0.0.1` sem `OPTMUS_API_TOKEN`. Gere um:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Depois, toda chamada leva o header:

```bash
curl -H "Authorization: Bearer $OPTMUS_API_TOKEN" https://seu-core.up.railway.app/health
```

Só `/health/live` fica pública — é o que o orquestrador usa para saber se o
processo está de pé, e um healthcheck não deve carregar credencial.

---

## Railway (recomendado)

Container longevo com disco persistente. É o alvo que combina com este código.

```bash
npm i -g @railway/cli
railway login
railway init                 # ou: railway link, num projeto existente
```

**1. Crie o volume** — sem isso, toda a memória do Optmus é apagada a cada
deploy, porque o SQLite vive no layer efêmero do container.

No painel: **Settings → Volumes → New Volume**, mount path `/data`.

**2. Configure as variáveis** (painel ou CLI):

```bash
railway variables set \
  OPTMUS_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  OPTMUS_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  OPTMUS_ENV=prod \
  OPTMUS_DATA_DIR=/data \
  OPTMUS_HTTP_HOST=0.0.0.0 \
  OPTMUS_ANTHROPIC_API_KEY=sk-ant-... \
  OPTMUS_WEB_BASE_URL=https://jarvis-pessoal-nine.vercel.app \
  OPTMUS_WEB_PASSWORD=... \
  OPTMUS_VOICE_ENABLED=false
```

**3. Suba:**

```bash
railway up
railway logs         # confira "core.pronto"
```

**4. Verifique:**

```bash
curl https://seu-core.up.railway.app/health/live
curl -H "Authorization: Bearer $TOKEN" https://seu-core.up.railway.app/health
```

### Uma réplica, sempre

`railway.json` fixa `numReplicas: 1`, e isso é obrigatório enquanto o barramento
de eventos for em processo. Com duas réplicas: cada uma teria o próprio
barramento (eventos não se cruzam), duas instâncias do consolidador noturno
rodariam sobre o mesmo banco, e dois processos escreveriam no mesmo arquivo
SQLite pelo volume. Escalar horizontalmente exige antes trocar o barramento por
Redis — está previsto na F5 e a interface já foi desenhada para isso.

---

## Docker local (teste antes de subir)

```bash
docker build -t optmus-core .
docker run --rm -p 8420:8420 \
  -v optmus-data:/data \
  -e OPTMUS_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  -e OPTMUS_API_TOKEN=token-de-teste-bem-comprido \
  -e OPTMUS_ANTHROPIC_API_KEY=sk-ant-... \
  optmus-core
```

```bash
curl localhost:8420/health/live
curl -H "Authorization: Bearer token-de-teste-bem-comprido" localhost:8420/health
```

---

## Vercel: por que não

Vercel serverless **não roda este código**, e não é questão de configuração.
Quatro coisas quebram, todas por causa da mesma propriedade: a função morre
entre requisições.

| O que quebra | Por quê |
|---|---|
| **Banco (SQLite)** | O filesystem é efêmero e só `/tmp` é gravável. A memória inteira — episódica, semântica, procedural — some entre invocações. |
| **Consolidador noturno** | É uma task asyncio que dorme até as 4h. Não existe "noite" num processo que dura 300ms. Nunca roda. |
| **Barramento de eventos** | Pub/sub em processo, com assinantes em memória. Cada invocação começa com zero assinantes. |
| **Memória de trabalho e métricas** | Vivem em RAM com TTL de 30 min. Reiniciam a cada requisição; `/metrics` sempre responde zero. |

O que *caberia* em serverless seria um subconjunto sem estado — roteador,
chamada ao modelo, ferramentas puras — com Postgres externo no lugar do SQLite,
um cron externo no lugar do consolidador e Redis no lugar do barramento. Isso é
uma reescrita da camada de persistência inteira, não um `vercel.json`. Se você
quiser esse caminho, dá para desenhar, mas ele desmonta o F0–F2 e vale a pena
decidir com os olhos abertos.

Por isso não incluí um `vercel.json`: um arquivo que faz o deploy "passar" e o
sistema perder a memória em silêncio seria pior do que não ter.

---

## Variáveis de ambiente

**Obrigatórias na nuvem:**

| Variável | O que é |
|---|---|
| `OPTMUS_SECRET_KEY` | Chave mestra (≥32 chars). Sem ela o processo não sobe |
| `OPTMUS_API_TOKEN` | Bearer da API. Sem ele, exposto, o processo não sobe |
| `OPTMUS_ENV` | `prod` — desliga `/docs` e exige o token |
| `OPTMUS_DATA_DIR` | `/data`, apontando para o volume |
| `OPTMUS_HTTP_HOST` | `0.0.0.0` |
| `OPTMUS_ANTHROPIC_API_KEY` | O cérebro |

**Injetada pela plataforma:** `PORT` — o Core lê essa variável sem prefixo,
justamente porque Railway, Render e Fly a injetam assim.

**Recomendadas:**

| Variável | Valor | Por quê |
|---|---|---|
| `OPTMUS_VOICE_ENABLED` | `false` | Não há microfone num container |
| `OPTMUS_LOG_JSON` | `true` | Log estruturado para o agregador da plataforma |
| `OPTMUS_EMBEDDING_PROVIDER` | `fastembed` | A imagem já traz o extra `[memoria]` |
| `OPTMUS_EMBEDDING_DIM` | `384` | Dimensão do multilingual-e5-small |

> Trocar `OPTMUS_EMBEDDING_DIM` num banco que já tem vetores exige
> `POST /memoria/reindexar`. Faça isso **antes** de popular a memória, ou logo
> após o primeiro deploy.

O resto está documentado em `.env.example`.

---

## A camada de voz continua local

Com o Core na nuvem, a máquina local roda só percepção e fala, apontando para o
Core remoto. Isso **ainda não está implementado**: o loop de voz atual instancia
o agente em processo, não fala com um Core remoto por HTTP.

O que falta para o modo dividido: um cliente que chame
`POST /voz/texto` no Core remoto em vez do `Agent` local, e a camada de voz
passando a ser um processo separado do orquestrador. É uma fase própria — não
tente contornar isso rodando dois Cores completos, um em cada lado: eles teriam
duas memórias divergentes, e escolher qual está certa é o problema que a spec
manda evitar.

## Armadilhas de deploy que já custaram um dia

Três defeitos diferentes produziram a mesma tela — "o Core não sobe" — por
causas que não se parecem entre si. Ficam registrados com o sintoma exato,
porque é pelo sintoma que a gente volta aqui.

### `ModuleNotFoundError: No module named 'integrations'`

O `Dockerfile` não copiava `integrations/` nem `reports/`, e o `pyproject` não
os listava em `[tool.hatch.build.targets.wheel] packages`. **Não aparece em
teste local**: o venv de desenvolvimento está em modo editável, então o import
resolve de volta para o código-fonte e o container é o único lugar onde falta.

Para reproduzir sem Docker, isole a raiz do repositório do `sys.path` — só o
`site-packages` do venv fica:

```python
raiz = "C:/.../optmus-core"
sys.path = [p for p in sys.path if p.replace("\\", "/").rstrip("/") != raiz]
```

Cuidado: filtrar por "contém optmus-core" apaga o `site-packages` junto, porque
ele mora em `<repo>/.venv/Lib/site-packages`. Só a raiz exata sai.

### `RuntimeError: /dev/null is an empty file`

Vinha de `--log-config=/dev/null` no start command. O uvicorn passa o caminho
para `logging.config.fileConfig`, que faz `os.path.getsize` e recusa arquivo de
tamanho zero. **`/dev/null` tem tamanho zero no Linux também** — não é
peculiaridade do Windows. Quem manda no log da aplicação é o
`configure_logging()` do lifespan; o uvicorn pode ficar com a config dele.

### `/bin/bash: line 1: uvicorn: command not found`

O `CMD` chamava `uvicorn` direto. As duas formas rodam o mesmo código, mas
`uvicorn` depende do diretório de console scripts estar no PATH do shell que a
plataforma usa; `python -m uvicorn` depende só do interpretador. Use sempre a
segunda.

Se o erro persistir com `python -m uvicorn`, o problema não é o comando: é que
a plataforma **não está usando o Dockerfile**. Confirme no log de build —
build por Dockerfile mostra `load build definition from Dockerfile`; Nixpacks
mostra `setup │ python312`.

### O start command mora no Dockerfile, e só nele

`railway.json` **não** declara `startCommand` de propósito. Ele já esteve nos
dois lugares, e foi assim que um ficou com `--port $PORT` e o outro com
`--port 8000` fixo — que é apostar na porta que a plataforma injeta.

Vale lembrar que **a configuração do painel tem precedência sobre o
`railway.json`**. Se o painel tiver um Start Command digitado ou o Builder em
Nixpacks, o arquivo é ignorado em silêncio. Os dois campos precisam estar
vazios/em Dockerfile para este repositório mandar no próprio deploy.
