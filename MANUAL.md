# MANUAL DE USO — Forecast Community

> Guia completo para o usuário. Versão correspondente ao estado do projeto em 11/09/2026.

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
7. [Validação temporal e seleção de modelos](#7-validação-temporal-e-seleção-de-modelos)
8. [Premissas e cenários](#8-premissas-e-cenários)
9. [Regressoras exógenas](#9-regressoras-exógenas)
10. [Hierarquia Bottom-Up](#10-hierarquia-bottom-up)
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
- Comparar automaticamente múltiplos modelos de previsão por série
- Aplicar premissas de negócio (reajustes, campanhas, CMED) como cenários determinísticos
- Usar variáveis exógenas (regressoras) como entrada adicional em modelos selecionados
- Reconciliar previsões em hierarquias Bottom-Up (ex.: SKU → Marca → Molécula → Total)
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

# 4. Instale as dependências do núcleo
pip install -r requirements.txt
```

### Instalar o modo avançado ML (opcional)

O complemento ML habilita o modelo LightGBM global. Só instale se quiser usar esse candidato.

```bash
pip install -r requirements-ml.txt
```

### Instalar dependências de desenvolvimento

Necessário apenas para executar os testes automatizados.

```bash
pip install -r requirements-dev.txt
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

Séries sem histórico suficiente para validação cruzada recebem o modelo `Naive` como baseline, com status `insufficient_history`. Elas entram na rodada mas não competem na seleção automática.

---

### Etapa 4 — Previsão

Esta etapa reúne a configuração de premissas/modelos e a execução da rodada.

**Premissas e cenários (opcional):** fica num expander recolhido por padrão —
cadastre cenários e regressoras só se precisar de ajustes incrementais sobre a
previsão base; cada cenário pode ser excluído com o botão "Excluir cenário".
As premissas não são regravadas a cada interação, só quando algo muda de fato.

**Horizonte e Modo** ficam sempre visíveis (reagem na hora: o aviso de horizonte
> 24 períodos aparece assim que você digita o valor). Os demais parâmetros
ficam em **"Opções avançadas"**:

| Parâmetro | Descrição | Padrão |
|-----------|-----------|--------|
| **Horizonte** | Número de períodos futuros a prever | 12 |
| **Modo** | Rápido (ETS, Theta, Croston) ou Avançado (+CES, ARIMA, LightGBM) | Rápido |
| **Candidatos** | Quais modelos concorrem (configurável) | Automático por perfil |
| **ML Global** | Habilitar LightGBM (requer `requirements-ml.txt`) | Desabilitado |
| **Janelas de CV** | Quantos folds de validação cruzada (máx. 3) | 3 |
| **Tamanho do lote** | Entidades processadas por lote (máx. 500) | 250 |
| **Jobs** | Paralelismo (máx. 4 ou CPUs disponíveis) | 1 |
| **Hierarquia** | Independente ou Bottom-Up — desabilita "Nível a modelar" imediatamente ao escolher Bottom-Up | Independente |

#### Hierarquia

| Modo | Comportamento |
|------|--------------|
| **Independente** | Modela cada entidade no nível escolhido; sem coerência entre níveis |
| **Bottom-Up** | Modela o nível mais detalhado e soma os pais; coerência matemática garantida |

#### Executar a rodada

Clique em **"▶ Executar previsão"** para iniciar. A aplicação:

1. Valida a configuração e verifica conflitos de premissas.
2. Executa CV e seleção de modelos em lotes de 250 entidades.
3. Realiza o refit final do vencedor com todo o histórico elegível.
4. Aplica cenários e premissas determinísticas.
5. Reconcilia Bottom-Up (se configurado).
6. Persiste resultados por lote (idempotente — pode ser chamado duas vezes sem duplicar).

**Progresso:** a UI exibe o estágio atual (`cv`, `forecast`, `scenario`, `reconcile`) e avança sozinha de lote em lote (sem cliques intermediários); o botão **Cancelar** fica visível durante todo o processamento. Uma rodada cancelada preserva os resultados parciais para inspeção. Ao concluir, clique em **"Ver resultados →"** para ir à etapa Resultados.

**Status de rodada:**
- `running` — em andamento
- `completed` — todas as séries elegíveis processadas
- `partial` — alguma série sem saída (fallback não pôde ser aplicado)
- `cancelled` — cancelado pelo usuário entre lotes
- `failed` — erro crítico
- `interrupted` — aplicação fechou durante a execução

---

### Etapa 5 — Resultados

Veja os resultados na aba de dashboard e exporte em CSV ou XLSX com **um clique** — os botões de download já vêm prontos, sem precisar gerar o arquivo antes.

Filtros disponíveis:
- Medida (Unidades / Valor)
- Cenário (base ou qualquer cenário criado)
- Nível / nó hierárquico
- Dimensões com busca textual

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

Todos os modelos são do pacote **StatsForecast** (Nixtla), que usa implementações otimizadas (Numba onde aplicável).

### Baselines — sempre disponíveis

| Modelo | Descrição | Quando usar |
|--------|-----------|-------------|
| `Naive` | Repete o último valor observado | Série estável; fallback universal |
| `HistoricAverage` | Média histórica | Série sem tendência nem sazonalidade |
| `SeasonalNaive` | Repete o valor do mesmo período do ano anterior | Série com sazonalidade clara e sem tendência |
| `ZeroBaseline` | Previsão zero | Série com histórico todo zero |

### Modo Rápido

| Modelo | Descrição | Mínimo de observações |
|--------|-----------|:---:|
| `AutoETS` | Suavização exponencial com seleção automática de componentes (erro, tendência, sazonalidade) | 8 obs. no menor fold |
| `AutoTheta` | Método Theta com seleção automática | 8 obs. no menor fold |
| `CrostonSBA` | Croston com ajuste de Syntetos-Boylan (demanda intermitente) | ADI ≥ 1,32 e ≥ 2 valores positivos |
| `TSB` | Teunter-Syntetos-Babai (demanda intermitente) | Mesma elegibilidade do CrostonSBA |

### Modo Avançado (adicional ao Rápido)

| Modelo | Descrição | Mínimo de observações |
|--------|-----------|:---:|
| `AutoCES` | Complex Exponential Smoothing | 8 obs. no menor fold |
| `AutoARIMA` | ARIMA com seleção automática (max_p/q=3, max_P/Q=1) | 8 obs. no menor fold |
| `AutoARIMA_X` | AutoARIMA com variáveis exógenas | Mesmo do AutoARIMA + regressoras válidas |

### Modelo Global ML (opcional)

| Modelo | Descrição | Requisitos |
|--------|-----------|-----------|
| `LightGBM` (via MLForecast) | Gradient Boosting global — um modelo treinado em todas as entidades da mesma medida | ≥ 20 entidades, ≥ 200 linhas treináveis após lags, `requirements-ml.txt` instalado |

**Features do LightGBM:**
- Lags [1, 2, 3] e lag sazonal (quando elegível)
- Média móvel causal de 3 observações (com shift de 1 período)
- Mês/trimestre como variável de calendário
- Dimensões estáticas categóricas mapeadas por fold

**Importante:** o modelo global é treinado uma vez sobre todas as entidades elegíveis por medida — não separadamente por lote. Lotes controlam apenas a predição e a persistência.

---

## 7. Validação temporal e seleção de modelos

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

O maior número de folds K ≤ 3 para o qual o primeiro treino tem pelo menos **3 observações** é usado. Com K < 2, aplica-se o baseline e a série é marcada como `insufficient_history`.

### Métricas calculadas

| Métrica | Fórmula | Uso |
|---------|---------|-----|
| **MAE** | `média(|yhat − y|)` | Critério primário de seleção |
| **RMSE** | `√(média((yhat − y)²))` | Diagnóstico e desempate |
| **WAPE** | `Σ|yhat − y| / Σ|y|` | Percentual ponderado; nulo se denominador zero |
| **Bias** | `média(yhat − y)` | Positivo = superestimação |

> MAPE **não** é usado como critério padrão devido à instabilidade com zeros.

### Regra de seleção

1. Vence o modelo com **menor MAE**.
2. Em empate (diferença ≤ 1%), vence o **modelo mais simples** pela ordem fixa:
   `ZeroBaseline → Naive → HistoricAverage → SeasonalNaive → CrostonSBA → TSB → AutoETS → AutoTheta → AutoCES → AutoARIMA → LightGBM`
3. O vencedor é **reajustado** com todo o histórico elegível para gerar a previsão final.
4. Se o reajuste final falhar, tenta o próximo candidato; depois `Naive`; se nenhum funcionar, a série falha.

### Intervalos de previsão

A aplicação tenta produzir intervalos de **80%** (`lo80` / `hi80`):

- **Intervalo nativo:** quando o modelo suporta diretamente.
- **Predição conformal:** para modelos apenas pontuais, quando houver histórico suficiente (`≥ 2 × horizonte + treino mínimo`).
- **Nulo com motivo explícito:** quando não for viável — campo `interval_method=unavailable_insufficient_history`.

> Os intervalos de nós agregados (Bottom-Up) e MAT derivado são **sempre nulos** nesta versão, com motivo explicado na interface.

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

### Limites

- Máximo de **5 regressoras** por rodada.
- A mesma variável não pode ser declarada simultaneamente como regressora e como multiplicador da mesma família de cenário.

### Variáveis apenas futuras

Eventos sem histórico (ex.: lançamento de produto novo) devem ser modelados como **cenários determinísticos** (pulse/step), não como regressoras — pois sem histórico não há coeficiente a estimar.

---

## 10. Hierarquia Bottom-Up

### Modos disponíveis

| Modo | Quando usar |
|------|------------|
| **Independente** | Você quer previsões apenas no nível escolhido; não precisa de coerência com outros níveis |
| **Bottom-Up** | Você precisa que SKU → Marca → Total sejam matematicamente coerentes |

### Como configurar

1. Escolha `Bottom-Up` no formulário de premissas.
2. Informe os níveis em ordem do mais agregado ao mais detalhado:
   - Ex.: `Classe Terapêutica → Molécula → Marca → SKU`
3. A aplicação valida que os níveis formam **partições aninhadas** (cada filho pertence a exatamente um pai).

### Regras de coerência

- O modelo é treinado no **nível folha** (mais detalhado).
- Cenários e premissas são aplicados nas folhas.
- Pais são calculados por **soma das folhas** após os cenários.
- A soma garante: `pai = Σ filhos` para cada período, medida e cenário.

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

- **Medida:** Unidades ou Valor (separadas; nunca somadas)
- **Cenário:** base ou qualquer cenário criado
- **Nível/nó:** nó específico da hierarquia
- **Dimensões:** filtro por valor de qualquer dimensão (com busca textual)

> Alterar um filtro **nunca** re-executa a modelagem — apenas filtra os resultados já persistidos.

### Cards de resumo

| Card | Conteúdo |
|------|---------|
| **Entidades previstas** | Quantidade com previsão completa / total elegível |
| **Cobertura** | % do histórico coberto pelas séries previstas |
| **Total do horizonte** | Soma das previsões no período selecionado (medidas aditivas) |
| **Variação vs. histórico** | Crescimento em relação à janela histórica de igual duração |
| **MAE do vencedor** | Erro médio absoluto no backtest (com horizonte avaliado explícito) |

> Se não existir janela histórica comparável, a variação aparece como `N/D`.  
> Aviso obrigatório para horizonte > 24 meses equivalentes.

### Gráfico de previsão

- Histórico observado (linha contínua)
- Histórico ajustado opcional (linha tracejada, quando houver tratamentos)
- Previsão base (linha colorida)
- Cenário selecionado (linha com cor distinta)
- Faixa de intervalo (`lo80` / `hi80`) — **somente quando disponível**

### Tabelas de diagnóstico

- **Modelos por série:** vencedor, MAE, RMSE, WAPE, bias, fallback usado
- **Premissas aplicadas:** regras, famílias, fatores e vigência
- **Qualidade:** exclusões, lacunas, outliers tratados
- **Falhas:** séries sem previsão e motivo

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

Colunas: `dimensões`, `node_id`, `nível`, `medida`, `cenário`, `período`, `tipo` (historico/previsao), `valor`, `yhat_base`, `lo80`, `hi80`, `modelo`, `status`, `preparation_id`, `run_id`, `dataset_id`.

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

### "Modelo LightGBM não aparece como candidato"

Verifique se `requirements-ml.txt` foi instalado no ambiente ativo:

```bash
pip list | findstr mlforecast
pip list | findstr lightgbm
```

Se ausente: `pip install -r requirements-ml.txt`.

### "Unidades fracionárias bloqueiam a importação"

Unidades de origem devem ser inteiras. Verifique se a coluna de unidades contém valores como `1.5` e corrija no arquivo de origem. Previsões de Unidades permanecem decimais (representam expectativas).

### "EAN aparece sem zeros à esquerda"

Se o XLSX já perdeu os zeros por armazenamento numérico, a aplicação informa a perda mas não reconstrói os dígitos. Exporte o arquivo original como CSV com a coluna EAN formatada como texto antes de importar.

### "Série sem previsão — status insufficient_history"

A série tem histórico curto para realizar a validação cruzada (< 3 observações no primeiro treino). O modelo `Naive` é aplicado como baseline. Se possível, amplie o período histórico.

### "Cenário bloqueado — E_SCENARIO_PRIORITY_TIE"

Duas regras da **mesma família** com a **mesma prioridade** se sobrepõem na mesma entidade/medida/período. Resolva alterando a prioridade de uma das regras ou separando em famílias distintas.

### "Execução muito lenta"

- Tente aumentar `n_jobs` para 2 ou 4 (se disponível na sua máquina).
- Reduza o número de candidatos desativando o modo avançado.
- Primeiro uso após instalar MLForecast compila o Numba (pode demorar alguns minutos). Execuções seguintes são mais rápidas.

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
| **MAE** | Erro absoluto médio — critério primário de seleção de modelos |
| **WAPE** | Weighted Absolute Percentage Error — percentual ponderado |
| **yhat** | Previsão pontual do modelo (não necessariamente a mediana) |
| **lo80 / hi80** | Limites inferior e superior do intervalo de previsão de 80% |
| **Backtest** | Avaliação histórica do modelo usando validação cruzada temporal |
| **Fold** | Uma janela de treino/teste na validação cruzada |
| **Fallback** | Modelo alternativo usado quando o vencedor falha no refit final |
| **Bottom-Up** | Método de reconciliação hierárquica: previsões de folhas somadas para os pais |
| **Pulse** | Efeito de cenário aplicado apenas em um intervalo de tempo |
| **Step** | Efeito de cenário persistente a partir de uma data |
| **Annual Step** | Efeito composto anualmente (ex.: reajuste de preço anual) |
| **Winsorização** | Limitação de valores extremos ao quantil configurado |
| **ffill** | Forward fill — preencher lacunas com o último valor observado |
| **DuckDB** | Banco de dados analítico embutido usado para persistência local |
| **Conformal Prediction** | Método para estimar intervalos de cobertura garantida em séries temporais |
