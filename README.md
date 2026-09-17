# Forecast Community

Ferramenta **local** de forecasting para analistas de BI, SFE e dados. Configure
estudos, importe CSV/XLSX (formatos longo e largo), faça diagnóstico de qualidade,
valide modelos com validação temporal, aplique premissas e cenários determinísticos,
reconcilie hierarquias (Bottom-Up / MinT) e exporte resultados em CSV/XLSX.

**A aplicação não elege um "modelo vencedor".** Você escolhe os métodos a executar
e o Forecast Community prevê **todos** eles por série, mostrando as métricas de
backtest para cada um — a decisão final de qual usar é sua.

**Os dados permanecem no seu computador.** Não há serviço remoto obrigatório,
API de IA, autenticação nem banco servidor — apenas Python e Streamlit rodando localmente.

---

## Início rápido (Windows)

Pré-requisito: [Python 3.12+](https://www.python.org/downloads/) instalado (marque "Add python.exe to PATH" no instalador).

1. Dê **dois cliques** em [`instalar.bat`](instalar.bat) (só na primeira vez) — cria o ambiente virtual e instala as dependências.
2. Dê **dois cliques** em [`iniciar.bat`](iniciar.bat) sempre que quiser abrir o Forecast Community.

O navegador abre sozinho em `http://localhost:8501`.

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

# 2. Instale as dependências (arquivo único: núcleo + ML)
pip install -r requirements.txt
```

No Windows, basta dar dois cliques em `instalar.bat`: ele cria o `.venv`, instala
`requirements.txt` e valida a importação dos pacotes.

O arquivo único já inclui **MLForecast + LightGBM + XGBoost**, usados pelos métodos
globais da Etapa 4. Se quiser uma instalação menor (sem os métodos globais), remova
o bloco "Aprendizado global" do `requirements.txt` — o núcleo continua funcionando.

### Dependências de desenvolvimento e testes

```bash
pip install -r dev/requirements-dev.txt
```

---

## Execução

```bash
python -m streamlit run app.py
```

A aplicação abre em `http://localhost:8501`. Nenhum dado, chave de API ou conexão com a nuvem é necessário para iniciar.

Os dados de trabalho ficam em `.local/forecast.duckdb` (nunca versionado).

---

## Jornada do usuário

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

A Etapa 4 mostra um **catálogo exato dos métodos disponíveis** para a sua
configuração e deixa você marcar quais executar. Os modelos são do pacote
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
| `LightGBM` | Gradient boosting global via MLForecast | ≥ 20 entidades e ≥ 200 linhas treináveis na medida; pacotes de `requirements.txt` instalados; checkbox "Habilitar aprendizado global" marcado |
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
- **Sem eleição de vencedor**: a rodada prevê exatamente os métodos escolhidos na
  Etapa 4; o backtest é informativo e apresentado por método no dashboard.
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
app.py                 Entry fino (configuração da página + roteamento)
contracts.py           Dataclasses, enums, schemas e constantes
data_engine/           Leitura, template, normalização, qualidade, DuckDB e exportação
   ├── dates.py        Grade temporal e conversões de período
   ├── templates.py    Download do modelo de arquivo
   ├── ingest.py       Inspeção/leitura de CSV e XLSX
   ├── profile.py      Diagnóstico de qualidade por série
   ├── normalize.py    Normalização canônica do dataset
   ├── db.py           Schema DuckDB (v2), migrações e persistência
   ├── results.py      Consultas, métricas de backtest e dashboard
   └── exports.py      Exportação CSV/XLSX (longo, largo, métricas, qualidade)
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
app_ui/                Interface (wizard de 5 etapas)
   ├── shell.py        Estado de sessão, navegação e cabeçalho
   ├── config_study.py Etapa 1 — Arquivo
   ├── mapping.py      Etapa 2 — Mapeamento
   ├── quality.py      Etapa 3 — Qualidade
   ├── assumptions.py  Premissas e cenários
   ├── run.py          Etapa 4 — Previsão
   └── dashboard.py    Etapa 5 — Resultados
requirements.txt       Dependências (arquivo único: núcleo + ML, versões testadas)
exports/               Artefatos de exportação gerados (mock e downloads)
.streamlit/config.toml Localhost, telemetria desativada, limite de upload
.local/                Banco DuckDB e artefatos de runtime (nunca versionado)
dev/                   Suíte de testes, mocks e benchmarks (não versionado)
```

---

## Dependências principais

| Pacote | Versão | Função |
|--------|--------|--------|
| `streamlit` | 1.63.0 | Interface web local |
| `polars` | 1.44.2 | Leitura e transformação de dados |
| `statsforecast` | 2.1.1 | Modelos estatísticos (ETS, ARIMA, Theta, Croston…) |
| `duckdb` | 1.1.3 | Persistência local embutida |
| `plotly` | 5.24.1 | Gráficos interativos |
| `pandas` | 2.3.3 | Adaptador pontual para interfaces de bibliotecas |
| `openpyxl` | 3.1.5 | Leitura de XLSX |
| `pyarrow` | 19.0.1 | Serialização columnar |

Métodos globais: `mlforecast==1.1.0`, `lightgbm==4.7.0`, `xgboost==2.1.4` (já no `requirements.txt`).
A reconciliação Bottom-Up/MinT é implementada em `forecast_engine/hierarchy.py`, sem `hierarchicalforecast`.

---

## Segurança e privacidade

- Nenhum dado de usuário sai do computador.
- O arquivo `.local/` (incluindo `forecast.duckdb`) está no `.gitignore`.
- A execução local não requer conexão com a internet.
- Nenhuma chave de API, segredo ou credencial é necessária.

---

## Licença

A definir pelo proprietário antes da publicação pública.