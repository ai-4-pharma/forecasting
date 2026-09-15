# Forecast Community

Ferramenta **local** de forecasting para analistas de BI, SFE e dados. Configure
estudos, importe CSV/XLSX (formatos longo e largo), faça diagnóstico de qualidade,
compare modelos com validação temporal, aplique premissas e cenários determinísticos,
reconcilie hierarquias Bottom-Up e exporte resultados em CSV/XLSX.

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
| B | Motor estatístico e seleção de modelos (P15–P20) | ✅ Concluída — Gate B verde (7/7 casos) |
| C | Premissas, cenários e hierarquia (P21–P27) | ✅ Concluída — Gate C verde (8/8 casos) |
| D | Aprendizado global opcional (P28–P30) | ✅ Concluída — Gate D verde (3/3 casos) |
| E | Dashboard e exportação completa (P31–P35) | ✅ Concluída — Gate E parcial (E04/E03 cobertos; E01/E02/E05/E06/E07 pendentes) |
| F | Gate E final e benchmark (P36) | 🔄 Próximo passo |
| G | Documentação e encerramento (P37–P38) | 🔄 Em andamento |

**Suíte atual:** 52 testes passando · `python -m pytest test_pipeline.py` · Data: 11/09/2026

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
```

### Complemento opcional — Modo Avançado com ML

Habilita o candidato **LightGBM global** via MLForecast. Requer o núcleo já instalado.

```bash
pip install -r requirements-ml.txt
```

### Dependências de desenvolvimento e testes

```bash
pip install -r requirements-dev.txt
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
| 4. **Previsão** | Horizonte, modo rápido/avançado, cenários/regressoras (opcional), executar | Rodada com progresso automático e resultados persistidos |
| 5. **Resultados** | Filtros, gráficos, cards e download CSV/XLSX em um clique | Saída vinculada à rodada com rastreabilidade completa |

---

## Testes e dados de exemplo

Executar a suíte de testes funcional:

```bash
python -m pytest test_pipeline.py -v
```

Gerar dados sintéticos para exploração:

```bash
python generate_mock_data.py
```

> O arquivo `N05A.xlsx` (523 entidades × 60 meses) é um sample real opcional.
> O teste `test_n05a_normalizacao_e_forecast` é pulado automaticamente se o arquivo estiver ausente.

---

## Modelos disponíveis

### Modo Rápido (padrão, núcleo)
| Alias | Tipo | Condição |
|-------|------|----------|
| `Naive` | Baseline | Sempre disponível |
| `HistoricAverage` | Baseline | Sempre disponível |
| `SeasonalNaive` | Baseline sazonal | ≥ 2 ciclos no menor treino |
| `AutoETS` | Exponential Smoothing | ≥ 8 observações no menor treino |
| `AutoTheta` | Theta | ≥ 8 observações no menor treino |
| `CrostonSBA` | Intermitente | ADI ≥ 1,32 e ≥ 2 valores positivos |
| `TSB` | Intermitente | Mesma elegibilidade do CrostonSBA |
| `ZeroBaseline` | Série toda zero | Automático |

### Modo Avançado (adicional)
| Alias | Tipo | Condição |
|-------|------|----------|
| `AutoCES` | Complex Exp. Smoothing | ≥ 8 observações no menor treino |
| `AutoARIMA` | ARIMA automático | ≥ 8 observações; max_p/q=3 |
| `AutoARIMA_X` | ARIMA com regressoras | Histórico + futuro de regressoras válidos |
| `LightGBM` | Modelo global ML | ≥ 20 entidades e ≥ 200 linhas treináveis; `requirements-ml.txt` instalado |

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
- Identificadores (EAN etc.) são sempre tratados como **texto**; chaves usam hash SHA-256 estável.
- **Validação temporal** com treino expansivo; nenhum fold consulta o futuro; dados
  imputados no teste não são usados como verdade.
- **Bottom-Up** para coerência hierárquica; intervalos de nós agregados ficam nulos
  com motivo explícito.
- **Cenários** são ajustes incrementais sobre a previsão base; regressoras exigem
  histórico e futuro preenchidos.
- Duplicidades, lacunas, zeros e outliers exigem **decisão explícita do usuário**;
  nenhum tratamento silencioso.

---

## Estrutura de arquivos

```
app.py                 UI e coordenação do fluxo
contracts.py           Dataclasses, enums, schemas e constantes
data_engine.py         Templates, ingestão, qualidade, DuckDB e exportação
forecast_engine.py     Candidatos, CV, previsão, premissas e hierarquia
test_pipeline.py       Suíte funcional por gates (A–E)
generate_mock_data.py  Dados sintéticos e benchmark reproduzível
requirements.txt       Dependências do núcleo (versões testadas)
requirements-ml.txt    Complemento opcional: MLForecast + LightGBM
requirements-dev.txt   pytest e psutil
.streamlit/config.toml Localhost, telemetria desativada, limite de upload
.local/                Banco DuckDB e artefatos de runtime (nunca versionado)
_plan.md               Plano de implementação detalhado e registro de decisões
```

---

## Dependências principais

| Pacote | Versão | Função |
|--------|--------|--------|
| `streamlit` | 1.63.0 | Interface web local |
| `polars` | 1.44.2 | Leitura e transformação de dados |
| `statsforecast` | 2.1.1 | Modelos estatísticos (ETS, ARIMA, Theta, Croston…) |
| `hierarchicalforecast` | 1.5.1 | Reconciliação Bottom-Up |
| `duckdb` | 1.1.3 | Persistência local embutida |
| `plotly` | 5.24.1 | Gráficos interativos |
| `pandas` | 2.3.3 | Adaptador pontual para interfaces de bibliotecas |
| `openpyxl` | 3.1.5 | Leitura de XLSX |
| `pyarrow` | 19.0.1 | Serialização columnar |

Complemento ML opcional: `mlforecast==1.1.0`, `lightgbm==4.7.0`.

---

## Segurança e privacidade

- Nenhum dado de usuário sai do computador.
- O arquivo `.local/` (incluindo `forecast.duckdb`) está no `.gitignore`.
- A execução local não requer conexão com a internet.
- Nenhuma chave de API, segredo ou credencial é necessária.

---

## Licença

A definir pelo proprietário antes da publicação pública.