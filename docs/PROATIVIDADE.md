# F7 — quando o Optmus fala sem ser chamado

Esta é a única função do sistema que **interrompe** alguém. Todas as outras
respondem a um pedido; esta decide sozinha que vale a pena falar. Por isso tem
mais freios que qualquer outra parte do Core, e por isso **vem desligada**.

```
OPTMUS_PROACTIVE_ENABLED=true
```

## Como funciona

Um agendador acorda a cada `OPTMUS_PROACTIVE_INTERVAL_MIN` minutos (padrão 30) e
roda um ciclo:

1. Está ligada? Está fora da janela de silêncio? Sobrou orçamento hoje?
2. Coleta gatilhos das fontes de **dado real**.
3. Descarta o que já foi avisado recentemente.
4. Pega **um** — o mais urgente.
5. O modelo escreve a frase, a partir só dos fatos.
6. Sai pelos canais. Audita, gasta orçamento, marca como avisado.

## As seis regras, e o motivo de cada uma

**1. Quem decide SE avisa é o código; quem decide COMO é o modelo.** O gatilho
sai de dado real por uma regra determinística. Só a frase é escrita pelo modelo,
para soar como o Optmus. O contrário — um modelo decidindo quando te interromper
— não dá para orçar, prever nem testar.

**2. O compositor não tem ferramenta nenhuma.** Um aviso proativo não tem humano
esperando para confirmar, então o portão de `EXTERNO` não se aplica. Um caminho
sem portão **jamais** pode alcançar terceiros — e sem ferramentas o WhatsApp não
está ao alcance nem por acidente. É `tools=None` no construtor, não um pedido no
prompt: prompt é sugestão, estrutura é estrutura.

**3. Um aviso por ciclo.** Três coisas vencendo hoje viram um aviso, não três.
Rajada é o que faz alguém silenciar o assistente para sempre.

**4. Orçamento diário rígido.** `OPTMUS_PROACTIVE_DAILY_BUDGET` (padrão 5),
contado por dia civil e gravado no banco — a chave inclui a data, então o "reset"
não depende de ninguém lembrar de zerar nada. Ao chegar a zero os avisos param;
não entram em fila para amanhã, porque fila de aviso vira enxurrada de manhã.

**5. A janela de silêncio descarta, não adia.** Padrão 22h–08h. E descartar não
perde nada: a fonte é relida a cada ciclo, então o prazo que ainda importar às 8h
aparece sozinho às 8h. O que sumir no meio da noite era o que deixou de importar.

**6. Nunca inventa.** Sem gatilho, sem aviso — e o modelo nem chega a ser
chamado. Ele recebe os fatos e a instrução de não acrescentar nada; se concluir
que não justifica interromper, responde `SEM AVISO` e nada sai.

## De onde vêm os gatilhos

| Fonte | O que observa | Precisa de |
|---|---|---|
| `PrazosDoNotion` | Prazos vencendo em ≤ 3 dias, ou já vencidos | Mapa do Notion configurado |
| `RotinasDaMemoria` | Hábitos que o consolidador detectou e que batem com este horário | Memória procedural populada |

Ambas devolvem **fato observado**, nunca opinião. *"prova de biologia em 16/09,
vence em 2 dias"* é fato; *"você deveria estudar"* é o modelo escrevendo a partir
dele.

Trinta dias de antecedência não é aviso, é ansiedade — daí o corte em 3 dias.
Prazo já vencido continua avisando: o que passou é o que mais importa saber.

Fonte quebrada não derruba o ciclo. O Notion fora do ar não pode calar uma
rotina detectada localmente, senão a proatividade inteira depende do serviço mais
frágil.

## Por onde sai

| Canal | Quando |
|---|---|
| `CanalBarramento` | Sempre — é o único que funciona sem nenhuma integração configurada |
| `CanalTelegram` | Se `OPTMUS_TELEGRAM_*` estiver configurado |

Tenta os dois e considera entregue se **algum** entregou: quem está no celular
não vê o navegador. Se nenhum entregar, **não gasta orçamento** — cobrar por um
aviso que não chegou faria uma rede instável silenciar o dia inteiro.

O `CanalTelegram` não passa pela ferramenta `telegram_enviar` (aquela é `EXTERNO`
e exige confirmação, que aqui não existe). Usa o cliente direto — e o que o mantém
seguro é o mesmo que já mantinha a ferramenta: **o destino mora na configuração** e
não pode ser escolhido nem pelo modelo, nem por texto que ele leu.

## Repetição

Cada gatilho tem uma impressão digital. O mesmo assunto não repete dentro de
`OPTMUS_PROACTIVE_COOLDOWN_H` (padrão 12 h).

A chave de um prazo usa a **data**, não os dias restantes. "Faltam 2" vira "falta
1" amanhã; se a chave carregasse os dias, o mesmo prazo viraria assunto novo toda
madrugada e seria avisado todo dia até vencer — a deduplicação existiria no papel
e não seguraria nada.

## Auditoria

Todo aviso entregue vira linha na trilha, com `tool = aviso_proativo` e
`decision = permitido`. Não há confirmação humana aqui de propósito: **o orçamento
diário é o que substitui o portão.**

## Sintomas

| O que você vê | Causa |
|---|---|
| Nunca avisa nada | `OPTMUS_PROACTIVE_ENABLED=false`, ou nenhuma fonte com dado |
| `nada a dizer` no status | Funcionando: não havia prazo perto nem rotina no horário |
| `janela de silencio` | Entre 22h e 8h. Volta sozinho de manhã |
| `orcamento do dia esgotado` | Já falou 5 vezes hoje. Vira à meia-noite |
| `tudo ja avisado recentemente` | Os gatilhos existem, mas saíram nas últimas 12 h |
| `nenhum canal entregou` | Telegram fora e barramento sem ninguém ouvindo |
