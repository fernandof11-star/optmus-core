# Optmus Core

Assistente ambiente da Montlux: processo local, sempre ligado, que ouve por voz,
mantém memória de longo prazo, opera dispositivos físicos e se apresenta por uma
interface visual reativa.

> **Este repositório não é o Optmus Web.** O Optmus Web é um projeto separado,
> vivo e intocado — API serverless em Node/TS na Vercel, dona dos dados
> estruturados no Notion. O Core **consome** essa API como serviço externo e
> nunca duplica os domínios dela. Não altere o repositório do Web a partir daqui:
> se precisar de algo que a API não expõe, peça.

| | Optmus Web | Optmus Core (aqui) |
|---|---|---|
| Natureza | API serverless, sob demanda | Processo local, sempre ligado |
| Stack | Node/TS, Express 5, Vercel | Python 3.12 |
| Guarda | Dados estruturados (Notion) | Memória conversacional + vetorial |
| Roda | Nuvem | Sua máquina |

---

## URL estável do Optmus Web

> **Preencher e manter atualizado. O mesmo campo deve existir no README do Optmus Web.**

| Campo | Valor |
|---|---|
| Domínio de produção (estável) | `https://jarvis-pessoal-nine.vercel.app` |
| Ambiente de preview | URLs de preview da Vercel mudam a cada deploy — **nunca** usar em `.env` |
| Variável no `.env` | `OPTMUS_WEB_BASE_URL` |

O domínio nunca é hardcoded no código: sai de `OPTMUS_WEB_BASE_URL`.

---

## Requisitos

- **Python 3.12** — o piso técnico é 3.11, mas use 3.12.
  `faster-whisper`, `openwakeword` e `mediapipe` (F1 e F8) **não publicam wheels
  acima de 3.12**. A F0 roda em 3.13/3.14; a F1 não. O `/health` avisa quando o
  interpretador está acima do suportado.
- Sem Docker. Sem GPU (o STT roda em CPU, `small` + `int8`).
- Windows, Linux ou macOS.

---

## Subir do zero

```bash
git clone <este-repo> optmus-core
cd optmus-core

# 1. venv (Windows: py -3.12 -m venv .venv && .venv\Scripts\activate)
python3.12 -m venv .venv
source .venv/bin/activate

# 2. dependências (-e = editável, o código roda direto da pasta)
pip install -e ".[dev]"          # núcleo + testes
pip install -e ".[dev,llm]"      # + SDK da Anthropic (cérebro)
pip install -e ".[dev,memoria]"  # + embeddings semânticos locais (fastembed)
pip install -e ".[dev,voz]"      # + microfone, wake word, STT (exige Python 3.12)

# 3. configuração
cp .env.example .env            # Windows: copy .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"
#   -> cole em OPTMUS_SECRET_KEY dentro do .env. O processo NÃO sobe sem ela.

# 4. testes
pytest -q

# 5. rodar
uvicorn main:app --reload       # ou: optmus
```

Abra <http://127.0.0.1:8420/health>. Docs interativas em `/docs`.

Para subir na nuvem (Railway/Docker), veja **[docs/DEPLOY.md](docs/DEPLOY.md)** —
e leia a seção do token antes de expor: a API entrega memória pessoal e execução
de ferramenta, e o Core recusa subir exposto sem `OPTMUS_API_TOKEN`.

O banco é criado sozinho em `data/optmus.db` no primeiro boot, junto com as
migrations. **Backup = copiar esse arquivo.**

---

## Verificar que a F0 está de pé

```bash
curl http://127.0.0.1:8420/health

curl -X POST http://127.0.0.1:8420/events \
  -H "content-type: application/json" \
  -d '{"type":"sistema.teste","payload":{"origem":"curl"}}'

curl "http://127.0.0.1:8420/events/recent?limit=5"
```

O evento publicado deve aparecer em `/events/recent` e o contador
`barramento.publicados` do `/health` deve subir.

---

## Endpoints

| Método | Rota | Para quê |
|---|---|---|
| `GET` | `/health` | Estado real: persistência, barramento, voz, degradações, config pendente por fase |
| `GET` | `/metrics` | Latência por etapa (p50/p95/máx) e turnos acima da meta |
| `POST` | `/events` | Publica evento no barramento (teste e, depois, webhooks) |
| `GET` | `/events/recent` | Últimos eventos persistidos, filtráveis por `type` |
| `POST` | `/voz/texto` | Injeta uma fala já transcrita — o pipeline inteiro, sem microfone |
| `POST` | `/voz/gatilho` | Wake word manual (HUD, atalho, testes) |
| `GET` | `/memoria/buscar` | Busca nas camadas permanentes, com o score aberto |
| `POST` | `/memoria/fato` | Grava fato semântico; `supersedes` versiona o antigo |
| `GET` | `/memoria/rotinas` | Rotinas derivadas pelo consolidador |
| `POST` | `/memoria/consolidar` | Roda o "sono" sob demanda, sem esperar a madrugada |
| `POST` | `/memoria/reindexar` | Recalcula vetores após trocar de modelo de embedding |
| `GET` | `/ferramentas` | O que o Optmus sabe fazer, com o risco de cada uma |
| `GET` | `/ferramentas/optmus-web/diagnostico` | Sonda o Web e mostra o que foi enviado/recebido |
| `GET` | `/seguranca/pendentes` | Ações de risco esperando confirmação humana |
| `POST` | `/seguranca/confirmar` | Libera uma ação retida pela política |
| `GET` | `/seguranca/auditoria` | Trilha append-only de tudo que executou |
| `POST` | `/sistema/parar` | Kill switch: aborta a fala em andamento |

`/health` responde `status: "degradado"` — e não erro — quando o sistema perdeu
capacidade mas continua de pé (ex.: `sqlite-vec` não carregou). Degradação
graciosa é princípio, não exceção: o Optmus nunca morre, ele reduz capacidade e
avisa.

---

## Arquitetura

```
core/config.py      configuração tipada (pydantic-settings), fail-fast
core/logging.py     structlog: console em dev, JSON em prod. Nenhum print no projeto.
core/bus.py         EventBus (ABC) + InProcessEventBus (asyncio)
core/metrics.py     latência por etapa — instrumentada desde a F1
core/router.py      roteador de intenção: camada 1 regex, camada 2 LLM
core/llm.py         Anthropic (streaming + tool use) / Ollama / nenhum
core/agent.py       loop de agente — porte do engine.ts do Optmus Web
core/voice_loop.py  wake → captura → STT → roteador → LLM → TTS
perception/         microfone, wake word, fim de fala, transcrição
expression/tts.py   ElevenLabs → Piper → voz do sistema, com corte por frase
memory/             4 camadas + perfil vivo + consolidador (ver abaixo)
main.py             FastAPI: lifespan, /health, /metrics, /events, /voz, /memoria
```

**Tudo é evento.** Percepção (voz, gesto, timer, webhook) e ação viram `Event`,
persistido em `events` antes do fanout. O log de eventos é a fonte de verdade e é
o que torna a F5 depurável às 2h da manhã.

Tipos usam namespace com ponto (`voz.wake`, `dispositivo.offline`,
`ferramenta.executada`); assinantes filtram por glob (`voz.*`, `*`).

Cada assinante tem fila própria: consumidor lento não segura o publicador. Fila
cheia descarta o evento **mais antigo** e loga — perder um frame de amplitude de
áudio é aceitável, travar o pipeline de voz não. Eventos de altíssima frequência
publicam com `persist=False` e não sujam o banco.

`EventBus` é abstrato de propósito: quando os workers virarem processos
separados (provavelmente na F5), entra um `RedisEventBus` com a mesma interface e
nenhum chamador muda.

### Esquema do banco

| Tabela | Papel |
|---|---|
| `events` | log de eventos, fonte de verdade |
| `memories` | memória episódica / semântica / procedural |
| `memory_vectors` | embeddings (`vec0`, dimensão de `OPTMUS_EMBEDDING_DIM`) |
| `meta` | dimensão e provedor de embedding, para detectar troca de modelo |
| `devices` | frota: apelido, plataforma, conexão, grupos, status |
| `audit_log` | auditoria **append-only**, garantida por trigger do SQLite |
| `schema_migrations` | controle de versão do esquema |

Duas regras que o esquema já impõe:

- **`audit_log` não aceita UPDATE nem DELETE.** O banco recusa — não é convenção,
  é trigger.
- **Fato novo não sobrescreve fato antigo.** `memories.superseded_by` /
  `superseded_at` versionam a contradição e preservam o histórico.

`OPTMUS_EMBEDDING_DIM` decide a largura da tabela vetorial. Mudar depois exige
reindexar toda a memória — decida antes de popular o banco.

---

## O loop de voz (F1)

```
wake ──▶ captura ──▶ STT ──▶ roteador ─┬─ camada 1 ──▶ TTS        (sem rede)
                                       └─ camada 2 ──▶ LLM ──▶ TTS (streaming)
```

Três decisões carregam a latência:

- **A camada 1 responde sem tocar na rede.** "Que horas são", "para", "cancela"
  morrem em regex, em menos de 1ms. Isso também é o que faz o kill switch
  funcionar com a internet caída — `parar` nunca pode depender de um HTTP.
- **O Optmus fala antes de terminar de pensar.** Os deltas do modelo entram numa
  fila; um consumidor separado corta em frases (`SentenceBuffer`) e sintetiza a
  primeira enquanto o resto ainda está sendo gerado.
- **Thinking adaptativo com `effort: low`, nunca desligado.** Desligar o thinking
  parece o atalho óbvio para latência, mas nos modelos atuais isso faz o modelo
  às vezes escrever a chamada de ferramenta **como texto** — o turno "dá certo",
  a ferramenta nunca roda e ninguém percebe. Num sistema que vai operar celular
  e WhatsApp, essa falha silenciosa é inaceitável.

Testar sem microfone e sem chave de API:

```bash
curl -X POST http://127.0.0.1:8420/voz/texto \
  -H "content-type: application/json" -d '{"texto":"que horas são"}'

curl http://127.0.0.1:8420/metrics
```

`/voz/texto` percorre exatamente o mesmo caminho do microfone a partir do
roteador. Não é atalho de teste: é como o HUD (F4) e o WhatsApp (F6) entram no
assistente sem duplicar pipeline.

### Latência

A meta da seção 3 é **wake word → primeira sílaba em menos de 1,2s**. Cada turno
é cronometrado etapa a etapa e o número aparece em `/metrics`; um turno acima da
meta gera `latencia.acima_da_meta` no log, com o detalhe de qual etapa estourou.

Migre o STT para nuvem **quando o número medido mandar**, não quando a sensação
mandar — `stt.transcricao` loga duração real e fator de tempo real a cada fala.

### Wake word — o passo que não dá para pular

Sem `OPTMUS_WAKE_MODEL_PATH`, o Core sobe com **gatilho manual** (`/voz/gatilho`):
tudo funciona, mas ele não escuta sozinho. Para a escuta contínua, treine um
modelo openWakeWord para "Optmus" com **~500 amostras da sua voz gravadas com o
ruído do seu ambiente real** — ar-condicionado, teclado, TV. Wake word treinado
em áudio limpo de estúdio falha na mesa de verdade.

### Cadeia de voz

ElevenLabs streaming → Piper local → voz do sistema (SAPI no Windows) → texto no
log. Cada elo cai para o próximo sem derrubar o turno: sem nenhum deles, o
Optmus fica mudo, não morto.

---

## A memória (F2)

```
memory/working.py       trabalho    conversa atual, RAM, TTL 30min
memory/episodic.py      episódica   o que aconteceu, quando, com quem
memory/semantic.py      semântica   fatos sobre o usuário e o mundo dele
memory/procedural.py    procedural  rotinas derivadas dos episódios
memory/profile.py       perfil vivo perfil.md, injetado em todo prompt
memory/consolidator.py  o "sono"    digere o dia de madrugada
memory/system.py        fachada     quem usa memória fala com ela
```

**O que entra no prompt a cada turno da camada 2:** o perfil vivo mais as
memórias recuperadas para aquela pergunta, dentro de `<perfil>` e `<memoria>`.
A camada 1 do roteador não paga essa busca — "que horas são" não consulta
memória.

### Decaimento de relevância

```
score = similaridade × recência × frequência × confiança
```

Sem decaimento, uma conversa de seis meses atrás compete de igual para igual com
a de ontem, e o assistente fica burro com o tempo: lembra de tudo e recupera o
irrelevante. A recência cai por meia-vida — episódio em 14 dias, fato semântico
em 180 —, e a frequência cresce em log, para o que já se provou útil ser
favorecido sem dominar toda busca. `GET /memoria/buscar` devolve os quatro
fatores separados: sem isso não dá para saber se um resultado ruim veio de ser
recente ou de ser parecido, e são problemas diferentes.

### Contradição versiona, não sobrescreve

Fato novo que contradiz o antigo marca o antigo como superado com data e o
mantém no banco. Duas razões: auditoria (por que o Optmus achava X em março?) e
reversão (a "correção" pode ter vindo de uma transcrição errada).

### O consolidador noturno

Toda madrugada (`OPTMUS_CONSOLIDATOR_HOUR`, padrão 4h): lê os episódios do dia,
extrai deles os fatos duráveis, detecta padrões que viraram rotina e marca o que
já foi digerido. Sem ele o banco vira lixo em três meses.

Duas fronteiras deliberadas:

- **Não escreve no `perfil.md`.** O perfil entra em todo prompt e só muda por
  ferramenta explícita. Um fato errado na camada semântica estraga uma busca; no
  perfil, contamina todas as conversas seguintes.
- **Não apaga episódio.** Consolidar marca `consolidated_at`; o episódio segue
  no banco, decaindo. Retenção é outra decisão, e é sua.

Sem chave de API, a extração de fatos é pulada e o resto roda igual — detecção
de rotina é heurística pura.

### Rotinas: repetição em semanas distintas

Um padrão só vira rotina com ocorrências em **semanas diferentes**. Três pedidos
na mesma sexta são uma tarefa, não um hábito. Padrão falso vira proatividade
errada, e proatividade errada na F7 é o motivo número um de alguém desligar o
assistente.

### Embeddings: leia isto antes de confiar na memória

| Provedor | Instala com | O que faz |
|---|---|---|
| `hashing` (padrão) | nada | Busca **lexical**: acha palavra, não significado |
| `fastembed` | `pip install -e ".[memoria]"` | Semântica de verdade, local, CPU |

Com o hashing, "quem cuida do meu imposto" **não** encontra "o contador é o
Ricardo". O `/health` reporta isso em `degradacoes` em vez de deixar você
descobrir na hora errada.

Trocar de modelo depois de popular o banco exige `POST /memoria/reindexar` —
vetores de modelos diferentes não são comparáveis, e a busca degradaria em
silêncio. O sistema grava dimensão e provedor na tabela `meta` e recusa fingir
que está tudo bem.

---

## Ferramentas e política de risco (F3)

```
tools/registry.py       contrato Tool + política + auditoria + sandbox
tools/impl/optmus_web.py ponte com o Optmus Web (Notion), com circuit breaker
tools/impl/memory_tools.py lembrar · recordar · perfil_atualizar
tools/impl/system.py    sistema_status
security/policy.py      LEITURA · ESCRITA · EXTERNO · DESTRUTIVO
security/audit.py       trilha append-only, com redação de campo sensível
```

| Risco | Comportamento |
|---|---|
| `LEITURA` | executa direto |
| `ESCRITA` | executa e registra na auditoria |
| `EXTERNO` | **não executa**: retém, o usuário confirma, aí roda |
| `DESTRUTIVO` | confirmação + frase-código + janela cancelável |

Três garantias que os testes travam:

- **O LLM nunca confirma sozinho.** Numa ação `EXTERNO`, o registro devolve ao
  modelo um texto mandando ler a ação em voz alta e esperar — a execução só
  acontece por `POST /seguranca/confirmar`, que é humano.
- **Token de confirmação vale uma vez**, inclusive quando a frase-código está
  errada. Não dá para ficar chutando a frase no mesmo token.
- **Ferramenta nova roda em simulação** nas primeiras `OPTMUS_TOOL_SANDBOX_RUNS`
  execuções — e o resultado diz isso ao modelo, para ele avisar o usuário em vez
  de reportar sucesso falso. Vale só para o que escreve: simular leitura
  devolveria dado falso.

### Busca web

É a **ferramenta server-side da Anthropic** (`web_search`), não um cliente HTTP
daqui: não exige chave de outro fornecedor, já volta com citação, e o resultado
não passa por este processo. O preço é que ela só existe com o cérebro na nuvem
— offline não há busca web, e o sistema não finge que há.

### O contrato com o Optmus Web

Conferido contra a API em produção — não é presumido.

```
POST /api/login   {"password": ...}  →  {"token": ...}
Authorization: Bearer <token>        em todas as demais chamadas

token = HMAC-SHA256(chave='jarvis-auth-v1', mensagem=senha)  em hex
```

O token é **estático**: não varia por requisição, não leva timestamp nem corpo.
O Core o deriva localmente da senha, sem round-trip de login — e cai para o
`POST /api/login` automaticamente se um 401 aparecer, para o dia em que o
esquema mudar não virar um 401 misterioso.

Dois caminhos, com riscos diferentes:

| Ferramenta | Rota | Risco | Quando |
|---|---|---|---|
| `optmus_web` | `GET /api/stats/*`, `/api/work-tasks`, `/api/progress-alerts` | `LEITURA` | Números prontos. Rápido, sem custo de LLM |
| `optmus_web_perguntar` | `POST /api/chat` | `ESCRITA` | Pergunta livre **ou registrar** algo |

`/api/chat` é o próprio agente do Optmus Web, com as ferramentas de Notion dele
— e **pode gravar**. Por isso é `ESCRITA`, não `LEITURA`: rotular de leitura uma
porta que escreve seria mentira de rótulo, e a política de risco depende desse
rótulo estar certo.

Indicadores disponíveis: `financeiro_mensal`, `financeiro_semanal`,
`gastos_por_categoria`, `taxa_de_poupanca`, `previsao_financeira`, `tarefas`,
`trabalho`, `estudos`, `notas_escolares`, `treino_frequencia`, `treino_mensal`,
`sono`, `alertas`.

Resiliência: timeout de 5s, 3 tentativas com backoff (cold start de serverless é
normal, não erro), circuit breaker que para de bater em porta fechada. Se o Web
cair, o Optmus diz que não alcança os dados e segue funcionando.

```bash
curl http://127.0.0.1:8420/ferramentas/optmus-web/diagnostico
```

---

## Configuração

Todas as variáveis estão documentadas em `.env.example`. Obrigatória na F0:
`OPTMUS_SECRET_KEY` (mínimo 32 caracteres) — chave mestra que deriva segredos e
criptografa tokens OAuth em repouso.

As credenciais das fases seguintes são opcionais aqui. Cada subsistema chama
`settings.require(...)` quando sobe, e o `/health` lista o que falta por fase em
`config_pendente`. Você descobre a variável ausente pelo nome, não por um
`NoneType` três camadas abaixo.

---

## Roadmap

| Fase | Entrega | Estado |
|---|---|---|
| **F0** | Esqueleto, SQLite+sqlite-vec, barramento, config, healthcheck | ✅ |
| **F1** | Loop de voz: wake → STT → LLM → TTS, com latência instrumentada | ✅ |
| **F2** | Memória 4 camadas + consolidador noturno | ✅ |
| **F3** | Registro de ferramentas + `optmus_web` + busca web | ✅ |
| F4 | HUD com reactor reativo via WebSocket | — |
| F5 | Device mesh: 1 celular, depois N | — |
| F6 | WhatsApp, Instagram, Home Assistant | — |
| F7 | Proatividade: gatilhos, rotinas, briefing | — |
| F8 | Gestos MediaPipe | — |

---

## Segurança

- `.env`, `*.log`, `*.db` e `data/` estão no `.gitignore` desde o primeiro commit.
- Toda ferramenta declara risco: `LEITURA` · `ESCRITA` · `EXTERNO` · `DESTRUTIVO`.
  `EXTERNO` exige confirmação por voz; `DESTRUTIVO` exige confirmação +
  frase-código + janela cancelável.
- `audit_log` é append-only no nível do banco.

**Dívida crítica herdada — resolver antes da F6.** O Optmus Web hoje é protegido
por senha numérica de 6 dígitos, exposta na internet, dando acesso total ao
Notion pessoal. O Core aumenta essa superfície. Antes de qualquer integração
externa: senha longa e aleatória em cofre, rate limit de login na API do Web e
restrição por IP ou proxy autenticado.

---

## O que este projeto não faz

- Não usa bibliotecas não-oficiais de WhatsApp (Baileys, whatsapp-web.js). O
  número é banido. Cloud API oficial da Meta, sempre.
- Não faz automação de engajamento (follow/like/comentário em massa, contas
  coordenadas). A frota existe para os seus aparelhos, seus apps, mídia, testes e
  publicação na sua própria conta via API oficial.
- Não copia identidade visual protegida. A paleta e a logo do Optmus são a
  identidade — branco, preto, azul marinho, âmbar reservado para execução ativa e
  alerta.
- Não deixa o LLM decidir sozinho ação irreversível. Sempre pelo motor de política.