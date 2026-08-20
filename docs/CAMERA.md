# Sintomas de câmera

Quatro defeitos apareceram ao ligar a visão, e nenhum deles se parece com bug
de código: a suíte passava, o `ruff` estava limpo, e o sintoma era sempre "o
Optmus não enxerga direito". Ficam aqui com a medição que os expôs, porque é
pela medição que se distingue um do outro.

Todos foram achados apontando a câmera para alguma coisa — nenhum apareceu em
teste com mock. Isso não é crítica ao mock: os testes com `FakeCv2` pegam
regressão em segundos e sem hardware. É que a webcam tem comportamento que só
existe no mundo.

---

## 1. Onze segundos por captura (backend MSMF)

**Sintoma:** cada olhada levava ~10,4 s. A ferramenta era inutilizável.

**Medição**, mesma câmera, mesma resolução pedida:

| | MSMF (padrão no Windows) | DirectShow |
|---|---|---|
| `VideoCapture()` | 4.077 ms | 265 ms |
| `set()` × 2 | **6.282 ms** | 513 ms |
| primeiro `read()` | 775 ms | 729 ms |
| **total** | **11.633 ms** | **2.555 ms** |

**Causa:** o MSMF reinicia a pipeline inteira a cada `set()`. Seis segundos só
para *sugerir* uma resolução.

**Correção:** preferir `cv2.CAP_DSHOW` no Windows, com fallback para o padrão
se ele não abrir — backend não é garantia, e 11 s é melhor que "não consigo
ver".

**Bônus inesperado:** com DirectShow a câmera aceita 1280×720. Pelo MSMF, pedir
1024×576 devolvia 640×480. O modo `descrever` passou a reduzir de 720p em vez
de aceitar VGA — mais nítido pelo mesmo custo em tokens.

---

## 2. `set()` mente

**Sintoma:** nenhum, e esse é o problema. O código pedia 1024×576, o driver
dizia "ok", e o quadro vinha 640×480.

**Medição:**

| pedido | `set()` retornou | `get()` alegava | quadro real |
|---|---|---|---|
| 1024×576 | `True` | 640×480 | 640×480 |
| 1280×720 | `True` | 1280×720 | 1280×720 |
| 3840×2160 | `True` | 1280×720 | 1280×720 |

**Causa:** `set()` sinaliza "aceitei o pedido", não "entreguei". Webcams
oferecem um conjunto discreto de modos e escolhem o mais próximo, ou ignoram.

**Regra:** **nunca confie em `set()` nem em `get()`.** A resolução real se mede
no `quadro.shape`. Confiar no driver faria o Core reportar
`resolucao_entregue: (1024, 576)` para uma imagem 640×480 — e o custo em tokens
calculado em cima disso estaria errado junto.

---

## 3. O aquecimento fixo capturava no pior momento

**Sintoma:** com um papel branco na frente, a foto saía um retângulo branco. O
modelo dizia — corretamente — que não dava para ler.

**Medição**, câmera apontada para uma tela clara num quarto escuro:

| quadro | brilho médio | pixels saturados |
|---|---|---|
| 0 | 180,6 | 57,1% |
| **5** | 227,4 | **80,3%** ← onde o código capturava |
| 10 | 182,4 | 32,3% |
| 20 | 140,9 | 2,1% |
| 39 | 99,7 | **0,0%** ← texto legível |

**Causa:** `QUADROS_DE_AQUECIMENTO = 5` não era só insuficiente — caía perto do
**pico** da superexposição. A auto-exposição leva ~40 quadros para assentar num
assunto claro.

**O erro de raciocínio por trás:** eu tinha medido o aquecimento antes e
concluído que não fazia diferença — variação de 0,1% no brilho entre os quadros
0 e 5. A medição estava certa; a conclusão, errada. Medi um **quarto escuro e
uniforme**, que é exatamente o caso em que a exposição já nasce boa. O cenário
que o modo `ler` existe para atender é o oposto, e era o único que importava
testar.

**Correção:** aquecimento adaptativo, com critério **duplo** — cada metade
falha num cenário oposto:

- **só estabilidade de brilho:** uma parede branca de verdade assenta em 250 com
  60% dos pixels estourados. Sairia "convergido" com a foto ilegível.
- **só saturação:** um quarto escuro tem 0% de saturação desde o quadro zero.
  Sairia no primeiro quadro, sem aquecimento nenhum.

Sai quando o brilho parou de mudar (< 1,5% por quadro, três seguidos) **e** a
saturação está abaixo de 5%, respeitando um piso de 5 quadros. Teto de 45
quadros e 3 s, porque cena que pisca — monitor, lâmpada — nunca assenta, e
esperar para sempre seria pior que uma foto imperfeita.

**Metadado que ficou:** `exposicao_convergiu`. Falso significa "a foto pode
estar clara ou escura demais" — é a diferença entre *"não há texto"* e *"havia
texto e a foto queimou"*, que foi exatamente o engano cometido aqui.

---

## 4. O teste sintético não pega nada disso

Vale explicitar, porque é a lição transferível: os três defeitos acima passaram
por uma suíte verde. O `FakeCv2` devolve o quadro que você mandar, na hora,
sem exposição, sem driver e sem backend.

O que fecha a lacuna:

```
python scripts/checar_visao.py          # fluxo completo, duas capturas
python scripts/checar_visao.py --ler    # papel escrito na frente da câmera
```

E, para diagnóstico rápido sem gastar chave, capturar um quadro e **olhar**:

```python
async with CameraCapture(1280, 720) as c:
    jpeg, meta = await c.capturar()
pathlib.Path("quadro.jpg").write_bytes(jpeg)
```

Foi assim que o defeito 3 apareceu — vendo o retângulo branco, em vez de
deduzir pela resposta do modelo.
