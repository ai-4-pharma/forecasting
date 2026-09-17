# MANUAL DE USO — Forecast Community

> Guia completo para o usuário. Versão correspondente ao estado do projeto em 16/09/2026.

---

## Sumário

1. [O que é o Forecast Community](#1-o-que-é-o-forecast-community)
2. [Instalação](#2-instalação)
3. [Iniciando a aplicação](#3-iniciando-a-aplicação)
4. [Jornada passo a passo](#4-jornada-passo-a-passo)
   - [Etapa 1 — Arquivo](#etapa-1--arquivo)
   - [Etapa 2 — Mapeamento](#etapa-2--mapeamento)
   - [Etapa 3 — Qualidade](#etapa-3--qualidade)
   - [Etapa 4 — Previsão](#etapa-4--previsão)
   - [Etapa 5 — Resultados](#etapa-5--resultados)
5. [Formatos de arquivo aceitos](#5-formatos-de-arquivo-aceitos)
6. [Modelos de previsão](#6-modelos-de-previsão)
   - [Funcionamento técnico de cada método](#funcionamento-técnico-de-cada-método)
7. [Validação temporal e métricas por método](#7-validação-temporal-e-métricas-por-método)
8. [Premissas e cenários](#8-premissas-e-cenários)
9. [Regressoras exógenas](#9-regressoras-exógenas)
10. [Hierarquia Bottom-Up / MinT](#10-hierarquia-bottom-up--mint)
11. [MAT — Acumulado Móvel de 12 Meses](#11-mat--acumulado-móvel-de-12-meses)
12. [Dashboard e visualização](#12-dashboard-e-visualização)
13. [Exportação de resultados](#13-exportação-de-resultados)
14. [Diagnóstico de qualidade](#14-diagnóstico-de-qualidade)
15. [Regras e limites do produto](#15-regras-e-limites-do-produto)
16. [Solução de problemas](#16-solução-de-problemas)
17. [Glossário](#17-glossário)

---

## 1. O que é o Forecast Community

O **Forecast Community** é uma ferramenta **local** de previsão de demanda desenvolvida para analistas de BI, SFE e dados da indústria farmacêutica e de bens de consumo. Toda a execução acontece no seu computador — seus dados nunca saem da sua máquina.

### O que você pode fazer

- Configurar estudos com múltiplas dimensões, frequências e medidas
- Importar dados em CSV ou XLSX (formato longo ou largo)
- Diagnosticar e tratar problemas de qualidade antes de modelar
- Escolher os métodos de previsão e obter previsões de **todos** eles por série, com
  métricas de backtest para decidir (a aplicação não elege um vencedor)
- Aplicar premissas de negócio (reajustes, campanhas, CMED) como cenários determinísticos
- Usar variáveis exógenas (regressoras) como entrada adicional em modelos selecionados
- Reconciliar previsões em hierarquias (ex.: SKU → Marca → Molécula → Total) por Bottom-Up/MinT
- Exportar resultados completos em CSV ou XLSX com metadados da rodada

### O que está fora do escopo

- Previsão diária ou semanal
- Modelos neurais ou redes profundas
- Acesso à internet, APIs comerciais ou serviços na nuvem
- Colaboração simultânea de múltiplos usuários no mesmo banco
- Ajuste automático de preços ou inferência de regras regulatórias

---

## 2. Instalação

### Pré-requisitos

- **Python 3.12** (versão de referência; também testado com 3.13.1)
- Windows ou Linux
- Acesso à internet apenas durante a instalação dos pacotes

### Passo a passo

```bash
# 1. Navegue até a pasta do projeto
cd caminho/para/forecasting

# 2. Crie um ambiente virtual
python -m venv .venv

# 3. Ative o ambiente
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

# 4. Instale as dependências (arquivo único: núcleo + ML)
pip install -r requirements.txt
```

O `requirements.txt` é único e já inclui **MLForecast, LightGBM e XGBoost** — os
modelos globais da Etapa 4 ficam disponíveis sem nenhum passo extra. Quem quiser uma
instalação mínima pode remover o bloco "Aprendizado global" do arquivo: o restante da
aplicação continua funcionando.

### Instalar dependências de desenvolvimento

Necessário apenas para executar os testes automatizados.

```bash
pip install -r dev/requirements-dev.txt
```

### Verificar a instalação

```bash
python -c "import streamlit, polars, statsforecast, duckdb; print('OK')"
```

---

## 3. Iniciando a aplicação

```bash
python -m streamlit run app.py
```

A aplicação abre automaticamente no navegador em `http://localhost:8501`.

> **Nota:** ao abrir pela primeira vez, a pasta `.local/` é criada automaticamente com o banco DuckDB. Essa pasta contém seus dados de trabalho e **nunca deve ser versionada**.

### Sobre sessões e reinício

- Fechar e reabrir o navegador mantém os estudos e rodadas anteriores.
- Recarregar a página permite selecionar o último estudo concluído.
- Se a aplicação fechar inesperadamente durante uma rodada, o status da rodada é marcado como `interrupted` na reabertura; os resultados parciais ficam acessíveis para inspeção.
- **Uma instância por vez.** Se uma segunda janela tentar abrir o mesmo banco, receberá uma orientação para fechar a primeira.

---

## 4. Jornada passo a passo

O Forecast Community é um **assistente linear (wizard)** de 5 etapas: **Arquivo →
Mapeamento → Qualidade → Previsão → Resultados**. Uma barra de progresso no topo
mostra a etapa atual, e o rodapé traz os botões **← Voltar** e **Avançar →** — este
último só é habilitado quando a etapa corrente está completa (se estiver
desabilitado, o motivo aparece logo acima do botão). Ações de conclusão
("Confirmar mapeamento", "Preparar dados") avançam automaticamente para a
próxima etapa após o sucesso; não é preciso navegar manualmente pela barra
lateral. A barra lateral serve apenas como indicador de progresso (✅ concluída,
▶ atual, ○ pendente) e como atalho **"Abrir estudo salvo"**, que retoma um
estudo já importado direto na etapa Qualidade.

Para instalar e abrir a aplicação no Windows com dois cliques, veja
[`instalar.bat` e `iniciar.bat`](#2-instalação).

### Etapa 1 — Arquivo

A configuração **deve acontecer antes do upload** dos dados. Ela determina o template que você vai baixar e como seus dados serão interpretados.

**Campos a preencher:**

| Campo | Descrição |
|-------|-----------|
| **Nome do estudo** | Identificador livre para sua análise |
| **Formato dos dados** | Longo (uma linha por período) ou Largo (colunas por período) |
| **Opções de leitura (CSV)** | Encoding, separador de colunas e separador decimal — "Detectar automaticamente" por padrão; mudar a opção já reaplica a leitura |

**Baixar modelo:** o botão **"⬇️ Baixar modelo (XLSX)"** gera um arquivo de exemplo com o formato escolhido e um expander **"Como preparar meu arquivo?"** resume as regras principais (formato longo × largo, período `YYYYMM`, uma medida por coluna, sem células mescladas).

Depois do upload, a prévia das colunas detectadas aparece na tela; clique em **Avançar →** para configurar dimensões, hierarquia e medidas.

---

### Etapa 2 — Mapeamento

Aqui você mapeia as colunas do arquivo carregado e define o restante da configuração do estudo.

**Processo:**

1. **Identificadores de entidade:** selecione as colunas que formam a chave (dimensões).
2. **Hierarquia e grão de análise:** ordene as dimensões do mais geral ao mais específico; o último nível define o **grão de análise**.
3. **Medidas:** `unidades`, `valor` ou ambas (formato Longo); no formato Largo, uma única medida por importação.
4. **Mapa temporal:** para formato **Largo**, o app já sugere o período (`YYYYMM`, `YYYY-MM`, `MM/YYYY` ou data do Excel); para colunas rotuladas como `Mês 1`, `Mês 2`…`Mês 60`, confirme que `Mês 1` é o último período fechado.
5. **Moeda, frequência de origem/modelagem, último período fechado e quantidade de períodos:** a aplicação já sugere valores a partir dos dados carregados (você pode ajustar).
6. **Configurações avançadas:** intervalo de confiança, moeda e demais parâmetros finos.
7. **Confirmar mapeamento:** a prévia mostra as primeiras linhas já mapeadas; clicar de novo com a mesma configuração não duplica o dataset. Após confirmar, o app avança automaticamente para a etapa Qualidade.

**O que é validado nessa etapa:**
- Chaves inválidas ou ausentes
- Datas ambíguas
- Cabeçalhos duplicados ou vazios
- Células mescladas (XLSX)
- Fórmulas sem valor calculado
- Arquivo acima do limite (100 MiB / 1.000.000 linhas físicas / 600.000 observações canônicas)
- Unidades fracionárias (bloqueiam até resolução)

> **Atenção com EAN:** identificadores numéricos como EAN são sempre preservados como texto, incluindo zeros à esquerda. Se o XLSX já perdeu esses zeros por armazenamento numérico, a aplicação informa a perda — sem inventar dígitos.

---

### Etapa 3 — Qualidade

Esta etapa apresenta o diagnóstico por série e pede decisões explícitas sobre cada tipo de problema. No topo, três indicadores resumem a base (séries, erros impeditivos, avisos); erros que nenhuma política de tratamento resolve (ex.: unidades fracionárias) desabilitam o botão "Preparar dados" e explicam que é preciso voltar à etapa Mapeamento.

#### Diagnóstico por série

Para cada entidade/medida, a aplicação calcula:

| Indicador | Descrição |
|-----------|-----------|
| Início / Fim | Primeiro e último período observado |
| Períodos esperados / observados | Cobertura da grade temporal |
| Nulos e lacunas | Períodos ausentes na grade |
| Zeros | Quantidade e proporção de valores zero |
| Negativos | Existência de valores negativos |
| Duplicidades | Linhas com mesma chave/período/medida |
| **ADI** | Intervalo médio de demanda (para intermitência) |
| **CV²** | Coeficiente de variação ao quadrado |
| Outliers | Pontos ≥ 3 × 1,4826 × MAD acima da mediana sazonal |
| Elegibilidade | Se a série tem mínimos para entrar em modelagem |

#### Decisões disponíveis

| Problema | Opções |
|----------|--------|
| **Duplicidade** | Rejeitar linhas duplicadas OU somar (se forem linhas aditivas de negócio) |
| **Nulos / lacunas** | Excluir a série OU preencher com zero OU preencher com o último valor observado (`ffill`) |
| **Negativos** | Rejeitar OU aceitar como venda líquida (habilita apenas candidatos compatíveis) |
| **Outliers** | Manter OU winsoriziar até o quantil configurado (padrão: 99º percentil, calculado no treino) |

> **Regra importante:** a preparação é estimada com base no período de treino de cada fold — não na base completa. Isso evita vazamento de informação futura para o modelo.

#### Séries inelegíveis

Séries sem histórico suficiente para validação cruzada recebem o modelo `Naive` como baseline, com status `insufficient_history`. Elas entram na rodada com esse método de referência.

---

### Etapa 4 — Previsão

Esta etapa reúne a configuração de premissas/modelos e a execução da rodada.

**Premissas e cenários (opcional):** fica num expander recolhido por padrão —
cadastre cenários e regressoras só se precisar de ajustes incrementais sobre a
previsão base; cada cenário pode ser excluído com o botão "Excluir cenário".
As premissas não são regravadas a cada interação, só quando algo muda de fato.

**Horizonte, Modo e Métodos** ficam sempre visíveis (reagem na hora: o aviso de
horizonte > 24 períodos aparece assim que você digita o valor). Os demais
parâmetros ficam em **"Opções avançadas"**:

| Parâmetro | Descrição | Padrão |
|-----------|-----------|--------|
| **Horizonte** | Número de períodos futuros a prever | 12 |
| **Modo** | "Incluir métodos avançados (ARIMA, CES e TBATS)": modo Rápido = núcleo (ETS, Theta, Holt, médias móveis); Avançado = + `AutoCES`, `AutoARIMA`, `AutoTBATS` | Desligado |
| **Métodos** | Multiseletor com o catálogo completo — **todos os marcados são executados** | `Naive`, `HistoricAverage`, `RegLinearDrift`, `AutoETS` |
| **ML Global** | Habilitar LightGBM/XGBoost (já instalados com o `requirements.txt`) | Desabilitado |
| **Nível do intervalo** | Percentual do intervalo de previsão solicitado (suportado: Holt, AutoETS, AutoTheta, AutoARIMA, CES etc.) | 80% |
| **Tamanho do lote** | Entidades processadas por lote (máx. 500) | 250 |
| **Threads** | Paralelismo entre séries (máx. 4; em Windows 1 evita travamentos) | 1 |
| **Hierarquia** | Independente, Bottom-Up ou MinT — Bottom-Up/MinT desabilitam "Nível a modelar" | Independente |
| **Dimensões dos níveis** | Ordem topo → folha dos níveis a considerar | Ordem do mapeamento |
| **Nível a modelar** | Qual nível agregado modelar (só em Independente) | Nível mais detalhado |

Ainda na Etapa 4, expandindo **"Premissas e cenários (opcional)"**, você cadastra
cenários/regressoras — e a seção **"Prévia das premissas"** mostra entidades afetadas
e fatores por período antes de executar.

#### Hierarquia

| Modo | Comportamento |
|------|--------------|
| **Independente** | Modela cada entidade no nível escolhido; sem coerência entre níveis |
| **Bottom-Up** | Modela o nível mais detalhado e soma os pais — por método; coerência matemática garantida |
| **MinT** | Reconciliado a partir das folhas (pais coerentes, rotulados como MinT na saída) |

#### Executar a rodada

Clique em **"▶ Executar previsão"** para iniciar. A aplicação:

1. Valida a configuração e verifica conflitos de premissas.
2. Executa a validação temporal (CV) dos métodos escolhidos em lotes de 250 entidades.
3. Reajusta **cada método escolhido** com todo o histórico elegível e projeta o
   horizonte — uma série de previsões por método por série.
4. Aplica cenários e premissas determinísticas.
5. Reconcilia Bottom-Up/MinT (se configurado).
6. Persiste resultados por lote (idempotente — pode ser chamado duas vezes sem duplicar).

**Progresso:** a UI exibe o estágio atual (`cv`, `forecast`, `scenario`, `reconcile`) e avança sozinha de lote em lote (sem cliques intermediários); o botão **Cancelar** fica visível durante todo o processamento. Uma rodada cancelada preserva os resultados parciais para inspeção. Ao concluir, clique em **"Ver resultados →"** para ir à etapa Resultados.

**Status de rodada:**
- `running` — em andamento
- `completed` — todas as séries elegíveis processadas
- `partial` — alguma série sem saída (um método escolhido não produziu previsão)
- `cancelled` — cancelado pelo usuário entre lotes
- `failed` — erro crítico
- `interrupted` — aplicação fechou durante a execução

---

### Etapa 5 — Resultados

Veja os resultados no dashboard e exporte em CSV ou XLSX com **um clique** — os botões de download já vêm prontos, sem precisar gerar o arquivo antes.

Filtros disponíveis:
- Medida (Unidades / Valor)
- Cenário (base ou qualquer cenário criado)
- Dimensão em que consolidar ("Analisar por") e itens com busca textual
- Método de referência (guia cards/tabela) e métodos exibidos no gráfico

---

## 5. Formatos de arquivo aceitos

### Formato A — Longo (uma linha por período)

Cada linha representa uma observação de uma entidade em um período.

```csv
classe;molecula;marca;ean;periodo;unidades;valor
Classe A;Molecula A;Marca A;0123456789012;2026-07;100;1500.00
Classe A;Molecula A;Marca A;0123456789012;2026-08;110;1650.00
```

### Formato B — Largo (colunas por período)

Cada linha representa uma entidade; os períodos são colunas.

```csv
classe;marca;ean;medida;2026-07;2026-08
Classe A;Marca A;0123456789012;unidades;100;110
Classe A;Marca A;0123456789012;valor;1500.00;1650.00
```

Também aceita o layout `Mês 60 ... Mês 1` (do mais antigo para o mais recente), confirmando que `Mês 1` é o último período fechado informado.

### Formatos de data aceitos

| Formato | Exemplo |
|---------|---------|
| YYYY-MM | 2026-07 |
| YYYYMM | 202607 |
| Data real | 2026-07-01 |
| Trimestre | 2026-Q3 |
| Anual | 2026 |
| MAT com mês de encerramento | MAT-2026-07 |

Datas ambíguas são rejeitadas até o usuário confirmar o formato.

---

## 6. Modelos de previsão

A Etapa 4 mostra um **catálogo dinâmico** com os métodos disponíveis para a sua
configuração e deixa você marcar quais executar. O Forecast Community **não decide
por você**: cada método marcado é executado por série, suas previsões são persistidas
e o dashboard mostra o backtest de cada um — a escolha final é sua.

Os métodos estatísticos usam o pacote **StatsForecast** (Nixtla), com implementações
otimizadas (Numba onde aplicável). Os métodos globais de aprendizado de máquina usam
**MLForecast** (LightGBM / XGBoost).

> **Sobre a sazonalidade:** o comprimento do ciclo sazonal é configurável em
> *Opções avançadas de execução → "Comprimento da sazonalidade (ciclo)"*:
> `auto` (padrão — 12 para mensal/MAT, 4 para trimestral, 1 para anual), ou um valor
> explícito (12 anual, 6 semestral, 4, 3 trimestral, 2 bimestral, 1 sem sazonalidade).
> O ciclo escolhido é passado a `SeasonalNaive`, `AutoETS`, `ETS_Damped`, `AutoTheta`,
> `AutoCES`, `AutoTBATS`, `AutoARIMA` (SARIMA) e `AutoARIMA_X`.
> `Holt`/`HoltDamped` (tendência) e `CrostonSBA`/`TSB` (intermitentes) não usam
> sazonalidade por construção.
> Se a série tiver sazonalidade forte, prefira ciclo ≥ 2 e concorra também com os
> métodos sazonais (`SeasonalNaive`, `AutoTBATS`) no multiseletor.

### Visão geral dos métodos

| Método | Tipo | Onde aparece | Uso resumido |
|--------|------|--------------|----------------|
| `Naive` | Baseline | Sempre | Benchmark; série estável |
| `HistoricAverage` | Baseline | Sempre | Série sem tendência nem ciclo |
| `MediaMovel3/6/12` | Baseline (janela) | Sempre | Suavizar ruído de curto prazo |
| `SeasonalNaive` | Baseline sazonal | Mensal/trimestral | Ciclo claro e estável |
| `RegLinearDrift` | Tendência | Sempre | Crescimento/queda constante |
| `Holt` / `HoltDamped` | Suavização (tendência) | Sempre | Tendência, com ou sem amortecimento |
| `AutoETS` / `ETS_Damped` | Suavização exponencial | Sempre | Série com nível/tendência (menor complexidade) |
| `AutoTheta` | Theta | Sempre | Série regular e estável |
| `CrostonSBA` / `TSB` | Intermitente | Séries com zeros | Demanda esporádica |
| `AutoCES` | CES | Modo avançado | Alternativa robusta à ETS |
| `AutoARIMA` / `AutoARIMA_X` | ARIMA / ARIMAX | Modo avançado (X: com regressoras) | Padrões autorregressivos / com variáveis externas |
| `AutoTBATS` | TBATS | Modo avançado | Sazonalidade forte/complexa |
| `LightGBM` / `XGBoost` | Global ML | Checkbox ML | Muitas séries, padrões comuns |
| `ZeroBaseline` | Previsão zero | Automático | Série toda zero |

Seleção padrão da interface (primeira visita): `Naive`, `HistoricAverage`,
`RegLinearDrift` e `AutoETS`. O catálogo também mostra **"Como funciona"** de cada
método ao lado do nome.

### Funcionamento técnico de cada método

Cada bloco abaixo explica a teoria por trás do método, os parâmetros que a aplicação
usa e as situações mais adequadas.

---

#### 6.1 `Naive` — Último valor observado

O modelo mais simples de todos: a projeção repete **o último valor observado** para
todo o horizonte. Não há parâmetros a estimar — basta a observação mais recente
(`ŷ_{t+h} = y_t`). Por isso serve de **benchmark**: qualquer método mais elaborado só
tem valor se conseguir errar menos que ele. Na aplicação ele é o baseline universal e
também o método aplicado a séries com histórico insuficiente (`insufficient_history`),
além de ser usado como modelo de recurso quando um método escolhido falha.

**Situações mais adequadas de uso:** séries de substituição com comportamento estável
e sem tendência; como referência de comparação nas "Métricas por série e método";
séries curtas demais para qualquer outro modelo.

---

#### 6.2 `HistoricAverage` — Média histórica

A projeção é a **média aritmética de todas as observações do histórico**
(`ŷ = Σ y_i / n`), constante para todo o horizonte. O cálculo usa a série inteira
disponível no corte, com peso igual para todos os períodos — não existe noção de
quanto tempo atrás o dado ocorreu. Funciona bem quando o processo é estacionário em
nível, sem tendência, sem sazonalidade e sem grandes mudanças de patamar.

**Situações mais adequadas de uso:** produtos cuja demanda oscila em torno de um nível
médio estável por anos (ex.: reposição de itens maduros sem promoções); cenário "e se
a demanda mantiver a média histórica".

---

#### 6.3 `MediaMovel3`, `MediaMovel6`, `MediaMovel12` — Média móvel de janela fixa

A previsão é a **média aritmética das N observações mais recentes** (média móvel simples),
constante para todo o horizonte: `ŷ_{t+h} = (y_t + y_{t-1} + … + y_{t-N+1}) / N`. Diferente
da média histórica, ela descarta tudo que é mais antigo que a janela — reagindo mais
rápido a mudanças recentes de nível, ao custo de seguir ruídos. A janela define o
compromisso: quanto maior, mais suave e mais lenta a reação; quanto menor, mais ágil
e menos estável. Na aplicação, as janelas são 3, 6 e 12 períodos (na frequência do estudo).

**Situações mais adequadas de uso:** `MediaMovel3` para suavizar oscilações muito
recentes; `MediaMovel6` para curto prazo com ruído moderado; `MediaMovel12` como
"referência anual de nível médio" (ex.: comparar o patamar dos últimos 12 meses).

---

#### 6.4 `SeasonalNaive` — Sazonal naive

Repete **o valor observado no mesmo período do ciclo anterior**: para cada passo `h`,
`ŷ_{t+h} = y_{t+h-s}`, em que `s` é o comprimento da sazonalidade (**12 para dados
mensais, 4 para trimestrais**). É a versão sazonal do Naive: captura o padrão cíclico
sem estimar nenhum parâmetro. Na prática, precisa de ao menos um ciclo completo no
histórico para ter referência (idealmente dois ciclos). Na aplicação ele está
disponível apenas para frequências mensal e trimestral.

**Situações mais adequadas de uso:** séries com sazonalidade clara e estável ano a ano
(ex.: produtos com pico no verão ou em datas fixas), quando o nível não muda de um ano
para o outro; como benchmark sazonal para validar modelos mais elaborados.

---

#### 6.5 `RegLinearDrift` — Random walk com drift (tendência)

Parte do último valor observado e projeta uma **tendência linear** calculada entre o
primeiro e o último ponto do histórico: `ŷ_{t+h} = y_t + h·d`, com
`d = (y_t − y_1)/(n − 1)`. Em outras palavras, assume que o ganho (ou perda) médio por
período observado entre o início e o fim da série se mantém no futuro. É um dos
"random walk with drift" clássicos — simples, mas eficaz quando o crescimento (ou
declínio) é mais ou menos constante.

**Situações mais adequadas de uso:** séries com tendência crescente ou decrescente
relativamente estável e sem sazonalidade forte (ex.: rampa de lançamento, tendência de
uso de um medicamento); como visão conservadora de longo prazo (não acelera nem
amortiza além da média histórica).

---

#### 6.6 `Holt` — Suavização exponencial com tendência (dupla)

Extensão da suavização exponencial simples que adiciona uma **equação de tendência**.
São duas equações recursivas atualizadas a cada observação:
`nível: l_t = α·y_t + (1−α)·(l_{t−1} + b_{t−1})` e
`tendência: b_t = β·(l_t − l_{t−1}) + (1−β)·b_{t−1}`.
A projeção é `ŷ_{t+h} = l_t + h·b_t`. Os parâmetros de suavização **α e β**
(α entre 0 e 1 controla o peso do dado recente no nível; β controla a velocidade de
atualização da tendência) são estimados por otimização sobre o histórico — quanto
maiores, mais a série responde a mudanças recentes. Não há componente sazonal.

**Situações mais adequadas de uso:** séries com tendência de crescimento ou declínio
constante, sem sazonalidade ou com sazonalidade já tratada; curto e médio prazo, quando
se quer que a tendência recente tenha peso relevante.

---

#### 6.7 `HoltDamped` — Suavização com tendência amortecida

É o **Holt com amortecimento da tendência**: o modelo usado é ETS(A,Ad,N) — erro e
tendência aditivos, tendência amortecida, sem sazonalidade. Em vez de projetar a
tendência `b_t` de forma linear e indefinida, cada passo futuro a reduz por um fator
de amortecimento **φ** (0 < φ < 1):
`ŷ_{t+h} = l_t + (φ + φ² + … + φʰ)·b_t`. O efeito prático é que a projeção se curva
em direção a um platô, em vez de crescer (ou cair) para sempre — mais realista para
horizontes longos, onde extrapolar tendência linear tende a ser otimista demais.

**Situações mais adequadas de uso:** séries com tendência que se espera **desacelerar**
com o tempo (ex.: maturação de mercado, efeito de campanha que satura); horizontes
médios e longos em que o Holt linear superestima; é também a recomendação clássica
quando há pouca certeza sobre a persistência da tendência.

---

#### 6.8 `AutoETS` — Suavização exponencial automática (ETS)

A família ETS descreve a série com combinações de **Erro (E), Tendência (T) e
Sazonalidade (S)**, cada componente podendo ser aditivo, multiplicativo ou ausente
(ex.: ETS(A,N,N) = suavização exponencial simples). O `AutoETS` **testa combinações
de componentes e escolhe a melhor por critério de informação**, estimando os parâmetros
de suavização (α, β, γ) por máxima verossimilhança com as equações de espaço de estado.
Na configuração atual da aplicação ele opera na forma clássica (comprimento de
sazonalidade 1), de modo que a seleção incide principalmente sobre o tratamento de
nível e tendência.

**Situações mais adequadas de uso:** série de referência para séries com nível e
tendência e pouco ruído; produtos com comportamento regular; quando a sazonalidade é
fraca ou já descontada. Para séries fortemente sazonais, prefira `SeasonalNaive`,
`AutoARIMA` ou `AutoTBATS`.

---

#### 6.9 `ETS_Damped` — ETS com tendência amortecida

É o `AutoETS` com o amortecimento de tendência forçado (`damped=True`): a seleção
automática de componentes continua, mas a tendência, se presente, é amortecida por φ.
Funciona como meio-termo entre o ETS livre e o HoltDamped: mantém a flexibilidade do
AutoETS e ainda evita a extrapolação linear indefinida. Reúne as duas características
— a escolha automática de E/T/S e a curvatura da tendência para um platô.

**Situações mais adequadas de uso:** séries com tendência em que a ETS simples tende a
superestimar no longo prazo; quando se suspeita que o crescimento vai desacelerar; bom
default para horizontes médios/longos.

---

#### 6.10 `AutoTheta` — Método Theta

Deriva do modelo Theta vencedor do espaço M3 de competições de forecasting. A série
original é decomposta em duas séries "theta" — uma mantém a forma original e outra
**reduz/atenua sua curvatura** (a segunda derivada é multiplicada por um parâmetro
theta). Cada uma é projetada separadamente com suavização exponencial (ou regressão)
e as duas são **combinadas por pesos** para formar a previsão final; o `AutoTheta`
seleciona automaticamente o melhor theta/combinação. É um método robusto e barato
computacionalmente, com bom histórico de desempenho em séries regulares.

**Situações mais adequadas de uso:** séries suaves, com nível e tendência moderados e
poucos valores atípicos; como alternativa robusta ao ETS em concorrência; quando se
quer uma previsão estável sem parametrização manual.

---

#### 6.11 `CrostonSBA` — Demanda intermitente (Croston com correção SBA)

Para séries com **muitos zeros** (demanda que não ocorre todo período), Croston separa
o problema em duas partes, cada uma suavizada exponencialmente:
- o **intervalo** entre períodos com demanda (`q`, número de períodos entre vendas);
- o **tamanho** da demanda quando ela ocorre (`z`).

A previsão é `ŷ = z̄ / q̄` (tamanho médio dividido pelo intervalo médio). O **SBA**
(Syntetos–Boylan–Approximation) corrige o viés dessa razão multiplicando por `(1 − α/2)`,
sendo α o parâmetro de suavização dos dois componentes (estimado por otimização). O
resultado é uma taxa média mensal "comprimida" pelos zeros, adequada para acumular
demanda intermitente. Na aplicação, o método exige série com ≥ 2 valores positivos e
sem valores negativos.

**Situações mais adequadas de uso:** demanda intermitente — itens de baixo giro com
períodos longos sem venda (ex.: reposição de produtos com sazonalidade de prescrição);
quando o objetivo é o total acumulado do período em vez da curva mensal.

---

#### 6.12 `TSB` — Teunter–Syntetos–Babai

Evolução do Croston que modela explicitamente a **probabilidade de ocorrer demanda**:
duas variáveis são atualizadas quando há venda e **também quando não há**:
- probabilidade de demanda `p_t` é atualizada com αₚ (`p_t = (1−αₚ)·p_{t−1} + αₚ·d_t`,
  onde `d_t = 1` se houve venda, senão 0);
- tamanho médio `z_t` é atualizado com α_d apenas nos períodos com demanda.

A previsão é `ŷ = p_t · z_t`. Isso captura melhor mudanças de comportamento — se o
produto está "morrendo", a probabilidade cai mesmo nos períodos sem venda. Na aplicação
os parâmetros são fixados em **α_d = 0,2 e α_p = 0,2**, com a mesma elegibilidade do
CrostonSBA (≥ 2 valores positivos, sem negativos).

**Situações mais adequadas de uso:** demanda esporádica com **evolução do padrão** —
fim de ciclo de vida, descontinuações, lançamentos cuja frequência de venda ainda está
se formando; quando o comportamento intermitente muda ao longo do tempo.

---

#### 6.13 `AutoCES` — Complex Exponential Smoothing

O CES (Complex Exponential Smoothing) substitui o alisamento real por uma **equação de
espaço de estado com autovalores complexos**: o nível é suavizado por apenas **um
parâmetro complexo**, cuja parte imaginária codifica oscilações amortecidas. Com uma
única estrutura de equações ele captura combinações de nível, tendência e componentes
periódicos, selecionando automaticamente entre formas simples e sazonais (`model='Z'`).
Em geral produz previsões competitivas com a ETS, com comportamento particularmente
bom quando há ciclos quase periódicos na série. No catálogo, é a alternativa "estatística
avançada" de suavização do modo avançado.

**Situações mais adequadas de uso:** séries com ciclos quase periódicos e leves, como
alternativa ao AutoETS em concorrência; quando se quer comparar um método de suavização
mais moderno sem abrir mão de velocidade.

---

#### 6.14 `AutoARIMA` — ARIMA/SARIMA automático (Box–Jenkins)

O ARIMA combina três partes: **autorregressão** (p — usa valores passados da própria
série), **diferenciação** (d — remove tendência para estacionarizar) e **médias móveis**
(q — usa erros passados). Com sazonalidade, torna-se **SARIMA**, com partes análogas
(`P, D, Q`) associadas ao comprimento do ciclo (`s` = 12 mensal / 4 trimestral). O
`AutoARIMA` usa **busca automática de ordem**: testa combinações de p/d/q/P/D/Q e
escolhe pela melhor **AICc**, com os limites da aplicação `max_p=3, max_q=3, max_P=1,
max_Q=1` (e aproximação para velocidade). A previsão é recursiva sobre a estrutura
estimada, com os parâmetros ajustados por máxima verossimilhança.

**Situações mais adequadas de uso:** séries com **autocorrelação forte** (o valor
depende de vários períodos anteriores); com tendência e sazonalidade combinadas
(quando ETS empaca); concorrência estatística "de alta capacidade" no modo avançado.

---

#### 6.15 `AutoARIMA_X` — ARIMA com regressoras exógenas

É o `AutoARIMA` com **variáveis externas** (`X`): além da estrutura ARIMA/SARIMA
(mesmos limites p/q/P/Q), o modelo inclui regressores `X₁...Xₖ` com coeficientes β
estimados junto, na forma `y_t = ARIMA + Σ βₖ·Xₖₜ`. A previsão de cada passo usa os
**valores futuros das regressoras** fornecidos pelo usuário — por isso só entra no
catálogo quando há regressoras válidas (histórico e futuro preenchidos, sem colinearidade
perfeita, no máximo 5 por rodada). É o **único** candidato da aplicação que usa
regressoras no componente estatístico.

**Situações mais adequadas de uso:** quando a demanda é guiada por variáveis conhecidas
no futuro — preço/CMED, investimento de campo, campanhas, feriados móveis; séries em
que o efeito da variável externa é mensurável e persistente.

---

#### 6.16 `AutoTBATS` — TBATS (sazonalidade complexa)

O TBATS combina: transformação **Box–Cox** de potência, componentes de **tendência**,
erros **ARMA**, e **sazonalidade trigonométrica** — cada componente sazonal é ajustado
por uma combinação de senos/cossenos (séries de Fourier) em vez de um "passo" por
período. Isso permite modelar **múltiplos ciclos e sazonalidades não inteiras** sem
explodir o número de parâmetros. Sua seleção automática define a série de Fourier de
cada componente e a estrutura ARMA. Por exigir histórico longo e custo maior, na
aplicação ele só aparece no **modo avançado** e com séries de no mínimo 24 observações.

**Situações mais adequadas de uso:** séries longas com **sazonalidade forte e/ou
complexa** (padrões que mudam de forma ao longo do tempo); quando SeasonalNaive e SARIMA
ficam distantes do histórico; modelagem de calendários de demanda atípicos (datas
móveis, decomposição de ciclos combinados).

---

#### 6.17 `LightGBM` — Aprendizado global (gradient boosting)

Modelo global de **gradient boosting**: um único conjunto de árvores de decisão é
treinado com os dados de **todas as séries da mesma medida ao mesmo tempo**, aprendendo
padrões comuns (ex.: como o passado recente afeta o futuro nas várias unidades). As
características (`features`) de cada observação são causais: **lags 1, 2, 3** do valor,
**lag sazonal 12** (mensal) ou 4 (trimestral), **média móvel de 3 períodos** (deslocada
de 1 para não vazar o presente) e **variáveis de calendário** (mês/trimestre e ano). A
aplicação usa 200 árvores, taxa de aprendizado 0,05, `num_leaves=31`, semente 42, e o
reajuste final é único por medida (não por série), com previsão recursiva. Exige
**≥ 20 entidades** e **≥ 200 linhas treináveis** na medida, e os pacotes ML do `requirements.txt`.

**Situações mais adequadas de uso:** portfólios grandes (dezenas a milhares de SKUs)
em que séries curtas se beneficiam do padrão aprendido nas séries longas; quando
relações não lineares entre passado recente e futuro importam; comparar com os
estatísticos para capturar o que eles não veem.

---

#### 6.18 `XGBoost` — Aprendizado global (gradient boosting)

Mesma arquitetura global do `LightGBM` (MLForecast, lags 1–3, lag sazonal, média móvel
de 3 e calendário; treino único por medida), mas com o algoritmo **XGBRegressor**
(`n_estimators=200`, `learning_rate=0.05`, `max_depth=6`). As duas implementações de
gradient boosting costumam ter desempenho parecido em demanda; a diferença está em
detalhes de regularização e splits. Requer as mesmas condições do LightGBM (≥ 20
entidades, ≥ 200 linhas, pacotes ML instalados) e, além disso, o pacote `xgboost`.

**Situações mais adequadas de uso:** os mesmos cenários do LightGBM — muitos SKUs,
padrões comuns, horizonte curto/médio; útil para comparar as duas implementações de
boosting e escolher a que melhor adere ao backtest na sua base.

---

#### 6.19 `ZeroBaseline` — Previsão zero

Projeta **0 para todos os períodos do horizonte**, sem parâmetros. Na aplicação ele é
aplicado **automaticamente** a séries cujo histórico é inteiramente zero (produto que
nunca vendeu no período), para que a exportação e a hierarquia recebam valores coerentes
em vez de uma série sem saída. Ele **não aparece** no multiseletor da Etapa 4, pois não
faz sentido escolhê-lo manualmente — é um tratamento de consistência.

**Situações mais adequadas de uso:** séries com histórico 100% zero onde a previsão de
qualquer outro método não teria base; garantir coerência de somas em nós agregados que
contêm itens sem venda.

---

## 7. Validação temporal e métricas por método

A aplicação usa a validação cruzada temporal apenas para **diagnóstico**: ela mede
como cada método se sai no histórico. **Não há eleição de vencedor** — cada método que
você marcou na Etapa 4 é avaliado e previsto. As métricas ajudam você a decidir.

> **Como interpretar:** o backtest mostra o comportamento histórico de cada método sob
> a mesma lógica temporal. O método de menor erro em treino não é "escolhido" pela
> aplicação — cabe a você, com o contexto de negócio, definir em qual focar (o dashboard
> permite selecionar um **método de referência** para cards e tabela).

### Como funciona a validação cruzada

A aplicação usa **validação cruzada com treino expansivo** (também chamada de walk-forward ou time series split):

- Os dados **nunca são embaralhados**.
- Os folds de teste são **não sobrepostos**.
- O treino de um fold nunca consulta dados do período de teste.

```
Fold 1:  [TREINO 1 ......................] [TESTE 1]
Fold 2:  [TREINO 2 ......................+...........] [TESTE 2]
Fold 3:  [TREINO 3 ...............................+...........] [TESTE 3]
```

**Parâmetros dos folds:**

| Frequência | Horizonte de CV padrão | Folds (máx.) |
|------------|:---:|:---:|
| Mensal / MAT | min(horizonte, 3) | 3 |
| Trimestral | min(horizonte, 2) | 3 |
| Anual | 1 | 3 |

O maior número de folds K ≤ 3 para o qual o primeiro treino tem pelo menos **3 observações** é usado. Com K < 2, aplica-se o baseline `Naive` e a série é marcada como `insufficient_history`.

### Métricas calculadas

| Métrica | Fórmula | Uso |
|---------|---------|-----|
| **MAE** | `média(|yhat − y|)` | Diagnóstico de aderência (mesma unidade da medida) |
| **RMSE** | `√(média((yhat − y)²))` | Diagnóstico que penaliza erros maiores |
| **WAPE** | `Σ|yhat − y| / Σ|y|` | Percentual ponderado pelo volume; nulo se denominador zero |
| **Bias** | `média(yhat − y)` | Positivo = superestimação |

> MAPE **não** é usado como critério padrão devido à instabilidade com zeros.

Essas métricas são calculadas e gravadas **por série e por método** e também
**agregadas por método** na tela "Backtest por método (agregado)". Pequenos valores
de MAE/RMSE/WAPE indicam maior aderência ao histórico.

### Intervalos de previsão

A aplicação tenta produzir intervalos de **80%** (`lo80` / `hi80`):

- **Intervalo nativo:** quando o modelo suporta diretamente.
- **Predição conformal:** para modelos apenas pontuais, quando houver histórico suficiente (`≥ 2 × horizonte + treino mínimo`).
- **Nulo com motivo explícito:** quando não for viável — campo `interval_method=unavailable_insufficient_history`.

> Os intervalos de nós agregados (Bottom-Up/MinT) e MAT derivado são **sempre nulos** nesta versão, com motivo explicado na interface.

---

## 8. Premissas e cenários

Cenários são **ajustes multiplicativos** sobre a previsão base. Eles não substituem a tendência histórica implícita no modelo — são incrementos de negócio declarados explicitamente.

### Tipos de efeito

| Tipo | Comportamento | Fim obrigatório? |
|------|--------------|:-:|
| `pulse` | Fator `1+r` aplicado apenas entre início e fim, inclusive | Sim |
| `step` | Fator `1+r` a partir do início, persistente | Não |
| `annual_step` | Fator `(1+r)^k` acumulando a cada aniversário anual | Não |

**Exemplo `annual_step` de 5% iniciando em abril:**
- Abril do ano 1: fator 1,05
- Abril do ano 2: fator 1,1025 (= 1,05²)
- Março de qualquer ano: fator anterior ao próximo aniversário

### Famílias de regras

- Regras de **famílias diferentes** se compõem **multiplicativamente**:  
  `yhat_cenário = yhat_base × fator_A × fator_B × ...`
- Dentro da **mesma família**, ganha a regra de **maior prioridade**.
- **Empate de prioridade** na mesma família/entidade/medida/período → cenário **bloqueado** (`E_SCENARIO_PRIORITY_TIE`).

### Escopo das regras

Cada regra define:
- **Medida alvo** (`unidades` e/ou `valor` — nunca somados entre si)
- **Filtros de dimensão** (AND entre dimensões, OR dentro de cada dimensão)
- **Entidades excluídas explicitamente**
- **Vigência** (início obrigatório; fim opcional dependendo do tipo)

### Cenário base

O cenário `base` é **sempre gerado e imutável** — corresponde à previsão do modelo sem ajuste. Outros cenários são calculados a partir do base.

### CMED e reajustes de preço

O rótulo CMED é uma **premissa preenchida pelo usuário**; a aplicação não consulta o teto regulatório da ANVISA. A aplicação padrão do label CMED afeta apenas `valor`. Produtos fora do escopo devem ser excluídos explicitamente pelo filtro de dimensão ou lista de entidades excluídas.

### Pré-visualização

Antes de executar, a interface mostra uma prévia com:
- Quais entidades serão afetadas por cada regra
- O fator resultante por período
- Conflitos de prioridade detectados

---

## 9. Regressoras exógenas

Variáveis exógenas (regressoras) são séries externas que podem influenciar o modelo — por exemplo, preço, investimento em marketing ou índice de sazonalidade de mercado.

### Requisitos

Para uma regressora entrar no modelo:
1. Ter **histórico numérico** alinhado ao treino
2. Ter **variação histórica** (coluna constante não estima coeficiente útil)
3. Não ter **colinearidade perfeita** com outra regressora declarada
4. Ter **cobertura de todos os períodos futuros** do horizonte
5. Ter os valores **identificados como conhecidos ou desconhecidos** no corte de CV

### Disponibilidade no corte (CV)

| Política | Comportamento |
|----------|--------------|
| `known_in_advance` | Usa os valores reais do período de teste (você confirma que estavam disponíveis no corte) |
| `explicit_hold` | Usa o último valor do treino como projeção durante o teste |

### Modelos que aceitam regressoras

Apenas `AutoARIMA_X` usa regressoras no componente estatístico. Os modelos univariados continuam disponíveis e concorrem sem regressoras.

### Checklist de ativação do `AutoARIMA_X`

O `AutoARIMA_X` **só aparece no catálogo de métodos da Etapa 4 quando existe pelo menos
uma regressora habilitada**. Se ele não aparecer, confira os quatro pontos:

1. **Colunas mapeadas**: a coluna da regressora precisa estar mapeada no arquivo importado
   (e constar no histórico da série).
2. **Regressora cadastrada e habilitada**: em *Premissas → Regressoras*, cadastre e deixe
   `enabled` — sem isso `cfg.regressor_ids` fica vazio e o método não entra no catálogo.
3. **Histórico + futuro válidos**: é obrigatório ter histórico numérico com variação **e**
   cobertura de todos os períodos futuros do horizonte (ou uma `fill_policy` que a complete).
   Se o futuro estiver ausente, a validação acusa
   `regressor_future_unavailable` / "regressora sem valores futuros" e a rodada não usa o
   método — a mensagem aparece na Etapa 4 antes de executar.
4. **Limites**: máximo de **5 regressoras** por rodada (`MAX_REGRESSORS_PER_RUN`) e sem
   colinearidade perfeita entre elas.

Com os quatro pontos atendidos, `AutoARIMA_X` aparece na lista de métodos da Etapa 4 e
pode ser marcado e executado normalmente.

### Limites

- Máximo de **5 regressoras** por rodada.
- A mesma variável não pode ser declarada simultaneamente como regressora e como multiplicador da mesma família de cenário.

### Variáveis apenas futuras

Eventos sem histórico (ex.: lançamento de produto novo) devem ser modelados como **cenários determinísticos** (pulse/step), não como regressoras — pois sem histórico não há coeficiente a estimar.

---

## 10. Hierarquia Bottom-Up / MinT

### Modos disponíveis

| Modo | Quando usar |
|------|------------|
| **Independente** | Você quer previsões apenas no nível escolhido; não precisa de coerência com outros níveis |
| **Bottom-Up** | Você precisa que SKU → Marca → Total sejam matematicamente coerentes |
| **MinT** | Mesma coerência, saindo da folha; os nós pais são reconciliados e rotulados como `MinT` na saída |

### Como configurar

1. Escolha o modo no formulário de premissas (`Independente`, `Bottom-Up` ou `MinT`).
2. Informe os níveis em ordem do mais agregado ao mais detalhado:
   - Ex.: `Classe Terapêutica → Molécula → Marca → SKU`
3. A aplicação valida que os níveis formam **partições aninhadas** (cada filho pertence a exatamente um pai).
4. Em `Bottom-Up`/`MinT`, o nível a modelar é automaticamente o mais detalhado.

### Regras de coerência

- O modelo é treinado no **nível folha** (mais detalhado).
- Em uma rodada multi-método, **a soma dos pais é feita por método** — nunca
  misturando previsões de métodos diferentes no mesmo nó pai.
- Cenários e premissas são aplicados nas folhas.
- Pais são calculados por **soma das folhas** após os cenários.
- A soma garante: `pai = Σ filhos` para cada período, medida, cenário e método.

### Cobertura parcial

Se uma folha não tiver previsão (falha ou exclusão), o nó pai recebe status `partial_coverage` — **não zero**. A interface lista quais folhas estão ausentes e qual a cobertura percentual.

### Intervalos em nós agregados

Nesta versão, **os limites de intervalo de nós agregados são sempre nulos**, com motivo explícito. Somar intervalos individuais de folhas não produz um intervalo de conjunto válido.

---

## 11. MAT — Acumulado Móvel de 12 Meses

MAT (Moving Annual Total) é o acumulado dos 12 meses encerrados no período t.  
**MAT não é uma quarta frequência aditiva** — posições MAT de períodos diferentes **não devem ser somadas**.

### Dois modos de MAT

#### MAT Derivado (você tem dados mensais)
- A aplicação modela os meses normalmente.
- O MAT é calculado **após a previsão**: `MAT(t) = Σ 12 meses encerrados em t`.
- Nos primeiros MATs futuros, combina meses históricos com meses projetados.
- Só produz MAT quando os 12 componentes mensais existirem.
- Com 60 meses completos → 49 posições MAT históricas completas.

#### MAT Direto (você só tem dados de MAT)
- A aplicação modela diretamente a série de acumulados.
- Identificado como `mat_direto` na saída.
- **Nunca** desagrega em meses nem soma posições MAT ao longo do tempo.
- Premissas nesse modo afetam o nível do acumulado, não simulam o efeito mensal de um evento.

### Card MAT no dashboard

O card mostra a **posição final MAT** e a comparação com a **posição equivalente** no histórico — nunca a soma de posições MAT.

---

## 12. Dashboard e visualização

### Filtros disponíveis

- **O que analisar:** Unidades ou Valor (separadas; nunca somadas)
- **Cenário:** base ou qualquer cenário criado
- **Analisar por:** dimensão em que consolidar (ex.: Classe, Molécula, Marca)
- **Itens:** valores da dimensão com busca textual
- **Método de referência:** qual método guia os **cards e a tabela** (a rodada previu
  todos os métodos escolhidos; você define em qual focar)
- **Métodos do gráfico:** quais métodos desenhar junto ao histórico
- **Layout da tabela/exportação:** longo ou largo

> Alterar um filtro **nunca** re-executa a modelagem — apenas filtra os resultados já persistidos.

### Cards de resumo

| Card | Conteúdo |
|------|---------|
| **Itens na análise** | Quantidade de itens da dimensão no recorte |
| **Total previsto** | Soma das previsões do **método de referência** (posição final em MAT) |
| **Variação vs. histórico** | Crescimento em relação à janela histórica de igual duração |
| **WAPE do método de referência** | Erro percentual médio no backtest das séries do recorte para o método selecionado |

> Se não existir janela histórica comparável, a variação aparece como `N/D`.
> Aviso obrigatório para horizonte > 24 meses equivalentes.

### Comparar métodos

A seção **"Comparar métodos"** agrega o backtest por método (MAE/RMSE/WAPE/Bias médios
e nº de séries), ordenando pelo menor MAE. É uma sugestão visual — a aplicação
**não escolhe por você**; use as métricas para decidir qual método é mais adequado ao
negócio.

### Gráfico de previsão

- Histórico observado (linha contínua) por item da dimensão
- Previsão do **método de referência** (linha contínua colorida) por item
- **Overlays** dos demais métodos selecionados (linhas tracejadas), lidos das
  previsões persistidas
- Faixa de intervalo (`lo80` / `hi80`) — **somente quando disponível**

### Tabelas de diagnóstico

- **Métricas por série e método:** MAE, RMSE, WAPE, bias, `eligible`, `selected` por série
- **Backtest por método (agregado):** métricas agregadas por método no recorte
- **Regras aplicadas:** rastro de premissas por série/período (na exportação XLSX)
- **Problemas da rodada:** erros e avisos da execução

---

## 13. Exportação de resultados

### Opções de configuração

| Parâmetro | Opções |
|-----------|--------|
| **Formato** | CSV ou XLSX |
| **Layout** | Longo (uma linha por período) ou Largo (colunas por data) |
| **Cenário** | Selecionar qual cenário exportar |
| **Medida** | Uma medida por vez ou todas (no longo) |
| **Nível** | Qual nível da hierarquia exportar |
| **Vista temporal** | Canônica (datas reais) ou MAT (posições de acumulado) |
| **Intervalos** | Incluir `lo80` / `hi80` quando disponíveis |
| **Arredondamento de Unidades** | Opcional; folhas arredondadas antes de recalcular pais |

### Conteúdo do CSV longo (canônico)

Colunas: `dimensões`, `node_id`, `nível`, `entity_id`, `medida`, `cenário`, `período`,
`tipo` (historico/previsao), `valor`, `yhat_base`, `observado`, `ajustado`, `lo80`,
`hi80`, `modelo`, `interval_method`, `status`, `preparation_id`, `run_id`, `dataset_id`.

### Conteúdo do XLSX

| Aba | Conteúdo |
|-----|---------|
| `Previsoes` | Previsões com dimensões, cenário, datas e intervalos |
| `Historico` | Histórico observado e ajustado por entidade |
| `Metricas` | MAE, RMSE, WAPE, bias por série/modelo |
| `Premissas` | Regras de cenário aplicadas |
| `Qualidade` | Diagnóstico de qualidade, exclusões e outliers |
| `Metadados` | Configuração da rodada, versões, datas e hash de configuração |

### Limites de exportação

- Abas XLSX são divididas automaticamente em múltiplas abas se ultrapassarem **1.048.576 linhas**.
- Para volumes muito grandes (ex.: 5.000 entidades × 120 meses × 2 medidas = 1.200.000 linhas de previsão), prefira CSV.
- EAN e identificadores são sempre exportados como **texto**.
- Downloads separados de métricas (`export_metrics_csv`) e qualidade (`export_quality_csv`) estão disponíveis na interface.

---

## 14. Diagnóstico de qualidade

### Intermitência (ADI e CV²)

| Métrica | Fórmula | Significado |
|---------|---------|-------------|
| **ADI** | Nº períodos / Nº períodos com demanda > 0 | ≥ 1,32 → habilita candidatos intermitentes |
| **CV²** | (DP dos valores positivos / média dos valores positivos)² | ≥ 0,49 → indicador de alta variabilidade |

ADI e CV² são calculados apenas para séries com ao menos um valor positivo.

### Detecção de outliers

Método: mediana/MAD sobre resíduos sazonais (quando houver ≥ 2 ciclos); caso contrário, sobre a série.

- Marcado como outlier: desvio absoluto > `3 × 1,4826 × MAD`.
- MAD zero não dispara classificação automática.
- Tratamento disponível: **winsoriziar** (limitar a cauda superior pelo quantil configurado, padrão 99%).
- Zeros e valores negativos aprovados **não** são suavizados automaticamente.

### Séries com histórico conflitante

Se um atributo (ex.: Molécula de uma Marca) muda ao longo do tempo, a aplicação detecta o conflito e pede:
- Mapeamento estático confirmado (reclassifica todo o histórico), **ou**
- Chave que diferencie os registros

Nenhuma escolha automática é feita.

---

## 15. Regras e limites do produto

| Regra | Detalhe |
|-------|---------|
| **Piso zero obrigatório** | Toda projeção finita ≥ 0; `NaN`/`inf` continuam sendo falhas |
| **Contagem de yhat limitados a zero** | Registrada por rodada para rastreabilidade |
| **Histórico máximo** | 60 meses / 20 trimestres / 5 anos |
| **Horizonte máximo** | 120 meses / 40 trimestres / 10 anos (com aviso acima de 24 meses equivalentes) |
| **Entidades máximas** | 5.000 por rodada (contadas antes de expandir medidas) |
| **Medidas** | Unidades e Valor sempre independentes; nunca somadas entre si |
| **Regressoras** | Máximo 5 por rodada |
| **Lote máximo** | 500 entidades |
| **Arquivo** | 100 MiB / 1.000.000 linhas físicas / 600.000 observações canônicas |
| **EAN e identificadores** | Sempre texto; zeros à esquerda preservados |
| **Uma moeda por estudo** | Não converte entre moedas |
| **Uma instância por banco** | Dois processos não disputam o mesmo `.duckdb` |

---

## 16. Solução de problemas

### "A aplicação não abre"

```bash
# Verifique se o ambiente virtual está ativo
.venv\Scripts\activate  # Windows

# Verifique a instalação
pip list | findstr streamlit

# Execute novamente
python -m streamlit run app.py
```

### "Erro de banco ocupado"

Feche qualquer outra instância da aplicação. O DuckDB permite apenas um escritor por vez. Se precisar de instâncias paralelas, configure caminhos de banco diferentes.

### "Rodada marcada como interrupted"

Ao reabrir a aplicação após uma queda, rodadas `running` são marcadas como `interrupted`. Os resultados parciais ficam acessíveis para inspeção. Para re-executar, crie uma nova rodada (a anterior é preservada).

### "Modelo LightGBM / XGBoost não aparece como candidato"

Verifique se os pacotes ML foram instalados no ambiente ativo:

```bash
pip list | findstr mlforecast
pip list | findstr lightgbm
pip list | findstr xgboost
```

Se ausente: `pip install -r requirements.txt`. Lembre também de marcar o checkbox
**"Habilitar aprendizado global"** na Etapa 4 — e lembre que o modelo global exige
**≥ 20 entidades** e **≥ 200 linhas treináveis** na medida; sem isso, ele não entra no
catálogo mesmo instalado.

### "Unidades fracionárias bloqueiam a importação"

Unidades de origem devem ser inteiras. Verifique se a coluna de unidades contém valores como `1.5` e corrija no arquivo de origem. Previsões de Unidades permanecem decimais (representam expectativas).

### "EAN aparece sem zeros à esquerda"

Se o XLSX já perdeu os zeros por armazenamento numérico, a aplicação informa a perda mas não reconstrói os dígitos. Exporte o arquivo original como CSV com a coluna EAN formatada como texto antes de importar.

### "Série sem previsão — status insufficient_history"

A série tem histórico curto para realizar a validação cruzada (< 3 observações no primeiro treino). O modelo `Naive` é aplicado como baseline. Se possível, amplie o período histórico.

### "Cenário bloqueado — E_SCENARIO_PRIORITY_TIE"

Duas regras da **mesma família** com a **mesma prioridade** se sobrepõem na mesma entidade/medida/período. Resolva alterando a prioridade de uma das regras ou separando em famílias distintas.

### "Execução muito lenta"

- Selecione **menos métodos** no multiseletor da Etapa 4 (cada método marcado é executado).
- Desative o modo avançado (desmarca ARIMA, CES e TBATS).
- Aumente "Threads" para 2 ou 4 se a máquina suportar (em Windows evite se travar).
- Primeiro uso após instalar MLForecast compila o Numba (pode demorar alguns minutos). Execuções seguintes são mais rápidas.
- **Reduza o número de folds da validação temporal** quando usar `AutoARIMA` e/ou
  `AutoTBATS`: esses métodos reestimam o modelo em cada fold, e o custo cresce de forma
  aproximadamente linear. Em medição com 50 entidades do `N05A.xlsx` (60 meses, h=60,
  `n_jobs=1`), somente `AutoARIMA`: **1 fold ≈ 30s**, 2 folds ≈ 64s, 3 folds ≈ 88s —
  sem ganho monotônico de MAE/WAPE nessa amostra. Rodadas exploratórias podem usar
  1 fold e deixar 2–3 folds para a rodada final.
- A rodada **não grava nada no banco até o fim**: o progresso aparece no log da Etapa 4
  ("X de N séries"), mas os resultados só são persistidos na conclusão. Se o tempo
  estimado passar de alguns minutos, reduza métodos e/ou folds antes de executar.

---

## 17. Glossário

| Termo | Definição |
|-------|-----------|
| **Entidade** | Combinação única de valores das colunas de chave (ex.: Marca A + EAN 001) |
| **Série** | Par entidade + medida; é a unidade de modelagem |
| **node_id** | Identificador único de uma entidade ou nó agregado na hierarquia |
| **entity_id** | Hash SHA-256 da chave canônica da entidade |
| **series_id** | Hash SHA-256 de `(entity_id, measure)` |
| **dataset_id** | Identificador UUID do arquivo importado + mapeamento |
| **preparation_id** | Identificador UUID da preparação (dataset + política de tratamento) |
| **run_id** | Identificador UUID de uma rodada de previsão |
| **config_hash** | Hash SHA-256 da configuração completa da rodada (reprodutibilidade) |
| **MAT** | Moving Annual Total — acumulado dos 12 meses encerrados em t |
| **ADI** | Average Demand Interval — métrica de intermitência |
| **CV²** | Coeficiente de variação quadrado — métrica de variabilidade |
| **MAD** | Desvio absoluto mediano — robusto para detecção de outliers |
| **MAE** | Erro absoluto médio — métrica de diagnóstico de aderência (a aplicação não elege vencedor) |
| **WAPE** | Weighted Absolute Percentage Error — percentual ponderado |
| **yhat** | Previsão pontual do modelo (não necessariamente a mediana) |
| **lo80 / hi80** | Limites inferior e superior do intervalo de previsão de 80% |
| **Backtest** | Avaliação histórica do método usando validação cruzada temporal |
| **Fold** | Uma janela de treino/teste na validação cruzada |
| **Método de referência** | Método escolhido pelo usuário para guiar cards e tabela no dashboard (a aplicação não escolhe por ele) |
| **Fallback** | Modelo alternativo (Naive) usado quando um método solicitado falha na projeção |
| **Bottom-Up** | Método de reconciliação hierárquica: previsões de folhas somadas para os pais, por método |
| **MinT** | Modo de reconciliação a partir das folhas; nós pais coerentes, rotulados como MinT |
| **Pulse** | Efeito de cenário aplicado apenas em um intervalo de tempo |
| **Step** | Efeito de cenário persistente a partir de uma data |
| **Annual Step** | Efeito composto anualmente (ex.: reajuste de preço anual) |
| **Winsorização** | Limitação de valores extremos ao quantil configurado |
| **ffill** | Forward fill — preencher lacunas com o último valor observado |
| **DuckDB** | Banco de dados analítico embutido usado para persistência local |
| **Conformal Prediction** | Método para estimar intervalos de cobertura garantida em séries temporais |
