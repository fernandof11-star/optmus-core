# Acesso direto ao Notion e conferência de números

O objetivo desta etapa é **uma pergunta só**: os números calculados aqui batem
com os que o Optmus Web já entrega? Enquanto a resposta não for "sim em tudo", as
duas fontes convivem e nada é desligado.

Por isso as agregações locais **não estão registradas como ferramenta do
agente**. Duas fontes de verdade expostas ao modelo é o jeito mais rápido de o
assistente escolher a errada — que é exatamente o que a spec manda evitar.

---

## 1. Criar a integração no Notion

1. <https://www.notion.so/my-integrations> → **New integration** → tipo
   **Internal**, capacidade **Read content** (não precisa de escrita nesta fase).
2. Copie o *Internal Integration Secret* para `OPTMUS_NOTION_TOKEN` no `.env`.
3. **Compartilhe cada base com a integração.** Criar o token não dá acesso a
   nada: abra cada base → `···` → **Conexões** → adicione a integração.

> O erro mais comum aqui é parar no passo 2. O sintoma é `404` com "não
> compartilhado com a integração" — o Core diz isso com todas as letras.

---

## 2. Descobrir o schema

```bash
curl http://127.0.0.1:8420/notion/descobrir | jq
```

Devolve:

- **`bases`** — toda base visível, com nome e tipo de cada propriedade. Isto é
  realidade, lida do Notion.
- **`rascunho_do_mapa`** — um palpite de qual base cumpre qual papel e qual
  coluna é a data, o valor, o tipo.

O rascunho é **rascunho**. Os nomes de propriedade são reais, mas a associação
base→papel é heurística pelo título ("financ", "trabalho", "estudo"). Revise
antes de confiar — especialmente **qual coluna é o valor no financeiro**: apontar
para a coluna errada produz um total plausível e errado, que é o pior resultado
possível numa conferência.

---

## 3. Escrever o mapa

Salve em `data/notion_map.json` (remova as chaves `_propriedades_disponiveis`,
que existem só para você consultar durante a revisão):

```json
{
  "financeiro": {
    "database_id": "1a2b3c...",
    "data": "Data",
    "valor": "Valor",
    "tipo": "Tipo",
    "valores_receita": ["Receita", "Entrada"],
    "valores_despesa": ["Despesa", "Saída"]
  },
  "trabalho": {
    "database_id": "4d5e6f...",
    "tipo": "Tipo",
    "status": "Status",
    "valores_concluido": ["Concluído", "Done"]
  },
  "prazos": [
    {
      "database_id": "7g8h9i...",
      "titulo": "Nome",
      "data": "Data",
      "detalhe": "Matéria",
      "rotulo": "estudo"
    }
  ]
}
```

`prazos` é uma lista: o `/api/progress-alerts` do Web parece juntar mais de uma
origem, e o `rotulo` é o que aparece no campo `tipo` do alerta.

Confira uma agregação isolada antes de comparar tudo:

```bash
curl "http://127.0.0.1:8420/notion/stats/financeiro_mensal" | jq
```

---

## 4. Conferir

```bash
curl http://127.0.0.1:8420/notion/conferir | jq
```

```json
{
  "equivalente": false,
  "veredito": "2 divergencia(s) - o Optmus Web ainda nao pode ser desligado",
  "indicadores": [
    {
      "indicador": "financeiro_mensal",
      "equivalente": false,
      "comparados": 6,
      "divergencias": [
        {"chave": "2026-07", "campo": "expense", "web": 790.0, "notion": 812.5}
      ],
      "so_no_web": [],
      "so_no_notion": []
    }
  ],
  "avisos": [
    {"campo": "financeiro.tipo", "detalhe": "linhas com tipo fora de valores_receita/valores_despesa", "linhas": 3}
  ]
}
```

Como ler:

- **`divergencias`** mostra os dois valores lado a lado. O relatório não tenta
  decidir quem está certo — quem decide é você, olhando a linha no Notion. Um
  relatório que "resolve" a diferença sozinho esconde a única informação que
  importa.
- **`avisos`** costuma explicar a divergência antes de você investigar: linhas
  sem data, sem valor, ou com um tipo que não está em
  `valores_receita`/`valores_despesa`.
- **`so_no_web` / `so_no_notion`** são registros que existem de um lado só —
  normalmente janela de tempo diferente ou filtro de arquivados.
- **Uma única divergência já reprova.** Não existe "quase igual" quando o passo
  seguinte é apagar a outra fonte.
- **`comparados` é tão importante quanto `equivalente`.** Um indicador que
  comparou 6 meses tem peso; um que comparou 1 linha, quase nenhum. Olhe esse
  número antes de comemorar.
- **`sem_dados: true` reprova.** Se os dois lados vierem vazios, não há
  divergência — e também não há prova de nada. Duas listas vazias batendo não é
  evidência de equivalência, é ausência de evidência.

### A cobertura importa mais do que o "100%"

Uma conferência que passa com dados escassos testa pouco. Vale olhar quantos
registros de fato entraram na comparação, e se as linhas exercitaram os
caminhos ambíguos — os marcados **PRESUMIDO** no código. Meses todos zerados
batem trivialmente; um único tipo de tarefa não testa o agrupamento; nenhum
lançamento sem `tipo` deixa a regra do sinal sem exercício.

### Onde as diferenças costumam estar

| Sintoma | Causa provável |
|---|---|
| Um mês a mais ou a menos | `OPTMUS_NOTION_MONTHS_WINDOW` ≠ janela do Web |
| Despesa maior aqui | O Web filtra algo (arquivado, categoria) que o Core soma |
| Aviso em `financeiro.tipo` | Há valores no select além de Receita/Despesa — acrescente-os ao mapa |
| Diferença de centavos | Ordem de arredondamento diferente. É reportada de propósito |
| `diasRestantes` diferente | Fuso: o Core usa UTC; confira se o Web usa horário local |

### Regras confirmadas (2026-08-12)

A primeira conferência com dados variados encontrou duas divergências, e as duas
eram erro do Core. Ficam registradas porque são exatamente o tipo de regra que
não dá para inferir sem comparar:

| Regra | Como era | Como é |
|---|---|---|
| Sinal do valor | `abs()` no valor | **Soma com sinal.** Um `Saída` de −200 é estorno e precisa *reduzir* a despesa. Com `abs()`, virava gasto novo de 200 — erro de 400 no total |
| Linha sem `Tipo` | Classificada pelo sinal | **Ignorada**, como o Web faz — mas vira aviso `financeiro.tipo_vazio`. Sumir em silêncio de um total financeiro é inaceitável |

Ambas estão travadas por teste de regressão com os dados reais de agosto/2026.

O que segue **PRESUMIDO** em `integrations/notion_stats.py`: janela de meses,
tratamento de tarefa arquivada, o fuso do cálculo de `diasRestantes` e — o mais
provável de divergir — as **duas pontas da janela de alertas**.

### A janela de alertas: medida e fechada

Busca binária movendo a data de um único item, comparando com o Web a cada passo:

| Dias | No Web? | Conclusão |
|---|---|---|
| +41 | não | Frente < 41 |
| **+30** | **sim** | Frente inclui 30 |
| −2, −27, −28, **−29** | **sim** | Trás inclui até 29 |
| **−30** | **não** | Trás exclui exatamente 30 |
| −60 | não | — |

**A janela do Web é ±30 dias, calculada em timestamp.** É o que explica a
assimetria observada: a meia-noite de 30 dias atrás já é anterior a
`agora − 30d` (fica de fora), enquanto a de daqui a 30 dias ainda não passou de
`agora + 30d` (fica dentro).

Como o Core compara **data**, e não timestamp, reproduzir isso exige a borda da
frente **inclusiva** e a de trás **exclusiva** (`on_or_before` + `after`). Assim
`OPTMUS_NOTION_ALERT_PAST_DAYS=30` significa 30 de verdade, sem ninguém precisar
lembrar de escrever 29.

```
OPTMUS_NOTION_ALERT_WINDOW_DAYS=30
OPTMUS_NOTION_ALERT_PAST_DAYS=30
```

As quatro bordas (+30, +31, −29, −30) estão travadas por teste.

### Como testar sem errar a data

**Não calcule a data de cabeça.** Um dia de erro joga a linha para fora da
janela, e o resultado fica **idêntico** ao de um item muito distante — parece
confirmação e não é. Foi o que aconteceu na primeira tentativa: o item ficou em
`2026-09-12`, um dia depois do limite `2026-09-11`, e três testes seguidos
devolveram o mesmo relatório.

```bash
curl "http://127.0.0.1:8420/notion/alertas/diagnostico?dias=-30" | jq
```

```json
"data_para_testar": {"dias": -30, "data": "2026-07-13", "entraria_no_core": true}
```

Cole essa data literal no item, rode `/notion/conferir`, repita. O sinal de que
o teste discriminou é `comparados` mudar, ou uma lista `so_no_*` deixar de ser
vazia — se continuar tudo igual, o item caiu fora dos dois lados de novo.

---

## Regras do Web descobertas por medição

Nenhuma destas está documentada em lugar nenhum — todas saíram de comparar
número com número:

| Regra | O que se descobriu |
|---|---|
| Sinal do valor | Soma **com sinal**. Um `Saída` de −200 é estorno e reduz a despesa |
| `Tipo` vazio | Linha **ignorada** — não entra em receita nem em despesa |
| Janela de alertas | **±30 dias**, calculada em timestamp: frente inclusiva, trás exclusiva |
| Baldes semanais | **Janela rolante de 7 dias** a partir de hoje — não semana civil |
| Média histórica | Últimos **3 meses** anteriores, contando os zerados no divisor |
| Taxa de poupança | `null` quando não há receita — não é 0% |
| Categoria vazia | Cai em `"Outros"` |
| `monthLabel` | `Ago/2026` com ano de 4 dígitos, diferente do `Ago/26` do mensal |
| Alertas e status | Item **concluído não gera alerta** — o painel avisa do que falta |
| Tarefas concluídas | **Total histórico**, não do mês. O próprio PDF do Web diz não conseguir recortar por mês |
| Prioridade e atraso | `porPrioridade` veio `[]` com 4 tarefas priorizadas — só **pendente** entra nesses blocos |
| Provas do mês | Recorte por **mês civil**, e o concluído **aparece** — ao contrário dos alertas |
| Estudo sem `Tipo` | Sai como **"Outro"** no PDF, não em branco |
| Taxa no PDF | Arredondada para **inteiro**: 76,62% vira `77%` |
| Treino e status | Conta só o **`Concluído`**. Um treino com data *passada* e `Planejado` segue zerado no Web |
| Fonte das notas | É a base **`Notas por trimestre`**, não a coluna `Nota` de `Estudos` |
| Data do sono | Vem do **título** `Noite` (`DD/MM`), com o ano corrente — a base não tem coluna de data |
| Sono sem noite | Linha com horas e sem noite **não entra** em nada: nem média, nem `porQualidade` |

Cada uma está travada por teste de regressão com os dados reais que a expuseram.

## 5. Só então decidir

Quando `equivalente: true` em execuções seguidas e em dias diferentes (a
igualdade de um dia só pode ser coincidência de dados parados), aí a conversa
sobre desligar o `jarvis-pessoal` passa a ser sobre o resto do que ele faz:

- o PWA de chat que você usa no celular;
- o PWA de chat que você usa no celular;
- o que mais o painel faz além dos `/api/stats/*`.

Os 13 indicadores estatísticos **e o relatório mensal em PDF** estão replicados
e conferidos.

### Estado por indicador

| Indicador | Situação |
|---|---|
| `financeiro_mensal` | ✅ replicado e conferido |
| `financeiro_semanal` | ✅ replicado e conferido |
| `gastos_por_categoria` | ✅ replicado e conferido |
| `taxa_de_poupanca` | ✅ replicado e conferido |
| `previsao_financeira` | ✅ replicado e conferido |
| `trabalho` | ✅ replicado e conferido |
| `alertas` | ✅ replicado e conferido |
| `estudos` | ✅ replicado e conferido |
| `treino_frequencia` | ✅ replicado e conferido |
| `treino_mensal` | ✅ replicado e conferido |
| `notas_escolares` | ✅ replicado e conferido |
| `sono` | ✅ replicado e conferido |
| `tarefas` | ✅ replicado e conferido |

### Relatório mensal (PDF)

`GET /relatorios/mensal` substitui `/api/reports/monthly`. `GET
/relatorios/mensal/dados` devolve os mesmos números em JSON — é por ali que se
confere o relatório sem extrair texto de PDF.

Conferência feita comparando o **texto** dos dois PDFs, não os bytes: fonte,
kerning e hora de geração mudam sempre. Os 66 valores numéricos do PDF do Web
aparecem todos no do Core, sem sobra dos dois lados.

Duas seções do relatório **não** saem dos indicadores já conferidos, e por isso
exigiram medição própria: `TAREFAS` (é `/api/stats/tasks`) e `PROVAS E
TRABALHOS DO MÊS` (recorte por mês sobre `Estudos`, não é o alerta).

Três diferenças deliberadas em relação ao PDF do Web:

| | Web | Core | Por quê |
|---|---|---|---|
| Nota do bloco TAREFAS | "o Notion não guarda a data em que cada tarefa foi concluída" | "o Optmus não restringe esse número ao mês" | A base **tem** `Data de conclusão`; a justificativa do Web é falsa. O número é o mesmo |
| Hora de geração | UTC (`02:15` quando eram `23:15` aqui) | Hora local | Carimbo de cabeçalho, não dado conferido |
| Página de avisos | não tem | tem | Linhas do Notion que ficaram fora dos totais — 6 hoje, todas invisíveis no Web |

**Tempo de geração: ~5 s**, com as 8 consultas em paralelo. O piso é do Notion,
não do código: uma consulta isolada leva 1,5 s, e 8 em paralelo saem em 4,2 s
num degrau de ~350 ms cada — o limite de 3 req/s da API. Encadeadas davam 11 s,
acima do limite de execução de função serverless.

### O que ainda é presunção, não medição

Bate hoje, mas por um caminho que os dados atuais não separam. Cada item traz o
lançamento que decidiria a questão.

| Ponto | Por que está em aberto | O que resolveria |
|---|---|---|
| Ano do sono | O título só tem `DD/MM`; assumo o ano corrente | Uma noite de dezembro lida em janeiro |
| Corte de `ultimasNoites` | Devolvo todas; com 2 noites não dá para ver limite | Mais de ~7 noites lançadas |
| `situacao` de disciplina | Só vi `"em andamento"`; `aprovado`/`reprovado` são leitura minha | Uma disciplina que feche acima de 60 pontos |
| Noite fora do formato | Confirmei só noite **vazia**; título tipo `"ontem"` nunca apareceu | Uma linha com horas e noite em texto livre |
| Virada de mês | Nunca rodei a conferência num dia 1º | Rodar em 1º de setembro |

**A base `Estudos` não é a fonte de `notas_escolares`.** Ela tem uma coluna
`Nota` e a tentação de usá-la é óbvia — mas com duas linhas preenchidas (nota
5,0 com `Tipo="Prova"` e status `Concluído`; nota 3,64 sem tipo e `Em
andamento`) o Web devolveu lista vazia nas duas vezes. Tipo, status e presença
de nota foram variados sem mudar nada. A disciplina só apareceu do lado do Web
depois que `Notas por trimestre` foi compartilhada — que é a fonte de verdade.
