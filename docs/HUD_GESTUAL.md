# F8 — HUD gestual e abrir coisas no PC

Duas metades com riscos bem diferentes: a **camada de execução** (Core, abre
arquivo e app de verdade) e a **camada de interação** (frontend, rastreia a mão).
Esta doc cobre as duas, e a fronteira entre elas.

## A regra da câmera — e como ela foi preservada

A Fase 6 estabeleceu, em texto: *"nunca fica ligada em segundo plano
ambientalmente"*. Rastrear gesto exige vídeo contínuo, o que colidiria
frontalmente com isso.

**Decidido em 26/08/2026:** modo gesto é **ativação explícita**, exatamente como
`olhar`. Você liga; a câmera acende com indicador visível; e ela se apaga sozinha
após 20 s sem nenhuma mão vista. A regra da Fase 6 continua valendo — não foi
revogada, foi reconciliada.

Três propriedades sustentam isso, e cada uma tem teste provado por injeção:

- **Nunca liga sozinha.** Só `ligar()` abre a câmera.
- **Sempre desliga.** Inatividade, erro de permissão, modelo ausente, GPU sem
  suporte — todos passam por `desligar()`.
- **Os trilhos param de verdade** (`track.stop()`). Soltar só a referência deixa
  o LED aceso até o coletor de lixo passar — e o LED é como você sabe se está
  sendo filmado.

## No Windows, "abrir" é "executar"

`os.startfile` é `ShellExecute`. O `PATHEXT` tem 13 extensões que o Windows roda
direto, e além delas um `.lnk` executa o que apontar, um `.docm` roda macro.

O portão de confirmação **não cobre isso sozinho**, e o motivo é sutil: a frase
que você lê diria *"abrir relatorio.docx"* — e você não tem como distinguir isso
de um `.docm` malicioso. Autorizar o que não dá para avaliar não é autorizar.

Por isso existe a lista, **além** do portão.

### O contrato: id, nunca caminho

| Ferramenta | Risco | Recebe | Devolve |
|---|---|---|---|
| `pc_listar` | LEITURA (sem portão) | `pasta_id` opcional | itens com `id`, `nome`, `tipo` |
| `pc_abrir` | EXTERNO (com portão) | `alvo_id` | confirmação pendente |

Caminho **não cruza essa fronteira em nenhuma direção**. Uma instrução injetada
que diga "abra `C:\Windows\System32\cmd.exe`" não tem onde escrever.

E a segurança não depende do id ser secreto: o id é um hash determinístico e o
algoritmo está no código. Calcular o id de um `.exe` dentro da pasta registrada
não abre nada, porque `resolver` **relista** o que é permitido agora em vez de
consultar um mapa — e executável nunca entra nessa lista.

### `data/alvos.json`

Fora do controle de versão — são caminhos da sua máquina.

```json
{
  "apps":   { "vscode":   { "nome": "VS Code",  "caminho": "C:/.../Code.exe" } },
  "pastas": { "projetos": { "nome": "Projetos", "caminho": "C:/Users/.../Documentos" } }
}
```

**Apps** são executáveis que você escolheu, um por um. **Pastas** liberam os
arquivos comuns dentro delas — e só eles: `.exe`, `.bat`, `.lnk`, `.docm`, `.ps1`
e o resto do `PATHEXT` continuam barrados mesmo lá dentro. Não desce em
subpastas: profundidade transformaria "uma pasta registrada" em "tudo abaixo
dela", que é um registro que você não fez conscientemente.

Teto de 40 itens por pasta — dez mil arquivos viraria um prompt gigante e uma
tela impossível de apontar com a mão.

## Os freios do gesto

Testados sem câmera, sem WASM e sem GPU: a máquina de estados é pura, e o tempo
entra como parâmetro.

| Freio | O que impede |
|---|---|
| **Permanência** (600 ms) | Atravessar a tela com a mão abriria tudo que passasse por baixo |
| **Margem de desambiguação** (40%) | Dois alvos próximos → **nenhum** é escolhido. Confirmar o alvo errado é pior que não pedir nada |
| **Rearme** | Mão fechada esquecida sobre um ícone abriria o arquivo 30× por segundo |
| **Reinício ao destravar** | Bombear a mão dispararia sem nova permanência |
| **Histerese na pinça** | Ruído de um pixel destravaria e redispararia sozinho |
| **Troca de alvo zera a contagem** | Escorregar para o vizinho dispararia o vizinho com o anel quase cheio — falso positivo que *parece* intencional |
| **Uma mão só** | Duas convidam ambiguidade sobre qual está mirando |

## Instalar

```
npm run gestos:assets
```

Copia o WASM do pacote e baixa o modelo (7,8 MB) para `public/mediapipe/`.

**O modelo não vem no pacote npm** — medido: `@mediapipe/tasks-vision` publica
`wasm/` e nenhum `.task`. Os dois ficam servidos localmente de propósito: gesto
que depende de CDN deixa de existir quando a rede cai.

No Core:

```
OPTMUS_PC_ENABLED=true
OPTMUS_PC_TARGETS_PATH=data/alvos.json
```

## O fluxo completo

1. Você ativa o modo gesto → câmera acende com indicador.
2. O HUD desenha os alvos vindos de `GET /pc/alvos`.
3. A mão mira; o anel enche em 600 ms; a pinça fecha.
4. `POST /pc/abrir` **não abre** — cria a pendência e devolve o token.
5. O portão dourado aparece: *"abrir o aplicativo VS Code no seu computador"*.
6. Você autoriza com a prova HMAC do dispositivo (Achado Sério 2).
7. O Core abre, e o resultado volta ao agente como turno novo (Achado Sério 1),
   com `falar=false` porque a origem é o HUD.

O gesto é **ação humana, não do modelo** — por isso não passa pelo `/chat`.
Roteá-lo pelo modelo o obrigaria a adivinhar qual id a mão apontou. Mas ser
humano não dispensa o portão: quem abre continua sendo
`POST /seguranca/confirmar`.

Timeout do portão: os mesmos 120 s do resto do sistema. Um número só para
raciocinar, e o cartão diz explicitamente o que vai abrir.

## Auditoria

`decision = confirmado`, igual a WhatsApp e câmera. **Nenhuma nuance nova** — um
quinto valor quebraria o CHECK do esquema e apagaria a distinção que já existe
entre `negado` (política barrou) e `cancelado` (humano recusou).

O metadado guarda **nome e tipo, nunca o caminho**: a trilha é permanente e não
precisa registrar a topologia do seu disco.

## Sintomas

| O que você vê | Causa |
|---|---|
| `Modelo de mão ausente` | Falta rodar `npm run gestos:assets` |
| A câmera apaga sozinha | Esperado: 20 s sem mão detectada |
| `dois alvos muito próximos` | O freio de ambiguidade agindo. Aponte com mais precisão |
| `abra a mão antes do próximo` | Rearme: solte o gesto antes de repetir |
| As ferramentas `pc_*` não aparecem | `OPTMUS_PC_ENABLED=false`, registro vazio, ou plataforma hospedada |
| `plataforma hospedada` | Esperado e correto: abrir arquivo só faz sentido local |

## Pendência aberta — as duas câmeras nunca rodaram juntas

**Aberta em 26/08/2026. Não bloqueia nada; só não pode ser tratada como
resolvida.**

Medi que a webcam **não é exclusiva** nesta máquina: duas capturas DirectShow
abriram e leram ao mesmo tempo. Mas o par que medi foi **cv2 + cv2**, e o
cenário real é outro:

> o navegador segurando a câmera para o MediaPipe **enquanto** o Core tenta
> abrir a mesma câmera via cv2 para o `OlharTool` (F4.2).

São stacks diferentes — Chrome usa MediaFoundation, o Core usa DirectShow — e
nada do que medi diz o que acontece quando os dois disputam. Se algum dia o modo
gesto estiver ligado e você pedir "olha o que está na minha frente", é neste par
que pode quebrar.

Sintoma esperado, se quebrar: `CameraIndisponivelError` no Core com a câmera
aparentemente livre, ou o vídeo do gesto congelando quando o `olhar` dispara.
Nesse caso, a saída provável é serializar — desligar o modo gesto enquanto o
`olhar` executa — mas isso é desenho para quando houver medição, não agora.

## O que ainda falta em F8

Entregue: camada de execução (Core, com todos os freios) e camada de gesto
(máquina de estados + ciclo de vida da câmera).

Falta: os painéis manipuláveis com animações Ultron, a voz contextual por painel,
e a sub-fase do celular (desbloqueio por voz, ligações, WhatsApp) — que é projeto
próprio e ainda não começou.
