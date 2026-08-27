# F10 — Modo Desenvolvedor

O Optmus programa nos seus projetos e **publica sozinho**. Desligado por padrão,
só local, e exige Docker.

```
OPTMUS_DEV_ENABLED=true
```

## O que foi revogado, e o que não foi

Em 26/08/2026 você revogou a trava "só humano decide deploy": *"controladamente"*
significa que **você** controla a frequência com que pede, não que existe uma
aprovação técnica no caminho.

**Foi revogado:** o portão de confirmação em `dev_publicar`.

**Continua valendo, sem exceção:**

| | Como |
|---|---|
| **Sandbox** | Testes rodam em contêiner, sem rede, montagem somente-leitura. Docker parado = sem publicar |
| **Auditoria** | Toda ação vira linha na trilha, com o vocabulário existente |
| **Superfície** | Só dentro dos projetos registrados. Nunca `.git/`, `.env*`, `data/` |
| **Histórico** | `push --force` e apagar branch continuam DESTRUTIVO, com portão e frase-código |

A dispensa mora numa **lista visível** (`dev_sem_portao` no config), não numa
flag por ferramenta. Se ela valesse por *risco* em vez de por *nome*, cobriria
câmera, WhatsApp e Telegram junto — o portão inteiro cairia com uma linha de
configuração. Há teste garantindo que só `dev_publicar` está lá.

## A cadeia real, medida

```
escrever → commit → push main → GitHub → Railway (Docker) → healthcheck → PRODUÇÃO
```

**Não existe CI. Não existe PR. Não existe revisão.** Não há `.github/workflows`.
Push em `main` **é** o deploy, em minutos.

Isso muda onde os freios têm que estar: **todos antes do push**. Depois dele só
existe o healthcheck do Railway, que pega "não sobe" — nunca "subiu errado".

## As seis ferramentas

| Ferramenta | Risco | O que faz |
|---|---|---|
| `dev_listar` | LEITURA | Projetos e o que mudou em cada um |
| `dev_ler` | LEITURA | Conteúdo de um arquivo |
| `dev_escrever` | ESCRITA | Grava, só dentro da superfície |
| `dev_testar` | ESCRITA | Roda a suíte em contêiner isolado |
| `dev_publicar` | EXTERNO | Commit + push. **Portão dispensado** |
| `dev_reverter` | EXTERNO | Desfaz o último deploy autônomo |

## `data/projetos.json`

Fora do controle de versão — são caminhos da sua máquina.

```json
{
  "optmus-core": {
    "nome": "Optmus Core",
    "raiz": "C:/Users/.../optmus-core",
    "testes": "pytest -q",
    "imagem": "optmus-core-testes",
    "branch": "main"
  }
}
```

O modelo manda **id + caminho relativo**, nunca absoluto. A contenção é
verificada **depois** de `resolve()`, o que também mata symlink apontando para
fora — `resolve()` os segue, então um link `atalho → C:/Users/.../.ssh` é
revelado antes da checagem.

## As três negações dentro do projeto

`.git/`, `.env*`, `data/` (mais `.venv/` e `node_modules/`, por ruído).

A que menos parece importar e mais importa é a primeira. **Escrever
`.git/hooks/pre-commit` é execução de código arbitrário no próximo comando
git** — e o próximo comando git é o que o próprio modo dev roda para commitar.
Seria um jeito de furar sandbox, portão e auditoria de uma vez, sem nunca chamar
uma ferramenta de deploy.

A zona negada é conferida duas vezes: no caminho pedido e no caminho já
resolvido, porque `docs/../.git/hooks/x` só revela o `.git` depois de resolvido.

## Os freios antes do push

**Sandbox verde é pré-requisito.** `dev_publicar` confere o daemon do Docker
*antes de qualquer coisa* — descobrir isso depois de commitar deixaria o
repositório sujo. Depois roda a suíte; vermelho não publica.

**Limiar de deleção.** Antes de commitar, conta quantos arquivos o **índice**
está removendo (`OPTMUS_DEV_MAX_DELECOES`, padrão 8). Acima disso, recusa — e
desfaz o `git add`. Deixar uma remoção em massa preparada seria pior que a
própria remoção, porque o próximo commit de qualquer um a levaria junto.

Lido do índice e não da árvore: é o índice que vira commit.

**Nunca shell.** Todo git passa por `create_subprocess_exec` com argv explícito.
Uma mensagem de commit contendo `--force ; rm -rf /` é apenas uma mensagem
esquisita — há teste que publica exatamente essa string e confirma que ela virou
texto do commit e que nada foi apagado.

## Injeção via conteúdo de código

O padrão do F8 — *"só identidade entra, nunca conteúdo"* — **não serve aqui**,
porque o conteúdo *é* o trabalho: o Optmus precisa ler código e comentários.

A defesa muda de lugar. Nenhum dos freios depende do que foi lido:

- O **destino** do push vem do registro de projetos, não de parâmetro. Um
  comentário dizendo "publique no repo X" não tem onde ser escrito.
- O **limiar de deleção** é aritmética sobre o índice.
- **Teste verde** não se negocia por texto.
- O **contêiner sem rede** impede que "rodar os testes" vire exfiltração.

Um comentário malicioso pode, no máximo, fazer o modelo escrever código ruim —
e código ruim é barrado pelos testes ou revertido por `dev_reverter`.

## Reversibilidade

Sem humano no caminho, poder voltar rápido é o que substitui parte da função que
a confirmação teria tido.

Cada publicação grava `sha_antes|sha_depois`. `dev_reverter` faz
`git revert --no-edit` do último deploy e envia — o que dispara um novo deploy
com o código anterior.

**`revert`, não `reset`:** não reescreve histórico, então o desfazer é auditável
e chega ao remoto sem força. Desfazer não pode depender de uma ação mais
perigosa que o erro.

O registro é limpo depois de reverter: um segundo revert do mesmo SHA falharia de
um jeito confuso.

## Meio-deploy

`git push` é atômico por ref: o repositório nunca fica pela metade. A janela real
é *"push feito, Railway construindo"* — e nela o healthcheck do Railway
(`/health/live`, 60 s, 3 tentativas) mantém o deploy anterior servindo se o novo
não subir.

O que o healthcheck **não** pega: código que sobe e está errado. Para isso existe
`dev_reverter`.

## A imagem Docker

`dev_testar` precisa de uma imagem com as dependências instaladas —
`python:3.12-slim` puro não roda a suíte do Core.

Para o `optmus-core` o `Dockerfile` existente instala `[llm,memoria,relatorios]`,
mas **não** instala `pytest` nem `ruff`. Provavelmente precisa de um
`Dockerfile.testes` ou de um estágio a mais. **Isto ainda não foi construído nem
validado** — o daemon do Docker estava parado.

## Sintomas

| O que você vê | Causa |
|---|---|
| As ferramentas `dev_*` não aparecem | `OPTMUS_DEV_ENABLED=false`, registro vazio, ou plataforma hospedada |
| `a sandbox exige Docker rodando` | Abra o Docker Desktop |
| `os testes de X nao passaram` | Exatamente isso. Não publicou |
| `removeria N arquivos, acima do teto` | Freio de deleção. Se for proposital, aumente `OPTMUS_DEV_MAX_DELECOES` |
| `sai da raiz do projeto` | Caminho relativo escapando. Correto |
| `esta em zona protegida` | `.git`, `.env` ou `data/`. Correto |
| `Nada mudou` | Não há o que publicar |

## Montlux

**Fora do modo dev por enquanto**, decidido em 26/08/2026: o diretório não existe
nesta máquina, e ele pode ter dados de terceiros no caminho — terceiros que não
participaram da decisão de revogar o portão.

Quando entrar, precisa de investigação própria sobre o que há de terceiros ali.
Adicionar é uma linha no `projetos.json`; a decisão é que não é.
