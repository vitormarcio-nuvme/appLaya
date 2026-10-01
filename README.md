# AUTOPISTA

Jogo de rodovia em **Python + tkinter** em que quem dirige é um **LLM local** (o Laya, via API System One). A cada ~0,5 s o jogo descreve a pista em texto e o modelo responde com um comando de uma palavra. Há ainda um piloto heurístico (sem rede) para comparação e o modo manual.

- Sem dependências externas: só a biblioteca padrão do Python + tkinter.
- Chamadas ao modelo em thread separada: a simulação nunca congela esperando a resposta.
- Painel de telemetria com latência, diário de decisões e histograma de comandos.

## Requisitos

- Python 3.8+ com tkinter (Windows e macOS já trazem; no Debian/Ubuntu: `sudo apt install python3-tk`).
- Para o modo LAYA: o servidor do Laya rodando em `http://localhost:8000/v1/systemone` (ou outro endereço via `--url`). Sem servidor, o jogo funciona com o piloto local ou no modo manual.

## Como executar

```bash
python autopista.py
python autopista.py --url http://localhost:8000/v1/systemone --model systemone
python autopista.py --zoom 1.0          # janela menor
python autopista.py --seed 42           # semente do tráfego
```

| Argumento | Padrão | Descrição |
|---|---|---|
| `--url` | `http://localhost:8000/v1/systemone` | Endpoint do Laya |
| `--model` | `systemone` | Nome do modelo enviado na requisição (também editável no painel, campo MODELO) |
| `--zoom` | `1.25` | Escala da janela |
| `--seed` | aleatória | Semente do gerador de tráfego |

## Controles

| Tecla | Ação |
|---|---|
| `Espaço` | Inicia, pausa e retoma (um clique na pista também inicia) |
| `R` | Reinicia a corrida |
| `1` / `2` / `3` | Troca o piloto: **LAYA** (LLM), **LOCAL** (heurístico), **MANUAL** |
| `←` / `→` | Troca de faixa (modo manual) |
| `↑` / `↓` | Acelera / freia (modo manual) |

As teclas do jogo (`Espaço`, `R`, `1`/`2`/`3`) ficam desativadas enquanto o cursor estiver no campo MODELO do painel.

## Os três pilotos

**LAYA (LLM).** A cada `DECIDE_S` (0,52 s) o jogo monta um snapshot em texto e faz um POST ao endpoint. Se o servidor falhar duas vezes seguidas, o estado vira *offline*, o piloto local assume e o jogo tenta reconectar a cada 3 s. Respostas vazias ou ilegíveis também caem no piloto local.

**LOCAL.** Heurística sem rede: aborta trocas de faixa que ficaram perigosas, evita faixas com carro ao lado ou vindo rápido por trás, escolhe a faixa vizinha com mais espaço e freia quando não há rota segura. As distâncias de segurança crescem com a velocidade.

**MANUAL.** Você dirige com as setas.

## Contrato com o Laya

Requisição (estilo chat completions):

```json
{
  "model": "systemone",
  "messages": [
    {"role": "system", "content": "<regras do jogo e dos comandos>"},
    {"role": "user",   "content": "<snapshot da pista>"}
  ],
  "temperature": 0.2,
  "max_tokens": 24,
  "stream": false
}
```

Exemplo de snapshot (`user`):

```text
velocidade 100 km/h (cruzeiro 105) · faixa CENTRO · troca: nenhuma · nível 1
ESQUERDA: livre
CENTRO: sedan a 38 m a 60 km/h (TTC 3.4 s)
DIREITA: livre
Responda somente o comando.
```

Cada faixa informa o veículo **ao lado** (ponto cego), o mais próximo **à frente** (distância, velocidade e TTC quando você está se aproximando) e o mais próximo **atrás**.

Resposta esperada: **uma única palavra** entre `ESQUERDA`, `DIREITA`, `ACELERAR`, `FREAR` e `MANTER`.

- O parser procura a palavra de comando que aparece **primeiro** na resposta e aceita alguns sinônimos.
- Uma letra solta só vale se for a resposta inteira.
- Leitura do corpo tolerante: `choices[0].message.content`, `choices[0].text`, `content`, `response`, `output`, `completion` ou texto puro.

> Modelos que "pensam" antes de responder podem estourar o limite de `max_tokens=24` e cair no piloto local. Ajuste a constante se for o caso.

## Como o jogo funciona

- **Pista:** 3 faixas. O carro fica fixo na tela e o mundo rola; 1 m = 7 px.
- **Velocidade:** é um *setpoint* (`set_v`). `ACELERAR` e `FREAR` mexem nele durante `HOLD_S` (0,75 s) e a velocidade real o persegue. `MANTER` conserva o valor atual. Um `FREAR` derruba a velocidade bastante: depois de frear, é preciso `ACELERAR` para voltar ao cruzeiro.
- **Troca de faixa:** os comandos `ESQUERDA`/`DIREITA` são relativos à faixa de destino. Enquanto uma troca está em andamento, uma segunda troca é ignorada; só o comando que volta à faixa de origem (abortar) é aceito.
- **Níveis:** sobem a cada 700 m, aumentando a velocidade máxima e a densidade de tráfego.
- **Tráfego:** cada carro tem uma velocidade desejada e respeita o carro à frente na mesma faixa (mantém distância, acompanha o líder e freia se colar), então os carros não se atravessam.
- **Batidas:** são classificadas (traseira, lateral em faixa vizinha, troca para faixa ocupada) e aparecem na tela final.

## Telemetria

O painel mostra o estado da conexão (online / standby / offline), a latência média, o comando atual e sua origem, a percepção por faixa, o diário das últimas decisões e o histograma de comandos. As métricas contam **mudanças de comando** (e cada troca de faixa efetiva), então `MANTER` repetido não infla os números.

## Configuração

Constantes no topo de `autopista.py`:

| Constante | Valor | Função |
|---|---|---|
| `LAYA_URL` | `http://localhost:8000/v1/systemone` | Endpoint padrão |
| `TIMEOUT_S` | `3.0` | Timeout de cada chamada ao Laya |
| `DECIDE_S` | `0.52` | Intervalo entre perguntas ao LLM (tempo de simulação) |
| `LOCAL_S` | `0.14` | Intervalo do piloto heurístico |
| `PROBE_S` | `3.0` | Intervalo de reconexão quando offline |
| `HOLD_S` | `0.75` | Duração do efeito de `ACELERAR`/`FREAR` |

## Estrutura do código

| Parte | Função |
|---|---|
| `Game` | Física, tráfego, percepção, piloto local e regras. Não depende de tkinter |
| `LayaAgent` | Thread de rede, fila de resultado com contador de geração (descarta respostas antigas) e fallback |
| `View` | Desenho da pista, carros, fumaça, destroços e HUD |
| `Panel` | Telemetria, diário e seleção de modo |
| `Ctrl` | Teclado, início, pausa e troca de piloto |

Como `Game` não usa tkinter, dá para rodá-lo sem interface (por exemplo, para testar o piloto local em muitas corridas) importando o módulo e chamando `update(dt)` num laço.

## Limitações conhecidas

- Os carros do tráfego respeitam uns aos outros, mas **ignoram o jogador**: quem vem atrás pode bater em você quando você freia.
- A simulação usa `dt` limitado a 33 ms; em máquinas lentas o jogo roda em câmera lenta e a latência do LLM pesa menos do que deveria, o que pode favorecer o modelo em comparações.
- A semente (`--seed`) vale só para a primeira corrida; ao reiniciar, o tráfego muda.
- `TIMEOUT_S` e `max_tokens` são fixos no código.
- Depois de editar o campo MODELO, o teclado pode continuar com o campo e não com o jogo.
- No macOS o tkinter costuma ignorar o `stipple`, então o véu de pausa pode aparecer sólido.