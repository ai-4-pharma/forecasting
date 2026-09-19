// Forecast Community — tela única (S2.4). Alpine.js coordena estado/eventos;
// ECharts desenha o gráfico; AG Grid mostra a tabela mês a mês.
// Nenhuma interação recarrega a página (tudo via fetch para a API local).

// Instância do ECharts mantida FORA do objeto reativo do Alpine (S2.9 rodada 3).
// Alpine (via @vue/reactivity) transforma toda propriedade do x-data em um
// Proxy profundo, inclusive `this.chart` se fosse guardado ali — isso
// corrompia as referências internas do ECharts (modelos de série/eixo/legenda
// deixavam de bater com a identidade que o próprio ECharts esperava),
// quebrando silenciosamente `resize()`, hover (tooltip) e clique na legenda
// com "Cannot read properties of undefined (reading 'type')" assim que
// qualquer operação interna de reset/relayout era disparada. Reproduzido de
// forma mínima (Alpine + ECharts, sem nenhum código da aplicação) guardando
// a instância em `this.chart`; o mesmo teste sem o Proxy (variável comum)
// nunca falhou. Por isso o chart vive numa variável de módulo comum.
let _chart = null;

// Markdown mínimo e seguro para as respostas do assistente: escapa o HTML antes
// de qualquer transformação (negrito, itálico, código, listas, títulos, tabelas).
function renderChatMarkdown(text) {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (s) =>
    esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  const isRow = (l) => /^\s*\|.*\|\s*$/.test(l);
  const isSep = (l) => /^[\s:|-]+$/.test(l) && l.includes("-");
  const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
  const lines = String(text || "").split("\n");
  const out = [];
  let list = null;
  const closeList = () => {
    if (list) out.push(`</${list}>`);
    list = null;
  };
  const openList = (kind) => {
    if (list !== kind) {
      closeList();
      out.push(`<${kind}>`);
      list = kind;
    }
  };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    let m;
    if (isRow(line)) {
      closeList();
      const rows = [];
      while (i < lines.length && isRow(lines[i])) rows.push(lines[i++]);
      const body = rows.filter((r) => !isSep(r));
      let html = '<div class="chat-table-wrap"><table>';
      body.forEach((r, k) => {
        const tag = k === 0 && rows.length > 1 && isSep(rows[1]) ? "th" : "td";
        html += "<tr>" + cells(r).map((c) => `<${tag}>${inline(c)}</${tag}>`).join("") + "</tr>";
      });
      out.push(html + "</table></div>");
      continue;
    }
    if ((m = line.match(/^\s*[-*•]\s+(.*)$/))) {
      openList("ul");
      out.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
      openList("ol");
      out.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^#{1,6}\s+(.*)$/))) {
      closeList();
      out.push(`<p><strong>${inline(m[1])}</strong></p>`);
    } else if (!line.trim()) {
      closeList();
    } else {
      closeList();
      out.push(`<p>${inline(line)}</p>`);
    }
    i++;
  }
  closeList();
  return out.join("");
}

function app() {
  return {
    datasetId: null,
    preparationId: null,
    datasetLabel: "Nenhum arquivo carregado",
    qualitySummary: "",
    runId: null,
    horizon: 12,
    running: false,
    statusText: "Sem rodada",
    statusClass: "",
    errorMessage: "",
    tree: null,
    selectedItems: [], // nós marcados na cascata (S2.7); o último é o "ativo" da Aba 1
    selected: null, // {dim, value} — nó ativo (último de selectedItems)
    series: null,
    grid: null,
    treeFilter: "",
    availableModels: { core: [], advanced: [], slow: [], slow_hint: "" },
    selectedModels: [], // até 5 aliases (S2.6/S2.9, núcleo + avançado); vazio = automático
    modelsPanelOpen: false,
    modelsPanelStyle: "",
    slowHintStyle: "",
    slowHintAlias: null, // alias sob o mouse com popup de "método lento" (S2.9 rodada 4)
    activeTab: 1, // 1 = projeção do nó; 2 = comparar métodos; 3 = multi-item (S2.8)
    compareData: null, // resposta de /series/by-model para a Aba 2
    multiSeries: [], // [{label, data}] por item marcado, para a Aba 3
    multiModel: null, // método único aplicado a todos os itens da Aba 3
    theme: localStorage.getItem("theme") || "auto", // "auto" | "light" | "dark" (S2.9)
    datasetName: "", // nome do arquivo enviado (sugestão de nome do estudo)
    currentStudy: null, // {run_id, short_id, name, saved, started_at, ...} da rodada em tela
    dialog: null, // null | "save" | "open"
    saveName: "",
    saveError: "",
    saving: false,
    studies: [],
    studiesLoading: false,
    showUnnamed: localStorage.getItem("showUnnamed") === "1", // rodadas sem nome ocultas por padrão
    deleteTarget: null,
    deleting: false,
    deleteError: "",
    // Assistente (chat via OpenRouter): o backend guarda a chave; aqui só o estado da conversa.
    chatOpen: false,
    chatConfig: { configured: null, key_source: null, key_hint: null, models: [], default_model: "", pricing_source: "" }, // configured: null = ainda não consultado
    chatKeyInput: "", // chave digitada na tela (vai só ao servidor local, que a guarda em memória)
    chatKeyBusy: false,
    chatKeyError: "",
    chatModelsOpen: false, // lista de modelos (dropdown com preço e contexto)
    chatCatalog: [], // catálogo completo do OpenRouter (datalist do "Outro modelo")
    chatModel: localStorage.getItem("chatModel") || "",
    chatCustom: false,
    chatIncludeContext: localStorage.getItem("chatIncludeContext") !== "0",
    chatAck: localStorage.getItem("chatAck") === "1", // ciência de que os dados da tela vão ao OpenRouter
    chatMessages: [], // [{role, content, model?}]
    chatInput: "",
    chatSending: false,
    chatError: "",
    chatSuggestions: [
      "Explique esta projeção em poucas linhas.",
      "O que significam WAPE e Bias neste item?",
      "Por que os métodos dão projeções diferentes?",
      "Que cuidados devo ter ao usar este resultado?",
    ],
    progressLog: [], // últimas mensagens de progresso da rodada (S2.9), mais recente por último
    progressCompleted: 0,
    progressTotal: 0,
    _progressSeq: 0,

    init() {
      this.applyTheme();
      this.loadModels();
      _chart = echarts.init(this.$refs.chartEl);
      window.addEventListener("resize", () => _chart && _chart.resize());
      this.grid = agGrid.createGrid(this.$refs.gridEl, {
        columnDefs: [
          { field: "ds", headerName: "Mês", flex: 1 },
          { field: "baseline", headerName: "Baseline", flex: 1, valueFormatter: (p) => this.fmtNum(p.value) },
          {
            field: "ajuste",
            headerName: "Ajuste",
            flex: 1,
            editable: false,
            cellClass: "cell-disabled",
            valueFormatter: () => "—",
          },
          { field: "final", headerName: "Final", flex: 1, valueFormatter: (p) => this.fmtNum(p.value) },
        ],
        rowData: [],
        defaultColDef: { resizable: true, sortable: false },
      });
    },

    async loadModels() {
      try {
        const resp = await fetch("/models");
        const body = await resp.json();
        this.availableModels = {
          core: body.core || [],
          advanced: body.advanced || [],
          slow: body.slow || [],
          slow_hint: body.slow_hint || "",
        };
      } catch (e) {
        this.availableModels = { core: [], advanced: [], slow: [], slow_hint: "" };
      }
    },

    allModelAliases() {
      return [...this.availableModels.core, ...this.availableModels.advanced];
    },

    // Painel e popup de métodos são `position: fixed` (fora do fluxo da barra
    // lateral, que tem overflow e cortaria um elemento absoluto): as
    // coordenadas saem do botão/painel no momento do clique/hover.
    toggleModelsPanel(ev) {
      this.modelsPanelOpen = !this.modelsPanelOpen;
      this.slowHintAlias = null;
      if (!this.modelsPanelOpen) return;
      const btn = ev.currentTarget.getBoundingClientRect();
      const pane = ev.currentTarget.closest(".tree-pane").getBoundingClientRect();
      this.modelsPanelStyle =
        `top:${Math.round(btn.top)}px;left:${Math.round(pane.right + 6)}px;` +
        `max-height:${Math.max(200, window.innerHeight - Math.round(btn.top) - 12)}px;`;
    },
    showSlowHint(alias, ev) {
      if (!this.isSlowModel(alias)) return;
      const opt = ev.currentTarget.getBoundingClientRect();
      const panel = this.$refs.modelsPanel.getBoundingClientRect();
      this.slowHintStyle =
        `top:${Math.round(Math.min(opt.top, window.innerHeight - 200))}px;left:${Math.round(panel.right + 6)}px;`;
      this.slowHintAlias = alias;
    },
    isSlowModel(alias) {
      return this.availableModels.slow.includes(alias);
    },

    // Cor de texto do gráfico conforme o tema atual (S2.9 rodada 4): a
    // legenda usava a cor padrão do ECharts (cinza escuro, ilegível no tema
    // escuro) porque nenhum `textStyle` era passado. Lida direto da variável
    // CSS `--text` (já resolvida pelo navegador para "auto"/"claro"/"escuro"
    // em `styles.css`), em vez de duplicar a lógica de tema aqui.
    _chartTextColor() {
      return getComputedStyle(document.documentElement).getPropertyValue("--text").trim() || "#0f172a";
    },

    // Descarta e recria a instância do ECharts antes de cada setOption entre
    // abas (S2.9). `chart.clear()` não bastou: depois de alternar abas com
    // formatos de série bem diferentes, a tooltip parava de responder ao
    // hover sem lançar exceção nem log (o componente tooltip do ECharts
    // ficava com identidade interna trocada — `getComponent('tooltip')`
    // chegou a devolver o id de uma série). dispose()+init() garante estado
    // interno limpo a cada troca; o container nunca é escondido (display:none),
    // então o problema histórico de canvas 0x0 no init não se aplica aqui.
    _resetChart() {
      if (_chart) _chart.dispose();
      _chart = echarts.init(this.$refs.chartEl);
    },

    toggleModel(alias) {
      const idx = this.selectedModels.indexOf(alias);
      if (idx >= 0) {
        this.selectedModels.splice(idx, 1);
      } else if (this.selectedModels.length < 5) {
        this.selectedModels.push(alias);
      }
    },

    // Tema manual (S2.9): "auto" segue prefers-color-scheme do SO; os outros
    // dois fixam via atributo, sobrepondo a media query em styles.css.
    applyTheme() {
      if (this.theme === "auto") {
        document.documentElement.removeAttribute("data-theme");
      } else {
        document.documentElement.setAttribute("data-theme", this.theme);
      }
    },

    toggleTheme() {
      const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
      const currentlyDark = this.theme === "dark" || (this.theme === "auto" && prefersDark);
      this.theme = currentlyDark ? "light" : "dark";
      try {
        localStorage.setItem("theme", this.theme);
      } catch (e) {
        // localStorage indisponível (ex.: navegação privada); tema não persiste entre sessões
      }
      this.applyTheme();
      // A cor do texto do gráfico é escrita na `option` do ECharts no
      // momento do `setOption` (S2.9 rodada 4) — não reage sozinha a uma
      // troca de tema depois de já renderizado, por isso redesenha aqui.
      if (this.series) this.refreshActiveTabChart();
    },

    async onFileSelected(ev) {
      const file = ev.target.files[0];
      if (!file) return;
      this.errorMessage = "";
      this.statusText = "Enviando arquivo…";
      this.statusClass = "running";
      const form = new FormData();
      form.append("file", file);
      try {
        const resp = await fetch("/datasets", { method: "POST", body: form });
        const body = await resp.json();
        if (!resp.ok) {
          const msg = (body.detail && body.detail.message) || "Falha ao processar o arquivo.";
          this.errorMessage = `${msg} Mapeamento manual ainda não está disponível nesta tela.`;
          this.statusText = "Erro no upload";
          this.statusClass = "error";
          return;
        }
        this.datasetName = file.name;
        this.datasetId = body.dataset_id;
        this.preparationId = body.preparation_id;
        this.datasetLabel = `${file.name} — ${body.n_series} séries, ${body.n_periods} períodos`;
        const badges = Object.values(body.quality_badges || {});
        const comZeros = badges.filter((b) => (b.zeros_pct || 0) >= 0.6).length;
        this.qualitySummary = comZeros
          ? `${comZeros} de ${badges.length} séries com muitos zeros`
          : "";
        this.statusText = "Arquivo pronto";
        this.statusClass = "completed";
      } catch (e) {
        this.errorMessage = "Não foi possível conectar à API local.";
        this.statusText = "Erro no upload";
        this.statusClass = "error";
      } finally {
        ev.target.value = "";
      }
    },

    async startRun() {
      if (!this.datasetId || this.running) return;
      this.errorMessage = "";
      this.running = true;
      this.statusText = "Iniciando rodada…";
      this.statusClass = "running";
      this.tree = null;
      this.series = null;
      this.selected = null;
      this.compareData = null;
      this.multiSeries = [];
      this.multiModel = null;
      this.activeTab = 1;
      this.progressLog = [];
      this.progressCompleted = 0;
      this.progressTotal = 0;
      try {
        const resp = await fetch("/runs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            dataset_id: this.datasetId,
            preparation_id: this.preparationId,
            horizon: this.horizon,
            mode: "fast",
            candidate_aliases: this.selectedModels,
          }),
        });
        const body = await resp.json();
        if (!resp.ok) {
          throw new Error(body.detail || "Falha ao iniciar a rodada.");
        }
        this.runId = body.run_id;
        await this.pollProgress();
      } catch (e) {
        this.errorMessage = e.message || "Falha ao iniciar a rodada.";
        this.statusText = "Erro na rodada";
        this.statusClass = "error";
        this.running = false;
      }
    },

    async pollProgress() {
      while (true) {
        const resp = await fetch(`/runs/${this.runId}/progress`);
        const body = await resp.json();
        if (!resp.ok) {
          this.errorMessage = "Rodada não encontrada.";
          this.statusText = "Erro na rodada";
          this.statusClass = "error";
          this.running = false;
          return;
        }
        this.statusText = body.message || body.status;
        this.progressCompleted = body.completed || 0;
        this.progressTotal = body.total || 0;
        this._pushProgressLog(body.message);
        if (body.status !== "running") {
          this.running = false;
          this.statusClass = body.status === "completed" ? "completed" : "error";
          if (body.status === "completed" || body.status === "partial") {
            await this.loadTree();
            await this.refreshCurrentStudy();
            this.openSaveDialog();
          } else {
            this.errorMessage = "A rodada não terminou com sucesso.";
          }
          return;
        }
        await new Promise((r) => setTimeout(r, 1000));
      }
    },

    // Log de progresso que esmaece (S2.9): guarda só as últimas mensagens
    // distintas (evita repetir a mesma linha a cada poll de 1s parado no
    // mesmo estágio); a UI aplica a opacidade decrescente via `progressLog`.
    _pushProgressLog(message) {
      if (!message) return;
      const last = this.progressLog[this.progressLog.length - 1];
      if (last && last.message === message) return;
      this.progressLog.push({ id: ++this._progressSeq, message });
      if (this.progressLog.length > 6) this.progressLog.shift();
    },

    // ---- Estudos: salvar (nomear), novo e abrir --------------------------
    async refreshCurrentStudy() {
      try {
        const resp = await fetch(`/studies/${this.runId}`);
        this.currentStudy = resp.ok ? await resp.json() : null;
      } catch (e) {
        this.currentStudy = null;
      }
    },

    openSaveDialog() {
      if (!this.currentStudy) return;
      this.saveError = "";
      this.saveName = this.currentStudy.saved
        ? this.currentStudy.name
        : this._suggestStudyName();
      this.dialog = "save";
      this.$nextTick(() => this.$refs.saveInput && this.$refs.saveInput.select());
    },

    _suggestStudyName() {
      const base = (this.datasetName || this.currentStudy.filename || "Estudo").replace(/\.[^.]+$/, "");
      const d = new Date();
      const pad = (n) => String(n).padStart(2, "0");
      return `${base} — ${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()}`;
    },

    async saveStudy() {
      if (this.saving || !this.saveName.trim()) return;
      this.saving = true;
      this.saveError = "";
      try {
        const resp = await fetch(`/studies/${this.runId}/name`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: this.saveName }),
        });
        const body = await resp.json();
        if (!resp.ok) {
          this.saveError = typeof body.detail === "string" ? body.detail : "Não foi possível salvar o estudo.";
          return;
        }
        this.currentStudy = body;
        this.dialog = null;
      } catch (e) {
        this.saveError = "Não foi possível conectar à API local.";
      } finally {
        this.saving = false;
      }
    },

    closeDialog() {
      this.dialog = null;
    },

    newStudy() {
      if (this.running) return;
      if (this.currentStudy && !this.currentStudy.saved &&
          !confirm("O estudo atual não foi salvo com nome. Ele continua no banco, mas fica oculto na lista \"Abrir estudo\" (marque \"Mostrar rodadas sem nome\" para vê-lo). Começar um novo estudo mesmo assim?")) {
        return;
      }
      this._resetStudyState();
    },

    _resetStudyState() {
      this.datasetId = null;
      this.preparationId = null;
      this.datasetName = "";
      this.datasetLabel = "Nenhum arquivo carregado";
      this.qualitySummary = "";
      this.runId = null;
      this.currentStudy = null;
      this.horizon = 12;
      this.running = false;
      this.statusText = "Sem rodada";
      this.statusClass = "";
      this.errorMessage = "";
      this.tree = null;
      this.selectedItems = [];
      this.selected = null;
      this.series = null;
      this.treeFilter = "";
      this.selectedModels = [];
      this.modelsPanelOpen = false;
      this.slowHintAlias = null;
      this.activeTab = 1;
      this.compareData = null;
      this.multiSeries = [];
      this.multiModel = null;
      this.progressLog = [];
      this.progressCompleted = 0;
      this.progressTotal = 0;
      this.dialog = null;
      this.chatMessages = [];
      this.chatError = "";
      this._resetChart();
      if (this.grid) {
        this.grid.setGridOption("rowData", []);
        setTimeout(() => this.grid.showNoRowsOverlay(), 0);
      }
    },

    visibleStudies() {
      return this.showUnnamed ? this.studies : this.studies.filter((s) => s.saved);
    },

    unnamedCount() {
      return this.studies.filter((s) => !s.saved).length;
    },

    toggleShowUnnamed() {
      this.showUnnamed = !this.showUnnamed;
      try { localStorage.setItem("showUnnamed", this.showUnnamed ? "1" : "0"); } catch (e) { /* sem storage */ }
    },

    askDelete(st) {
      this.deleteTarget = st;
      this.deleteError = "";
      this.dialog = "delete";
    },

    cancelDelete() {
      this.deleteTarget = null;
      this.dialog = "open";
    },

    async confirmDelete() {
      const st = this.deleteTarget;
      if (!st || this.deleting) return;
      this.deleting = true;
      this.deleteError = "";
      try {
        const resp = await fetch(`/studies/${st.run_id}`, { method: "DELETE" });
        if (!resp.ok && resp.status !== 404) {
          const body = await resp.json().catch(() => ({}));
          this.deleteError = typeof body.detail === "string" ? body.detail : "Não foi possível excluir o estudo.";
          return;
        }
        this.studies = this.studies.filter((s) => s.run_id !== st.run_id);
        this.deleteTarget = null;
        if (this.runId === st.run_id) this._resetStudyState(); // estudo aberto foi excluído: tela limpa
        await this.openStudiesDialog();
      } catch (e) {
        this.deleteError = "Não foi possível conectar à API local.";
      } finally {
        this.deleting = false;
      }
    },

    async openStudiesDialog() {
      if (this.running) return;
      this.dialog = "open";
      this.studies = [];
      this.studiesLoading = true;
      try {
        const resp = await fetch("/studies");
        const body = await resp.json();
        this.studies = resp.ok ? body.studies : [];
      } catch (e) {
        this.errorMessage = "Não foi possível listar os estudos.";
      } finally {
        this.studiesLoading = false;
      }
    },

    async openStudy(runId) {
      const st = this.studies.find((s) => s.run_id === runId);
      if (!st) return;
      this._resetStudyState();
      this.runId = st.run_id;
      this.datasetId = st.dataset_id;
      this.preparationId = st.preparation_id;
      this.datasetName = st.filename;
      this.datasetLabel = `${st.filename} — ${st.n_series} séries`;
      if (st.horizon) this.horizon = st.horizon;
      this.currentStudy = st;
      this.statusText = "Estudo aberto";
      this.statusClass = "completed";
      await this.loadTree();
    },

    fmtDateTime(iso) {
      if (!iso) return "—";
      const d = new Date(iso);
      return d.toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
    },

    // ---- Assistente (chat) -------------------------------------------------
    async toggleChat() {
      this.chatOpen = !this.chatOpen;
      if (!this.chatOpen) return;
      if (this.chatConfig.configured !== true) await this.loadChatConfig();
      this.$nextTick(() => {
        this._chatScroll();
        this.$refs.chatInput && this.$refs.chatInput.focus();
      });
    },

    async loadChatConfig() {
      try {
        const resp = await fetch("/chat/config");
        const body = await resp.json();
        this.chatConfig = body;
        const ids = (body.models || []).map((m) => m.id);
        if (!this.chatModel) this.chatModel = body.default_model;
        this.chatCustom = !ids.includes(this.chatModel);
        if (this.chatCustom) this.loadChatCatalog();
      } catch (e) {
        this.chatError = "Não foi possível consultar a configuração do chat.";
      }
    },

    async loadChatCatalog() {
      if (this.chatCatalog.length) return;
      try {
        const resp = await fetch("/chat/models");
        const body = await resp.json();
        this.chatCatalog = body.models || [];
      } catch (e) {
        /* catálogo é opcional: sem ele só os favoritos e o campo livre */
      }
    },

    pickChatModel(m) {
      if (!m.available) return;
      this.chatCustom = false;
      this.setChatModel(m.id);
      this.chatModelsOpen = false;
    },

    pickCustomChatModel() {
      this.chatCustom = true;
      this.loadChatCatalog();
      this.setChatModel("");
      this.chatModelsOpen = false;
      this.$nextTick(() => this.$refs.chatCustomInput && this.$refs.chatCustomInput.focus());
    },

    chatModelSummary() {
      if (this.chatCustom) return this.chatModel ? `Outro: ${this.chatModel}` : "Outro modelo…";
      const m = this.chatConfig.models.find((x) => x.id === this.chatModel);
      if (!m) return this.chatModel || "Escolha um modelo";
      return m.input == null ? m.label : `${m.label} · US$ ${this.fmtUsd(m.input)} / ${this.fmtUsd(m.output)}`;
    },

    chatPricingNote() {
      const src = this.chatConfig.pricing_source === "live"
        ? "Preços do catálogo do OpenRouter (podem variar por provedor)."
        : "Sem acesso ao catálogo agora: valores de 19/09/2026.";
      return `USD por 1 milhão de tokens. ${src}`;
    },

    fmtUsd(v) {
      if (v == null) return "—";
      return v.toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: v < 0.1 ? 3 : 2 });
    },

    fmtCtx(v) {
      if (v == null) return "—";
      if (v >= 1e6) return `${(v / 1e6).toLocaleString("pt-BR", { maximumFractionDigits: 2 })}M`;
      return `${Math.round(v / 1000)}k`;
    },

    async activateChatKey() {
      const key = this.chatKeyInput.trim();
      if (!key || this.chatKeyBusy) return;
      this.chatKeyBusy = true;
      this.chatKeyError = "";
      try {
        const resp = await fetch("/chat/key", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ api_key: key }),
        });
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          this.chatKeyError = typeof body.detail === "string" ? body.detail : "Não foi possível guardar a chave.";
          return;
        }
        this.chatKeyInput = "";
        await this.loadChatConfig();
        if (body.verified === false) {
          this.chatError = "Chave guardada, mas não foi possível validá-la agora (sem conexão com o OpenRouter?).";
        }
      } catch (e) {
        this.chatKeyError = "Não foi possível conectar à API local.";
      } finally {
        this.chatKeyBusy = false;
      }
    },

    async forgetChatKey() {
      try {
        await fetch("/chat/key", { method: "DELETE" });
      } catch (e) {
        /* falha de rede local: o estado abaixo é reconsultado de qualquer forma */
      }
      this.chatError = "";
      await this.loadChatConfig();
    },

    setChatModel(v) {
      this.chatModel = (v || "").trim();
      try { localStorage.setItem("chatModel", this.chatModel); } catch (e) { /* sem storage */ }
    },

    ackChat() {
      this.chatAck = true;
      try { localStorage.setItem("chatAck", "1"); } catch (e) { /* sem storage */ }
    },

    saveChatPrefs() {
      try { localStorage.setItem("chatIncludeContext", this.chatIncludeContext ? "1" : "0"); } catch (e) { /* sem storage */ }
    },

    clearChat() {
      this.chatMessages = [];
      this.chatError = "";
    },

    chatHtml(text) {
      return renderChatMarkdown(text);
    },

    _chatScroll() {
      this.$nextTick(() => {
        const el = this.$refs.chatScroll;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },

    async sendChat(text) {
      const content = (text !== undefined ? text : this.chatInput).trim();
      if (!content || this.chatSending) return;
      if (!this.chatModel) {
        this.chatError = "Escolha um modelo.";
        return;
      }
      this.chatError = "";
      this.chatMessages.push({ role: "user", content });
      this.chatInput = "";
      this.chatSending = true;
      this._chatScroll();
      try {
        const resp = await fetch("/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            messages: this.chatMessages.map((m) => ({ role: m.role, content: m.content })),
            model: this.chatModel,
            context: this.chatIncludeContext ? this.chatContext() : null,
          }),
        });
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          this.chatError = typeof body.detail === "string" ? body.detail : "Não foi possível obter a resposta.";
          // devolve a pergunta ao campo para tentar de novo (ex.: outro modelo)
          this.chatMessages.pop();
          this.chatInput = content;
          if (resp.status === 400) this.chatConfig.configured = false;
          return;
        }
        this.chatMessages.push({ role: "assistant", content: body.reply, model: body.model });
      } catch (e) {
        this.chatError = "Não foi possível conectar à API local.";
        this.chatMessages.pop();
        this.chatInput = content;
      } finally {
        this.chatSending = false;
        this._chatScroll();
      }
    },

    // O que o usuário está vendo agora, em JSON compacto para o assistente:
    // estudo, item em foco, cards, histórico recente, projeção e, conforme a
    // aba ativa, a comparação de métodos ou de itens. Números arredondados.
    chatContext() {
      const r1 = (v) => (v == null || Number.isNaN(v) ? null : Math.round(v * 10) / 10);
      const pct = (v) => (v == null ? null : Math.round(v * 1000) / 10);
      const ym = (d) => String(d).slice(0, 7);
      const fc = (rows) => (rows || []).map((b) => [ym(b.ds), r1(b.yhat)]);
      const ctx = {
        estudo: this.currentStudy
          ? {
              nome: this.currentStudy.saved ? this.currentStudy.name : null,
              id: this.currentStudy.short_id,
              rodou_em: this.currentStudy.started_at,
              arquivo: this.currentStudy.filename,
              horizonte_meses: this.currentStudy.horizon || this.horizon,
              metodos_rodados: this.currentStudy.models || [],
              metodo_automatico: !this.selectedModels.length,
            }
          : null,
        arquivo: { rotulo: this.datasetLabel, qualidade: this.qualitySummary || null },
        aba_ativa: ["Projeção do item", "Comparar métodos", "Comparar itens"][this.activeTab - 1],
        filtros_marcados: this.selectedItems.map((n) => `${n.dim} = ${n.name}`),
        item_em_foco: this.selected
          ? {
              nivel: this.selected.dim,
              valor: this.selected.value,
              series_detalhadas_somadas: this.series ? this.series.n_series : null,
              medida: this.series ? this.series.measure : null,
            }
          : null,
      };
      const s = this.series;
      if (s) {
        const k = s.kpis || {};
        const st = k.stats;
        ctx.cards = {
          ultimo_mat: r1(k.mat),
          variacao_mat_yoy_pct: pct(k.yoy),
          cagr_mat_pct: pct(k.cagr),
          cagr_anos: k.cagr_years,
          meses_de_historico: k.hist_months,
          total_projetado_no_horizonte: r1(k.total_h),
          meses_projetados: k.horizon_n,
          wape_backtest_pct: pct(k.wape),
          bias_backtest: r1(k.bias),
          estatisticas_do_historico: st
            ? {
                maxima: r1(st.max), mes_da_maxima: ym(st.max_ds),
                minima: r1(st.min), mes_da_minima: ym(st.min_ds),
                desvio_padrao: r1(st.std), erro_padrao_da_media: r1(st.sem),
              }
            : null,
        };
        ctx.metodos_melhores_no_backtest = (s.alternatives || []).map((a) => ({
          metodo: a.alias,
          wape_pct: pct(a.wape),
        }));
        ctx.historico_mensal_ultimos_36 = (s.history || []).slice(-36).map((h) => [ym(h.ds), r1(h.y)]);
        ctx.projecao_mensal = {
          colunas: ["mes", "previsto", "faixa80_min", "faixa80_max"],
          linhas: (s.baseline || []).map((b) => [ym(b.ds), r1(b.yhat), r1(b.lo80), r1(b.hi80)]),
          observacao:
            "É a coluna Baseline do grid (a coluna Final é igual: não há ajustes manuais). " +
            "Com método automático, cada série detalhada usa o seu melhor método.",
        };
      }
      if (this.activeTab === 2 && this.compareData && this.compareData.models) {
        ctx.comparacao_de_metodos = this.compareData.models.map((m) => ({
          metodo: m.alias,
          wape_backtest_pct: pct(m.wape),
          projecao: fc(m.baseline),
        }));
      }
      if (this.activeTab === 3 && this.multiSeries.length) {
        ctx.comparacao_de_itens = {
          metodo_aplicado_a_todos: this.multiModel,
          itens: this.multiSeries.map((it) => {
            const k = (it.data && it.data.kpis) || {};
            return {
              item: it.label,
              ultimo_mat: r1(k.mat),
              total_projetado_no_horizonte: r1(k.total_h),
              wape_backtest_pct: pct(k.wape),
              projecao: fc(it.data && it.data.baseline),
            };
          }),
        };
      }
      return ctx;
    },

    progressPct() {
      if (!this.progressTotal) return 0;
      return Math.min(100, Math.round((100 * this.progressCompleted) / this.progressTotal));
    },

    async loadTree() {
      const resp = await fetch(`/runs/${this.runId}/tree`);
      if (!resp.ok) return;
      this.tree = await resp.json();
      this._annotatePaths(this.tree, "");
      this.selectedItems = [];
      // seleciona automaticamente o 1º filho do 1º nível (evita tela vazia).
      const first = (this.tree.children || [])[0];
      if (first) await this.addItem(first);
    },

    // `_path` identifica cada nó de forma única (posição na árvore, não só o
    // nome — o mesmo valor pode existir sob pais diferentes) e permite achar
    // descendentes por prefixo ao desmarcar um item na cascata (S2.7).
    _annotatePaths(node, prefix) {
      node._path = prefix;
      for (const c of node.children || []) {
        this._annotatePaths(c, `${prefix}/${c.dim}:${c.name}`);
      }
    },

    // Ordem das dimensões (níveis da cascata), lida da própria árvore em vez
    // de repetir `study.dimension_names` no front.
    dimsOrder() {
      const dims = [];
      let node = this.tree;
      while (node && node.children && node.children.length) {
        dims.push(node.children[0].dim);
        node = node.children[0];
      }
      return dims;
    },

    // 1 nível por dimensão; nível N+1 = união dos filhos dos itens marcados
    // no nível N (nada marcado no nível N => nível N+1 não aparece).
    cascadeLevels() {
      if (!this.tree) return [];
      const q = this.treeFilter.trim().toLowerCase();
      const levels = [];
      let parents = [this.tree];
      for (const dim of this.dimsOrder()) {
        const seen = new Map();
        for (const p of parents) {
          for (const c of p.children || []) {
            if (c.dim === dim && !seen.has(c._path)) seen.set(c._path, c);
          }
        }
        const optionsAll = Array.from(seen.values());
        const options = q
          ? optionsAll.filter((o) => o.name.toLowerCase().includes(q))
          : optionsAll;
        levels.push({ dim, options });
        const checkedHere = optionsAll.filter((o) => this.isSelected(o));
        if (!checkedHere.length) break;
        parents = checkedHere;
      }
      return levels;
    },

    isSelected(node) {
      return this.selectedItems.includes(node);
    },

    async addItem(node) {
      if (!this.isSelected(node)) this.selectedItems.push(node);
      await this.syncActiveFromSelection();
    },

    async removeItem(node) {
      this.selectedItems = this.selectedItems.filter(
        (n) => n !== node && !n._path.startsWith(node._path + "/")
      );
      await this.syncActiveFromSelection();
    },

    async toggleItem(node) {
      if (this.isSelected(node)) await this.removeItem(node);
      else await this.addItem(node);
    },

    // `<select multiple>` devolve o conjunto inteiro marcado a cada mudança;
    // comparar com o estado anterior para saber o que entrou/saiu.
    async onLevelChange(level, ev) {
      const chosen = new Set(Array.from(ev.target.selectedOptions).map((o) => o.value));
      for (const opt of level.options) {
        const was = this.isSelected(opt);
        const now = chosen.has(opt._path);
        if (now && !was) await this.addItem(opt);
        else if (!now && was) await this.removeItem(opt);
      }
    },

    async syncActiveFromSelection() {
      const last = this.selectedItems[this.selectedItems.length - 1];
      if (last) {
        await this.selectNode(last.dim, last.name);
      } else {
        this.selected = null;
        this.series = null;
      }
    },

    async selectNode(dim, value) {
      this.selected = { dim, value };
      this.errorMessage = "";
      try {
        const params = new URLSearchParams({ dim, value, measure: "unidades", scenario: "base" });
        const resp = await fetch(`/runs/${this.runId}/series?${params}`);
        const body = await resp.json();
        if (!resp.ok) {
          this.errorMessage = "Não foi possível carregar a série selecionada.";
          return;
        }
        this.series = body;
      } catch (e) {
        this.errorMessage = "Não foi possível carregar a série selecionada.";
        return;
      }
      // Fora do try acima: um erro de renderização não deve ser confundido
      // com falha ao buscar a série (o fetch já teve sucesso nesse ponto).
      this.renderGrid();
      await this.refreshActiveTabChart();
    },

    // KPIs e grid usam sempre `this.series` (nó ativo), independente da aba;
    // só o gráfico central muda de conteúdo conforme a aba (S2.8).
    async refreshActiveTabChart() {
      if (this.activeTab === 1) {
        this.renderChart();
      } else if (this.activeTab === 2) {
        await this.loadCompare();
      } else if (this.activeTab === 3) {
        await this.refreshMultiSeries();
      }
    },

    async switchTab(n) {
      this.activeTab = n;
      await this.refreshActiveTabChart();
    },

    tab3ModelOptions() {
      return this.selectedModels.length ? this.selectedModels : this.allModelAliases();
    },

    // Aba 2: todas as curvas de método rodadas no nó ativo, sobrepostas.
    async loadCompare() {
      if (!this.selected) {
        this.compareData = null;
        this.renderCompareChart();
        return;
      }
      try {
        const { dim, value } = this.selected;
        const params = new URLSearchParams({ dim, value, measure: "unidades", scenario: "base" });
        const resp = await fetch(`/runs/${this.runId}/series/by-model?${params}`);
        this.compareData = resp.ok ? await resp.json() : null;
      } catch (e) {
        this.compareData = null;
      }
      this.renderCompareChart();
    },

    // Aba 3: 1 linha por item marcado na cascata (S2.7), todos com o mesmo
    // método escolhido no dropdown da aba — não rerroda a previsão, só troca
    // qual curva já persistida é lida por item.
    async refreshMultiSeries() {
      if (!this.multiModel) this.multiModel = this.tab3ModelOptions()[0] || null;
      if (!this.multiModel || !this.selectedItems.length) {
        this.multiSeries = [];
        this.renderMultiChart();
        return;
      }
      const results = [];
      for (const node of this.selectedItems) {
        try {
          const params = new URLSearchParams({
            dim: node.dim,
            value: node.name,
            measure: "unidades",
            scenario: "base",
            model: this.multiModel,
          });
          const resp = await fetch(`/runs/${this.runId}/series?${params}`);
          if (resp.ok) results.push({ label: node.name, data: await resp.json() });
        } catch (e) {
          // item que falhar é ignorado; os demais continuam aparecendo
        }
      }
      this.multiSeries = results;
      this.renderMultiChart();
    },

    async onMultiModelChange() {
      await this.refreshMultiSeries();
    },

    renderChart() {
      if (!_chart || !this.series) return;
      const hist = this.series.history;
      const base = this.series.baseline;
      const dates = [...hist.map((r) => r.ds), ...base.map((r) => r.ds)];
      const histMap = Object.fromEntries(hist.map((r) => [r.ds, r.y]));
      const loMap = Object.fromEntries(base.map((r) => [r.ds, r.lo80]));
      const hiMap = Object.fromEntries(base.map((r) => [r.ds, r.hi80]));
      const baseMap = Object.fromEntries(base.map((r) => [r.ds, r.yhat]));

      // clear() antes de cada setOption (S2.9): as 3 abas alternam entre
      // opções com número/forma de séries bem diferentes (5 aqui, 1 método
      // menos na Aba 2, N itens x2 na Aba 3); reaproveitar o diff interno do
      // ECharts entre opções tão diferentes é o que gerava
      // "Cannot read properties of undefined (reading 'type')" ao passar o
      // mouse logo após trocar de aba (não só num resize manual, como se
      // pensava em S2.8) — tooltip/legenda pareciam simplesmente não
      // responder porque o hover quebrava silenciosamente. clear() descarta
      // o estado anterior por completo antes do setOption(notMerge:true).
      this._resetChart();
      const textColor = this._chartTextColor();
      _chart.setOption({
        color: ["#64748b", "#2563eb", "#f97316"],
        tooltip: { trigger: "axis", confine: true, valueFormatter: (v) => this.fmt(v) },
        legend: {
          data: ["Histórico", "Baseline", "Final", "Faixa 80%"],
          selectedMode: true,
          textStyle: { color: textColor },
        },
        // containLabel: true — o rótulo do eixo Y (números de 6-7 dígitos)
        // entra na área reservada em vez de ser cortado pela borda do canvas
        // (S2.9: usuário reportou eixos cortados pela tabela/sidebar).
        grid: { left: 16, right: 24, top: 40, bottom: 30, containLabel: true },
        xAxis: { type: "category", data: dates, axisLabel: { color: textColor } },
        yAxis: { type: "value", axisLabel: { color: textColor } },
        series: [
          {
            name: "Histórico",
            type: "line",
            data: dates.map((d) => histMap[d] ?? null),
            color: "#64748b",
            symbol: "none",
          },
          {
            name: "Faixa 80%",
            type: "line",
            data: dates.map((d) => loMap[d] ?? null),
            lineStyle: { opacity: 0 },
            stack: "faixa",
            symbol: "none",
            silent: true,
          },
          {
            name: "Faixa 80% (largura)",
            type: "line",
            data: dates.map((d) =>
              loMap[d] != null && hiMap[d] != null ? hiMap[d] - loMap[d] : null
            ),
            lineStyle: { opacity: 0 },
            areaStyle: { color: "#2563eb", opacity: 0.15 },
            stack: "faixa",
            symbol: "none",
            silent: true,
            tooltip: { show: false },
          },
          {
            name: "Baseline",
            type: "line",
            data: dates.map((d) => baseMap[d] ?? null),
            color: "#2563eb",
            symbol: "none",
          },
          {
            // Sem overrides ainda (S3.1): "final" replica o baseline.
            name: "Final",
            type: "line",
            data: dates.map((d) => baseMap[d] ?? null),
            color: "#f97316",
            lineStyle: { type: "dashed" },
            symbol: "none",
          },
        ],
      }, true); // notMerge: true — evita o diff interno do ECharts entre
      // séries de nós diferentes (datas/tamanhos distintos), que quebrava
      // com "Cannot read properties of undefined (reading 'type')".
    },

    // Aba 2: histórico + 1 linha por método rodado no nó ativo (WAPE no nome
    // da série, mostrado na legenda/tooltip do próprio ECharts).
    renderCompareChart() {
      if (!_chart) return;
      const models = (this.compareData && this.compareData.models) || [];
      if (!models.length) {
        this._resetChart();
        _chart.setOption({ title: undefined, legend: { data: [] }, series: [] }, true);
        return;
      }
      const hist = this.compareData.history || [];
      const dates = Array.from(
        new Set([...hist.map((r) => r.ds), ...models.flatMap((m) => m.baseline.map((r) => r.ds))])
      ).sort();
      const histMap = Object.fromEntries(hist.map((r) => [r.ds, r.y]));
      const palette = ["#2563eb", "#f97316", "#16a34a", "#9333ea", "#dc2626", "#0891b2"];

      const series = [
        { name: "Histórico", type: "line", data: dates.map((d) => histMap[d] ?? null), color: "#64748b", symbol: "none" },
        ...models.map((m, i) => {
          const map = Object.fromEntries(m.baseline.map((r) => [r.ds, r.yhat]));
          const name = m.wape != null ? `${m.alias} (WAPE ${(m.wape * 100).toFixed(1)}%)` : m.alias;
          return { name, type: "line", data: dates.map((d) => map[d] ?? null), color: palette[i % palette.length], symbol: "none" };
        }),
      ];

      this._resetChart();
      const textColor = this._chartTextColor();
      _chart.setOption({
        tooltip: { trigger: "axis", confine: true, valueFormatter: (v) => this.fmt(v) },
        legend: { data: series.map((s) => s.name), top: 0, selectedMode: true, textStyle: { color: textColor } },
        grid: { left: 16, right: 24, top: 50, bottom: 30, containLabel: true },
        xAxis: { type: "category", data: dates, axisLabel: { color: textColor } },
        yAxis: { type: "value", axisLabel: { color: textColor } },
        series,
      }, true);
    },

    // Aba 3: 1 linha por item marcado na cascata, para comparar trajetórias
    // entre itens — 2 séries por item (mesma cor, mesmo nome de legenda):
    // trecho histórico sólido e trecho projetado tracejado, conectados no
    // último mês observado (S2.9: usuário pediu para distinguir visualmente
    // histórico de projeção nesta aba).
    renderMultiChart() {
      if (!_chart) return;
      if (!this.multiSeries.length) {
        this._resetChart();
        _chart.setOption({ legend: { data: [] }, series: [] }, true);
        return;
      }
      const allDates = new Set();
      for (const item of this.multiSeries) {
        for (const r of item.data.history || []) allDates.add(r.ds);
        for (const r of item.data.baseline || []) allDates.add(r.ds);
      }
      const dates = Array.from(allDates).sort();
      const palette = ["#2563eb", "#f97316", "#16a34a", "#9333ea", "#dc2626", "#0891b2", "#ca8a04", "#0d9488"];

      const series = [];
      this.multiSeries.forEach((item, i) => {
        const histMap = Object.fromEntries((item.data.history || []).map((r) => [r.ds, r.y]));
        const baseMap = Object.fromEntries((item.data.baseline || []).map((r) => [r.ds, r.yhat]));
        const histDates = (item.data.history || []).map((r) => r.ds).sort();
        const lastHistDs = histDates.length ? histDates[histDates.length - 1] : null;
        const color = palette[i % palette.length];
        series.push({
          name: item.label,
          type: "line",
          data: dates.map((d) => histMap[d] ?? null),
          color,
          symbol: "none",
        });
        series.push({
          name: item.label,
          type: "line",
          data: dates.map((d) => {
            if (d in baseMap) return baseMap[d];
            if (d === lastHistDs) return histMap[d] ?? null; // conecta sólido -> tracejado
            return null;
          }),
          color,
          lineStyle: { type: "dashed" },
          symbol: "none",
          tooltip: { show: false },
          legendHoverLink: false,
        });
      });

      this._resetChart();
      const textColor = this._chartTextColor();
      _chart.setOption({
        tooltip: { trigger: "axis", confine: true, valueFormatter: (v) => this.fmt(v) },
        legend: {
          data: this.multiSeries.map((item) => item.label),
          top: 0,
          selectedMode: true,
          textStyle: { color: textColor },
        },
        grid: { left: 16, right: 24, top: 50, bottom: 30, containLabel: true },
        xAxis: { type: "category", data: dates, axisLabel: { color: textColor } },
        yAxis: { type: "value", axisLabel: { color: textColor } },
        series,
      }, true);
    },

    renderGrid() {
      if (!this.grid || !this.series) return;
      const rows = this.series.baseline.map((r) => ({
        ds: r.ds,
        baseline: r.yhat,
        ajuste: null,
        final: r.yhat,
      }));
      this.grid.setGridOption("rowData", rows);
      // AG Grid às vezes só reavalia o overlay "No Rows" no próximo tick do
      // seu próprio ciclo de refresh; chamar hideOverlay() no mesmo tick de
      // setGridOption perde a corrida e o overlay antigo fica preso na tela.
      setTimeout(() => {
        if (rows.length) this.grid.hideOverlay();
        else this.grid.showNoRowsOverlay();
      }, 0);
    },

    fmt(v) {
      if (v === null || v === undefined) return "N/D";
      return this.fmtNum(v);
    },
    fmtMonth(iso) {
      if (!iso) return "";
      const [y, m] = iso.split("-");
      return `${m}/${y}`;
    },
    matHint() {
      const h = this.series && this.series.history;
      if (!h || !h.length) return "";
      if (this.series.kpis.mat === null) return "Precisa de 12 meses de histórico.";
      return `Soma dos 12 meses observados até ${this.fmtMonth(h[h.length - 1].ds)}.`;
    },
    fmtPct(v) {
      if (v === null || v === undefined) return "N/D";
      return `${(v * 100).toFixed(1)}%`;
    },
    fmtNum(v) {
      if (v === null || v === undefined) return "N/D";
      return Number(v).toLocaleString("pt-BR", { maximumFractionDigits: 1 });
    },
  };
}
