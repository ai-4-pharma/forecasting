# Forecast Community

Ferramenta **local** de forecasting para analistas de BI, SFE e dados. Configure
estudos, importe CSV/XLSX (formatos longo e largo), faça diagnóstico de qualidade,
valide modelos com validação temporal, aplique premissas e cenários determinísticos,
reconcilie hierarquias (Bottom-Up / MinT) e exporte resultados em CSV/XLSX.

![Tela única: gráfico da projeção, cards e grid](docs/img/04-projecao.png)

Há duas formas de usar:

- **Tela única (caminho principal)** — abra o arquivo, clique em **Gerar**, navegue pela
  árvore de itens, compare métodos e **exporte para Excel**. Servida por uma API local
  (FastAPI) + página HTML, sem configurar nada antes de ver o primeiro número.
- **Laboratório (Streamlit)** — o assistente de 5 etapas com mapeamento manual,
  qualidade, cenários, regressoras e hierarquia MinT. Continua disponível para estudos
  avançados (ver [Jornada do usuário](#jornada-do-usuário)).

**Escolha de método.** Na tela única, se você marcar métodos (até 5), a rodada executa
**exatamente** esses. Se não marcar nenhum, o sistema **recomenda o melhor método por
item** (menor WAPE em 1 fold de validação) e mostra qual foi e quais foram as
alternativas — nada é escondido, e você pode comparar todos os que rodaram. No
Laboratório, a rodada sempre executa exatamente os métodos que você marcar.

**Os dados permanecem no seu computador.** Não há serviço remoto obrigatório,
API de IA, autenticação nem banco servidor — apenas Python rodando localmente
(o servidor web escuta só em `127.0.0.1`).

---

## Início rápido (Windows)

Pré-requisito: [Python 3.12+](https://www.python.org/downloads/) instalado (marque "Add python.exe to PATH" no instalador).

1. Dê **dois cliques** em [`instalar.bat`](instalar.bat) (só na primeira vez) — cria o ambiente virtual e instala as dependências.
2. **Tela única:** no terminal, dentro da pasta do projeto:
   ```bash
   .venv\Scripts\python.exe -m api
   ```
   O navegador abre em `http://127.0.0.1:8765`.
3. **Laboratório (Streamlit):** dois cliques em [`iniciar.bat`](iniciar.bat) — abre em `http://localhost:8501`.

> `iniciar.bat` abre o **Laboratório (Streamlit)**, não a tela única. Não há atalho
> `.bat` para a tela única nesta versão.

### Uso rápido da tela única (3 passos)

1. **Abrir arquivo** (`.xlsx` ou `.csv` no formato largo: colunas de texto = dimensões,
   colunas `YYYYMM` = períodos). O formato é detectado sozinho.
2. (Opcional) ajuste o **Horizonte** (padrão 12) e marque os **Métodos** a comparar; depois **Gerar**.
3. Navegue pela árvore, veja gráfico/KPIs/grid e clique em **Exportar Excel**.

Detalhes no [MANUAL.md](MANUAL.md#31-tela-única-uso-rápido).

---

## Guia visual da tela única

Os prints abaixo usam o arquivo de exemplo `N05A.xlsx` (523 séries × 60 meses). Para
reproduzir, siga a ordem das seções.

### 1. Tela inicial

![Tela inicial vazia](docs/img/01-inicio.png)

Ao abrir `http://127.0.0.1:8765` a tela está limpa. Tudo o que você controla fica na **barra
lateral esquerda**; o **gráfico e o grid** ficam no centro; os **cards** ficam à direita.
O botão de tema (**Escuro** / **Claro**) e o status da rodada ficam no canto superior direito.

### 2. Abrir arquivo, horizonte e métodos

![Painel flutuante de métodos](docs/img/02-metodos.png)

1. Clique em **Abrir arquivo** e escolha o `.xlsx` ou `.csv`. A barra lateral mostra o nome do
   arquivo, o número de séries e períodos e um aviso de qualidade (ex.: séries com muitos zeros).
2. Ajuste o **Horizonte** (meses a projetar; padrão 12).
3. Clique em **Métodos** para abrir o painel flutuante. Marque **até 5** métodos para comparar.
   O ⏱ indica métodos lentos em bases grandes (passe o mouse para ver o aviso). **Sem marcar
   nenhum**, o botão fica em *Métodos (automático)* e o sistema recomenda o melhor método
   por item.
4. Clique em **Gerar**. Uma barra de progresso acompanha a rodada.

### 3. Dar nome ao estudo

![Janela para nomear o estudo](docs/img/03-nome-estudo.png)

Quando a rodada termina, a tela pede um **nome** para o estudo (já vem sugerido: arquivo +
data). **Enter** ou **Salvar** grava; **Agora não** deixa o estudo sem nome. Os valores
projetados já estão gravados no banco local, então nomear serve para reabrir depois. O cartão
no topo da barra lateral mostra nome, **ID** e **data/hora** do estudo, com o botão
**Renomear estudo**.

### 4. Navegar pelos resultados

![Projeção do item, cards e grid](docs/img/04-projecao.png)

- **Filtros (barra lateral, embaixo):** *Buscar* filtra a árvore por texto. Abaixo, um seletor
  por nível da hierarquia (aqui: Classe → Molécula → Produto), **em cascata**: ao escolher uma
  classe, o nível seguinte mostra só o que pertence a ela. Use **Ctrl/Shift + clique** para
  marcar vários. Os itens marcados viram *chips* no fim da lista; clique no chip para removê-lo. O chip em
  destaque (azul-escuro) é o último item marcado e alimenta o gráfico, os cards e o grid.
- **Aba "Projeção do item":** histórico (cinza), projeção (azul/laranja tracejada) e faixa de
  80% de confiança. Passe o mouse sobre o gráfico para ver os valores.
- **Cards da direita:** *Último MAT* (soma dos últimos 12 meses), *Variação MAT (YoY)*, *CAGR do
  MAT* (só com 36+ meses), *Total projetado no horizonte*, *Erro do método (WAPE)* e *Viés
  (Bias)* do backtest, mais as *estatísticas do histórico* (máxima, mínima, desvio padrão e erro
  padrão da média). Cada card traz a explicação embaixo do número.
- **Grid (embaixo):** os valores projetados mês a mês.

### 5. Comparar métodos

![Aba Comparar métodos](docs/img/05-comparar-metodos.png)

A aba **Comparar métodos** desenha **uma curva por método** rodado para o item selecionado. A
legenda traz o **WAPE de backtest** de cada método (menor = melhor). Nada é escondido: você
vê todos os métodos que rodaram e decide.

### 6. Comparar itens

![Aba Comparar itens](docs/img/06-comparar-itens.png)

Marque vários itens nos filtros e abra **Comparar itens**: uma linha por item (histórico
contínuo, projeção tracejada), todas com o **mesmo método**, escolhido no seletor
*Método* no canto superior direito do gráfico. Os cards e o grid seguem o item em destaque
(o chip em azul-escuro).

### 7. Estudos: abrir e excluir

![Lista de estudos salvos](docs/img/07-abrir-estudo.png)

- **Novo estudo** limpa a tela para uma nova projeção (os estudos anteriores continuam no banco).
- **Abrir estudo** lista os estudos rodados (nome, arquivo, data/hora, horizonte, séries,
  métodos e ID). Clique numa linha para reabrir: árvore, gráficos, cards e **Exportar Excel**
  voltam como estavam. Rodadas sem nome ficam ocultas; marque **Mostrar rodadas sem nome**
  para vê-las.
- **Excluir** (por linha) abre uma confirmação:

![Confirmação de exclusão](docs/img/08-excluir-estudo.png)

A exclusão apaga do banco local as projeções, o backtest e as métricas daquele estudo (o
arquivo importado e os outros estudos não são afetados) e **não pode ser desfeita**.

### 8. Exportar para Excel

Com um estudo aberto, **Exportar Excel** baixa `forecast_<id>.xlsx`: aba `Historico`, uma aba
por método rodado e `Metadados` (detalhes em [Exportação](#exportação-tela-única)).

### 9. Tema escuro

![Tema escuro](docs/img/09-tema-escuro.png)

O botão **Escuro** / **Claro** no topo alterna o tema. Sem escolha manual, a tela segue a
preferência do sistema operacional.

---

## Status de implementação

| Fase | Descrição | Estado |
|------|-----------|--------|
| A | Base, contratos, ingestão e qualidade (P01–P14) | ✅ Concluída — Gate A verde (8/8 casos) |
| B | Motor estatístico e validação temporal (P15–P20) | ✅ Concluída — Gate B verde (7/7 casos) |
| C | Premissas, cenários e hierarquia (P21–P27) | ✅ Concluída — Gate C verde (8/8 casos) |
| D | Aprendizado global opcional (P28–P30) | ✅ Concluída — Gate D verde (3/3 casos) |
| E | Dashboard e exportação completa (P31–P35) | ✅ Concluída — Gate E parcial (E04/E03 cobertos; E01/E02/E05/E06/E07 pendentes) |
| F | Gate E final e benchmark (P36) | 🔄 Próximo passo |
| G | Documentação e encerramento (P37–P38) | 🔄 Em andamento |

**Nota (16/09/2026):** a eleição automática de "modelo vencedor" foi removida
por decisão do usuário — a rodada executa exatamente os métodos escolhidos na
Etapa 4, e o backtest passa a ser **por método**.

**Nota (18/09/2026):** novo norte fechado — tela única em FastAPI + HTML/JS, motor mais
rápido (previsão final em painel, persistência em lote), defaults h=12 / 1 fold / ML
opcional e exportação Excel com histórico + uma aba por modelo. O wizard de 5 etapas
virou o "Laboratório". Ficaram **fora** desta versão: ajuste manual de meses no grid
(overrides), congelar rodada, painel de exceções e `laboratorio.bat`. Registro completo
em `20260918_ajustes.md` (seção 10).

**Nota (17/09/2026):** os artefatos de desenvolvimento (suíte de testes, mocks,
benchmarks e sondas) foram movidos para `dev/`, fora do produto entregue e do
versionamento (`dev/` está no `.gitignore`). Para rodá-los:

```bash
pip install -r dev/requirements-dev.txt
python -m pytest dev/ -q
```

---

## Instalação

**Python 3.12** como versão de referência (testado também com **3.13.1** no ambiente de desenvolvimento).

```bash
# 1. Crie e ative o ambiente virtual
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate

# 2. Instale as dependências do núcleo
pip install -r requirements.txt

# 3. (opcional) Aprendizado global — LightGBM/XGBoost via MLForecast
pip install -r requirements-ml.txt
```

No Windows, basta dar dois cliques em `instalar.bat`: ele cria o `.venv` e instala
`requirements.txt` (núcleo). O aprendizado global é opcional (ver tabela abaixo).

| Arquivo | Conteúdo | Quando instalar |
|---|---|---|
| `requirements.txt` | Núcleo: `polars`, `statsforecast`, `duckdb`, `streamlit`, `openpyxl`, `plotly`, `pandas`, `pyarrow`, `fastapi`, `uvicorn`, `python-multipart` | Sempre — é o mínimo para rodar a tela única, o Laboratório e o motor estatístico (Naive/SeasonalNaive/AutoETS/AutoTheta/CrostonSBA/TSB/...) |
| `requirements-ml.txt` | `mlforecast`, `lightgbm`, `xgboost` | Só se for usar os candidatos `LightGBM`/`XGBoost` (na tela única aparecem no painel "Métodos"; no Laboratório, checkbox "Habilitar aprendizado global"). **ML é opcional**: sem este arquivo, `_ml_available()` retorna falso e o restante do produto funciona normalmente. |

### Dependências de desenvolvimento e testes

```bash
pip install -r dev/requirements-dev.txt
```

---

## Execução

**Tela única (principal):**

```bash
python -m api
```

Abre `http://127.0.0.1:8765` no navegador. Alternativa sem abrir o navegador:
`python -m uvicorn api.main:app --port 8765`. A documentação interativa dos
endpoints fica em `http://127.0.0.1:8765/docs`.

**Laboratório (Streamlit):**

```bash
python -m streamlit run app.py
```

Abre em `http://localhost:8501`. Nenhum dado, chave de API ou conexão com a nuvem é necessário para iniciar.

Os dados de trabalho ficam em `.local/forecast.duckdb` (nunca versionado), compartilhado
pelos dois modos. **Não rode os dois ao mesmo tempo**: o DuckDB permite um único escritor.
Mudanças em código Python exigem reiniciar o servidor; arquivos de `web/` só precisam de reload do navegador.

---

## Jornada do usuário

### Tela única

| Área | O que faz |
|------|-----------|
| **Topo** | Logo, título centralizado, tema claro/escuro e status da rodada |
| **Esquerda (cima)** | **Novo estudo** / **Abrir estudo** (estudos salvos no banco), cartão com nome, ID e data/hora do estudo, abrir arquivo, Horizonte, painel **Métodos** (até 5; vazio = sistema recomenda por item), **Gerar** e **Exportar Excel** |
| **Esquerda (baixo)** | Filtros: busca e seletor hierárquico em cascata (ex.: Classe → Molécula → Produto → SKU) com múltipla seleção |
| **Centro** | Gráfico com 3 abas: *Projeção do item*, *Comparar métodos* (todas as curvas do item) e *Comparar itens* (uma linha por item marcado, com o mesmo método) |
| **Direita** | Cards explicados: Último MAT, Variação MAT (YoY), CAGR do MAT (com 36+ meses), Total projetado no horizonte, Erro do método (WAPE), Viés (Bias) e estatísticas do histórico (máxima, mínima, desvio padrão, erro padrão da média) |
| **Baixo** | Grid do horizonte com os valores projetados |

**Estudos salvos:** cada rodada concluída fica no banco local com ID e data/hora; ao terminar,
a tela pede um **nome** para o estudo (*Salvar*). **Novo estudo** volta à tela limpa e
**Abrir estudo** lista e reabre estudos anteriores (gráficos, cards e exportação); rodadas sem nome ficam ocultas por padrão e cada estudo pode ser **excluído** (com confirmação; definitivo). Detalhes no
[MANUAL.md](MANUAL.md#31-tela-única-uso-rápido).

Limites conhecidos: não há formulário de mapeamento manual (arquivos que a
auto-detecção não reconhecer devem ser tratados no Laboratório) e os valores
projetados não são editáveis na tela — ajustes são feitos no Excel exportado.

### Laboratório (wizard de 5 etapas)

Assistente linear (wizard) de 5 etapas, com barra de progresso e rodapé
**← Voltar | Avançar →** (habilitado só quando a etapa atual está completa).
Ações de conclusão avançam automaticamente; a barra lateral serve como
indicador de progresso e "Abrir estudo salvo".

| Etapa | O que fazer | Resultado esperado |
|-------|------------|-------------------|
| 1. **Arquivo** | Nome do estudo, formato (Longo/Largo), opções de leitura, upload | Colunas detectadas e template disponível para download |
| 2. **Mapeamento** | Dimensões, hierarquia, medidas, mapa temporal, frequência, moeda | `StudyConfig`/mapeamento confirmados e diagnóstico estrutural |
| 3. **Qualidade** | Revisar diagnóstico por série e decidir tratamento | Base elegível, exclusões visíveis e ajustes rastreáveis |
| 4. **Previsão** | Horizonte, métodos a comparar (multiseletor), modo rápido/avançado, ML e cenários/regressoras (opcional), executar | Rodada com progresso automático; previsão de **todos** os métodos escolhidos, persistida por série | 
| 5. **Resultados** | Filtros, método de referência, gráfico multi-método, tabelas e download CSV/XLSX em um clique | Saída vinculada à rodada com métricas de backtest por método e rastreabilidade completa |

---

## Testes e dados de exemplo

Executar a suíte de testes funcional (núcleo + fumaça da UI):

```bash
python -m pytest dev/ -v
```

Gerar dados sintéticos para exploração:

```bash
python dev/generate_mock_data.py
```

> O arquivo `N05A.xlsx` (523 entidades × 60 meses) é um sample real opcional.
> O teste `test_n05a_normalizacao_e_forecast` é pulado automaticamente se o arquivo estiver ausente.

---

## Modelos disponíveis

A Etapa 4 do Laboratório (e o painel **Métodos** da tela única, dividido em Núcleo e
Avançado, com ⏱ nos métodos lentos em bases grandes) mostra o **catálogo dos métodos
disponíveis** e deixa você marcar quais executar. Na tela única o teto é de 5 métodos
por rodada e `AutoARIMA_X` não é oferecido (não há tela de regressoras). Os modelos são do pacote
**StatsForecast** (Nixtla). A tabela abaixo resume **todos** os candidatos e as
condições para aparecerem no catálogo.

> O Forecasting Community **não escolhe um vencedor**: a rodada prevê e persiste
> todos os métodos marcados, e o dashboard apresenta o backtest de cada um para
> você decidir.

### Núcleo (modo rápido e avançado)
| Alias | Tipo | Quando aparece |
|-------|------|----------------|
| `Naive` | Baseline | Sempre |
| `MediaMovel3` `MediaMovel6` `MediaMovel12` | Média móvel de janela fixa | Sempre (janela 3/6/12) |
| `HistoricAverage` | Baseline de nível médio | Sempre |
| `SeasonalNaive` | Baseline sazonal | Frequência mensal ou trimestral |
| `RegLinearDrift` | Tendência linear (random walk com drift) | Sempre |
| `Holt` | Suavização exponencial com tendência | Sempre |
| `HoltDamped` | Suavização exponencial com tendência amortecida | Sempre |
| `AutoETS` | Suavização exponencial automática (nível+tendência+sazonalidade) | Sempre |
| `ETS_Damped` | ETS com tendência amortecida | Sempre |
| `AutoTheta` | Método Theta | Sempre |
| `CrostonSBA` | Intermitente (Croston-SBA) | Série com ≥ 2 valores positivos e sem negativos |
| `TSB` | Intermitente (Teunter–Syntetos–Babai) | Mesma condição do CrostonSBA |
| `AutoCES` | Complex Exponential Smoothing | Modo **avançado** |
| `AutoARIMA` | ARIMA/SARIMA automático | Modo **avançado** |
| `AutoTBATS` | TBATS (sazonalidade complexa) | Modo **avançado** |
| `AutoARIMA_X` | ARIMA com regressoras | Quando há regressoras válidas |
| `ZeroBaseline` | Previsão zero | **Automático** para séries com histórico todo zero (não selecionável) |

### Modo avançado (adicional)
O checkbox **"Incluir métodos avançados (ARIMA, CES e TBATS)"** adiciona
`AutoCES`, `AutoARIMA` e `AutoTBATS` ao catálogo.

### Aprendizado global (opcional)
| Alias | Tipo | Condição |
|-------|------|----------|
| `LightGBM` | Gradient boosting global via MLForecast | ≥ 20 entidades e ≥ 200 linhas treináveis na medida; pacotes de `requirements-ml.txt` instalados; checkbox "Habilitar aprendizado global" marcado |
| `XGBoost` | Gradient boosting global via MLForecast | Mesma condição do LightGBM (requer `xgboost`) |

- O catálogo da Etapa 4 traz também **"Como funciona"** de cada método para
  apoiar a escolha.
- **Comprimento da sazonalidade (ciclo)** é configurável em *Opções avançadas de
  execução*: `auto` (padrão: 12 mensal/MAT, 4 trimestral, 1 anual) ou valor explícito
  (12, 6, 4, 3, 2 ou 1). Vale para `SeasonalNaive`, `AutoETS`, `ETS_Damped`,
  `AutoTheta`, `AutoCES`, `AutoTBATS`, `AutoARIMA` e `AutoARIMA_X`;
  `Holt`/`HoltDamped` e os intermitentes (`CrostonSBA`/`TSB`) não usam sazonalidade.
- Seleção padrão da interface: `Naive`, `HistoricAverage`, `RegLinearDrift` e
  `AutoETS` (marcados na primeira vez; você pode mudar).
- Sem explicação teórica detalhada aqui: consulte o [MANUAL.md](MANUAL.md),
  seção 6, que descreve o funcionamento de cada modelo e quando usá-lo.

---

## Exportação (tela única)

O botão **Exportar Excel** baixa `forecast_<run_id>.xlsx` (também disponível em
`GET /runs/{run_id}/export.xlsx`), no formato da base de origem, só com as séries mais detalhadas (folhas):

| Aba | Conteúdo |
|-----|----------|
| `Historico` | Colunas de dimensão + uma coluna por período do histórico (`YYYYMM`) |
| Uma aba por método rodado (`AutoETS`, `Naive`, ...) | Colunas de dimensão + uma coluna por período projetado |
| `Metadados` | Configuração da rodada e versões das dependências |

O Laboratório mantém a exportação CSV/XLSX completa (longo/largo, métricas, qualidade).

---

## Frequências suportadas

| Frequência | Passo | Histórico máximo | Horizonte máximo |
|------------|-------|:-:|:-:|
| Mensal | MS | 60 meses | 120 meses |
| Trimestral | QS | 20 trimestres | 40 trimestres |
| Anual | YS | 5 anos | 10 anos |
| MAT (direto) | MS | 60 posições | 120 posições |
| MAT (derivado) | Modelado em meses | 60 meses | 120 meses |

---

## Regras centrais do produto

- **Piso zero obrigatório** em toda projeção (Unidades, Valor, cenários, agregações e
  limites dos intervalos). Não é desativável.
- **Sem eleição às escondidas**: com métodos marcados, a rodada prevê exatamente
  esses (backtest informativo por método). Na tela única, sem seleção, o sistema
  recomenda o melhor por item (WAPE em 1 fold) e registra o motivo
  (`selection_reason`); todos os candidatos ficam persistidos e comparáveis.
- Identificadores (EAN etc.) são sempre tratados como **texto**; chaves usam hash SHA-256 estável.
- **Validação temporal** com treino expansivo; nenhum fold consulta o futuro; dados
  imputados no teste não são usados como verdade.
- **Bottom-Up/MinT** para coerência hierárquica; a soma dos pais é feita por método;
  intervalos de nós agregados ficam nulos com motivo explícito.
- **Cenários** são ajustes incrementais sobre a previsão base; regressoras exigem
  histórico e futuro preenchidos.
- Duplicidades, lacunas, zeros e outliers exigem **decisão explícita do usuário**;
  nenhum tratamento silencioso.

---

## Estrutura de arquivos

```
LICENSE                Licença MIT
app.py                 Entry do Laboratório (Streamlit)
contracts.py           Dataclasses, enums, schemas e constantes
api/                   API FastAPI da tela única (python -m api)
   ├── main.py         App, roteadores e servidor estático de web/
   ├── state.py        Conexão DuckDB única e registro de rodadas em andamento
   ├── datasets.py     Upload com auto-detecção e mapeamento manual (API)
   ├── runs.py         Rodada em background, progresso e cancelamento
   ├── results.py      Árvore, série agregada, por modelo e exceções
   ├── models.py       Catálogo de métodos para o seletor
   ├── exports.py      Download do XLSX (histórico + uma aba por modelo)
   └── studies.py      Salvar (nomear), listar e reabrir estudos
web/                   Tela única: index.html, app.js, styles.css e vendor/ (Alpine, ECharts, AG Grid — sem CDN)
data_engine/           Leitura, template, normalização, qualidade, DuckDB e exportação
   ├── dates.py        Grade temporal e conversões de período
   ├── templates.py    Download do modelo de arquivo
   ├── ingest.py       Inspeção/leitura de CSV e XLSX
   ├── profile.py      Diagnóstico de qualidade por série
   ├── normalize.py    Normalização canônica do dataset
   ├── db.py           Schema DuckDB (v2), migrações e persistência
   ├── results.py      Consultas, métricas de backtest e dashboard
   ├── studies.py      Estudos salvos: nome, ID e data/hora de uma rodada
   └── exports.py      Exportação CSV/XLSX (longo, largo, métricas, qualidade) e XLSX por modelo
forecast_engine/       Candidatos, CV, previsão final, premissas e hierarquia
   ├── models.py       Registro/catálogo de métodos e fábrica StatsForecast
   ├── const.py        Labels PT e ordem/rank dos candidatos
   ├── cv.py           Validação temporal (folds) e avaliação por série
   ├── runner.py       Orquestrador run_forecast (lotes, cenários, reconciliação)
   ├── final.py        Previsão final (reajuste no histórico completo)
   ├── hierarchy.py    Nós, agregação e reconciliação Bottom-Up/MinT
   ├── scenarios.py    Regras e cenários determinísticos
   ├── regressors.py   Regressoras exógenas (AutoARIMA_X)
   ├── ml.py           Aprendizado global (LightGBM/XGBoost via MLForecast)
   ├── metrics.py      Métricas (MAE, RMSE, WAPE, bias) e piso zero
   └── dates.py        Datas do motor
app_ui/                Laboratório: interface Streamlit (wizard de 5 etapas)
   ├── shell.py        Estado de sessão, navegação e cabeçalho
   ├── config_study.py Etapa 1 — Arquivo
   ├── mapping.py      Etapa 2 — Mapeamento
   ├── quality.py      Etapa 3 — Qualidade
   ├── assumptions.py  Premissas e cenários
   ├── run.py          Etapa 4 — Previsão
   └── dashboard.py    Etapa 5 — Resultados
docs/img/              Prints da tela única usados neste README
requirements.txt       Dependências do núcleo (versões testadas)
requirements-ml.txt    Complemento opcional: aprendizado global (LightGBM/XGBoost)
exports/               Artefatos de exportação gerados (mock e downloads)
.streamlit/config.toml Localhost, telemetria desativada, limite de upload
.local/                Banco DuckDB e artefatos de runtime (nunca versionado)
dev/                   Suíte de testes, mocks e benchmarks (não versionado)
```

---

## Dependências principais

| Pacote | Versão | Função |
|--------|--------|--------|
| `fastapi` / `uvicorn` | `>=0.118` / `>=0.34` | API local e servidor da tela única (versões mínimas: o `streamlit` 1.63 impõe `starlette>=0.46`) |
| `python-multipart` | `>=0.0.20` | Upload de arquivos na API |
| `streamlit` | 1.63.0 | Laboratório (wizard) |
| `polars` | 1.44.2 | Leitura e transformação de dados |
| `statsforecast` | 2.1.1 | Modelos estatísticos (ETS, ARIMA, Theta, Croston…) |
| `duckdb` | 1.1.3 | Persistência local embutida |
| `plotly` | 5.24.1 | Gráficos interativos |
| `pandas` | 2.3.3 | Adaptador pontual para interfaces de bibliotecas |
| `openpyxl` | 3.1.5 | Leitura de XLSX |
| `pyarrow` | 19.0.1 | Serialização columnar |

Métodos globais (opcionais, `requirements-ml.txt`): `mlforecast==1.1.0`, `lightgbm==4.7.0`, `xgboost==2.1.4`.
A reconciliação Bottom-Up/MinT é implementada em `forecast_engine/hierarchy.py`, sem `hierarchicalforecast`.

---

## Segurança e privacidade

- Nenhum dado de usuário sai do computador.
- O arquivo `.local/` (incluindo `forecast.duckdb`) está no `.gitignore`.
- A execução local não requer conexão com a internet.
- Nenhuma chave de API, segredo ou credencial é necessária.

---

## Licença

Distribuído sob a **licença MIT** — veja o arquivo [LICENSE](LICENSE). Você pode usar,
copiar, modificar e redistribuir, inclusive comercialmente, mantendo o aviso de copyright.

Componentes de terceiros mantêm suas próprias licenças: as bibliotecas de front-end
incluídas em `web/vendor/` (Alpine.js — MIT; Apache ECharts — Apache-2.0; AG Grid
Community — MIT) e as dependências Python instaladas por `requirements*.txt` (por exemplo
StatsForecast e Streamlit — Apache-2.0; Polars e DuckDB — MIT).