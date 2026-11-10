(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const AGENT_LABEL = {
    planner: "规划",
    researcher: "研究",
    critic: "审阅",
    writer: "写作",
    system: "系统",
  };
  const AGENT_DEFAULT = {
    planner: "拆解研究问题",
    researcher: "检索与阅读",
    critic: "找证据缺口",
    writer: "综合与引用",
  };
  const STAGE_ORDER = ["planning", "research", "review", "writing", "done"];
  const STATUS_LABEL = { queued: "排队中", running: "研究中", done: "已完成", error: "失败", cancelled: "已取消" };
  const PROVIDER_LABEL = {
    auto: "自动",
    so360: "360 搜索",
    sogou: "搜狗",
    bing: "Bing",
    tavily: "Tavily",
    offline: "离线",
  };
  const CITE_RE = /\[(S\d+)\]/g;

  const state = {
    config: {},
    view: "hero",
    runId: null,
    run: null,
    status: "idle",
    deep: false,
    starting: false,
    events: [],
    seenEvents: new Set(),
    sourceMap: new Map(),
    notes: [],
    questions: [],
    reportBuffer: "",
    report: "",
    stats: null,
    es: null,
    streamErrorShown: false,
    startedAt: null,
    timer: null,
    renderTimer: null,
    toastTimer: null,
    activeTab: "report",
  };

  const el = {};

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    cacheElements();
    bindEvents();
    fetchConfig();

    const params = new URLSearchParams(window.location.search);
    const runId = params.get("run");
    if (runId) {
      loadRun(runId);
    }
  }

  function cacheElements() {
    [
      "hero", "workspace", "researchForm", "queryInput", "submitBtn", "examples",
      "navWorkbench", "navHistory", "navArchitecture", "modeChip", "modeText",
      "settingsBtn", "demoNotice", "demoSettingsBtn", "runQuery", "runIdShort",
      "runMeta", "newRunBtn", "downloadBtn", "copyBtn", "cancelBtn", "progressLabel",
      "progressFill", "stepper", "agentCount", "log", "sourceCount", "noteCount",
      "reportMeta", "reportPlaceholder", "questionChips", "reportContent",
      "citationBar", "sourcesList", "notesList", "historyDrawer", "drawerOverlay",
      "historyList", "closeHistory", "settingsModal", "closeSettings", "cancelSettings",
      "saveSettings", "demoModeBtn", "baseUrlInput", "modelInput", "apiKeyInput",
      "tavilyKeyInput", "searchProviderInput", "toggleKey", "settingsStatus",
      "archModal", "closeArch", "toast",
    ].forEach((id) => { el[id] = document.getElementById(id); });
  }

  function bindEvents() {
    el.researchForm.addEventListener("submit", startResearch);
    el.queryInput.addEventListener("input", onQueryInput);
    el.queryInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        if (el.queryInput.value.trim()) startResearch(event);
      }
    });

    el.examples.addEventListener("click", (event) => {
      const chip = event.target.closest(".example-chip");
      if (!chip) return;
      el.queryInput.value = chip.dataset.query || "";
      onQueryInput();
      el.queryInput.focus();
    });

    $$(".depth-opt").forEach((button) => {
      button.addEventListener("click", () => {
        $$(".depth-opt").forEach((b) => b.classList.toggle("active", b === button));
        state.deep = button.dataset.deep === "true";
      });
    });

    el.settingsBtn.addEventListener("click", openSettings);
    el.demoSettingsBtn.addEventListener("click", openSettings);
    el.closeSettings.addEventListener("click", closeSettings);
    el.cancelSettings.addEventListener("click", closeSettings);
    el.saveSettings.addEventListener("click", saveSettings);
    el.demoModeBtn.addEventListener("click", enableDemoMode);
    el.toggleKey.addEventListener("click", toggleKeyVisibility);

    el.navHistory.addEventListener("click", openHistory);
    el.closeHistory.addEventListener("click", closeHistory);
    el.drawerOverlay.addEventListener("click", closeHistory);
    el.navWorkbench.addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));
    el.navArchitecture.addEventListener("click", () => el.archModal.classList.remove("hidden"));
    el.closeArch.addEventListener("click", () => el.archModal.classList.add("hidden"));
    el.archModal.addEventListener("click", (event) => {
      if (event.target === el.archModal) el.archModal.classList.add("hidden");
    });
    el.settingsModal.addEventListener("click", (event) => {
      if (event.target === el.settingsModal) closeSettings();
    });

    el.newRunBtn.addEventListener("click", newResearch);
    el.copyBtn.addEventListener("click", copyReport);
    el.downloadBtn.addEventListener("click", downloadReport);
    el.cancelBtn.addEventListener("click", cancelRun);

    $$(".tab").forEach((tab) => tab.addEventListener("click", () => switchTab(tab.dataset.tab)));
    el.reportContent.addEventListener("click", (event) => {
      const cite = event.target.closest("a.cite");
      if (cite) {
        event.preventDefault();
        openSource(cite.dataset.cite);
      }
    });
    el.sourcesList.addEventListener("click", (event) => {
      const link = event.target.closest("a.source-title");
      if (link) return;
      const card = event.target.closest(".source-card");
      if (card) card.classList.toggle("expanded");
    });

    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (!el.settingsModal.classList.contains("hidden")) closeSettings();
      else if (!el.archModal.classList.contains("hidden")) el.archModal.classList.add("hidden");
      else if (el.historyDrawer.classList.contains("open")) closeHistory();
    });
  }

  // ── config & mode ─────────────────────────────────────────
  async function fetchConfig() {
    try {
      state.config = await api("/api/config");
      updateModeChip();
      if (state.config.mode === "mock") el.demoNotice.classList.remove("hidden");
    } catch (error) {
      el.modeChip.classList.remove("loading");
      el.modeChip.classList.add("demo");
      el.modeText.textContent = "后端未连接";
      toast("无法连接后端服务，请确认 python run.py 已启动", "error");
    }
  }

  function updateModeChip() {
    const config = state.config || {};
    el.modeChip.classList.remove("loading", "live", "demo");
    if (!config.mode) {
      el.modeChip.classList.add("loading");
      el.modeText.textContent = "连接中";
      return;
    }
    if (config.mode === "live") {
      el.modeChip.classList.add("live");
      el.modeText.textContent = `已接入 · ${config.model}`;
      el.modeChip.title = `${config.model} @ ${config.base_url}`;
      el.demoNotice.classList.add("hidden");
    } else {
      el.modeChip.classList.add("demo");
      el.modeText.textContent = "演示模式";
      el.modeChip.title = "未配置 API Key，当前使用内置模拟模型";
      el.demoNotice.classList.remove("hidden");
    }
  }

  // ── research lifecycle ────────────────────────────────────
  function onQueryInput() {
    const input = el.queryInput;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
    el.submitBtn.disabled = !input.value.trim();
  }

  async function startResearch(event) {
    event?.preventDefault();
    const query = el.queryInput.value.trim();
    if (!query || state.starting) return;

    state.starting = true;
    el.submitBtn.disabled = true;
    closeStream();
    clearTimer();
    resetWorkspace();
    showWorkspace(query);

    try {
      const response = await api("/api/research", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, deep: state.deep }),
      });
      state.runId = response.run_id;
      state.run = { id: response.run_id, query, mode: response.mode, model: response.model, deep: state.deep };
      el.runIdShort.textContent = response.run_id;
      history.replaceState(null, "", `?run=${response.run_id}`);
      state.status = "running";
      renderRunMeta();
      startTimer();
      connectEvents(response.run_id);
      appendSyntheticLog("system", "已创建研究任务，连接 Agent 事件流");
    } catch (error) {
      state.status = "error";
      renderRunMeta();
      setPlaceholderError(error.message);
      toast(error.message, "error");
    } finally {
      state.starting = false;
      el.submitBtn.disabled = !el.queryInput.value.trim();
    }
  }

  function showWorkspace(query) {
    state.view = "workspace";
    el.hero.classList.add("hidden");
    el.workspace.classList.remove("hidden");
    el.runQuery.textContent = query;
    el.runIdShort.textContent = "··";
    el.cancelBtn.classList.remove("hidden");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function resetWorkspace() {
    state.events = [];
    state.seenEvents = new Set();
    state.sourceMap = new Map();
    state.notes = [];
    state.questions = [];
    state.reportBuffer = "";
    state.report = "";
    state.stats = null;
    state.status = "queued";
    state.streamErrorShown = false;
    if (state.renderTimer) {
      clearTimeout(state.renderTimer);
      state.renderTimer = null;
    }

    el.log.innerHTML = "";
    el.sourcesList.innerHTML = '<div class="empty-state">来源会随检索实时出现</div>';
    el.notesList.innerHTML = '<div class="empty-state">研究员完成阅读后会生成结构化笔记</div>';
    el.questionChips.innerHTML = "";
    el.reportContent.innerHTML = "";
    el.reportContent.classList.add("hidden");
    el.reportContent.classList.remove("streaming");
    el.reportPlaceholder.classList.remove("hidden");
    el.reportPlaceholder.querySelector("p").textContent = "研究团队正在启动…";
    el.citationBar.classList.add("hidden");
    el.reportMeta.innerHTML = "";
    el.sourceCount.textContent = "0";
    el.noteCount.textContent = "0";
    el.downloadBtn.disabled = true;
    el.copyBtn.disabled = true;
    el.cancelBtn.disabled = false;
    el.cancelBtn.classList.remove("hidden");

    setProgress(0);
    setStage("planning", 0, true);
    $$(".agent-row").forEach((row) => {
      const agent = row.dataset.agent;
      row.classList.remove("active");
      const dot = $(".status-dot", row);
      dot.className = "status-dot idle";
      $(".agent-info span", row).textContent = AGENT_DEFAULT[agent];
    });
    switchTab("report");
    renderRunMeta();
  }

  function newResearch() {
    closeStream();
    clearTimer();
    resetWorkspace();
    state.runId = null;
    state.run = null;
    state.status = "idle";
    state.view = "hero";
    el.workspace.classList.add("hidden");
    el.hero.classList.remove("hidden");
    history.replaceState(null, "", window.location.pathname);
    window.scrollTo({ top: 0, behavior: "smooth" });
    el.queryInput.focus();
  }

  async function cancelRun() {
    if (!state.runId) return;
    el.cancelBtn.disabled = true;
    try {
      await api(`/api/runs/${state.runId}/cancel`, { method: "POST" });
      appendSyntheticLog("system", "已发送取消请求");
    } catch (error) {
      el.cancelBtn.disabled = false;
      toast(error.message, "error");
    }
  }

  function connectEvents(runId) {
    closeStream();
    const source = new EventSource(`/api/runs/${runId}/events`);
    state.es = source;
    source.onopen = () => { state.streamErrorShown = false; };
    source.onmessage = (message) => {
      try {
        handleEvent(JSON.parse(message.data));
      } catch (error) {
        console.warn("bad event", error);
      }
    };
    source.onerror = () => {
      if (["done", "error", "cancelled"].includes(state.status)) {
        closeStream();
        return;
      }
      if (!state.streamErrorShown) {
        state.streamErrorShown = true;
        appendSyntheticLog("system", "事件流中断，浏览器正在自动重连…");
      }
    };
  }

  function closeStream() {
    if (state.es) {
      state.es.close();
      state.es = null;
    }
  }

  function handleEvent(event) {
    if (!event || !event.type) return;
    if (event.type !== "token") {
      if (state.seenEvents.has(event.seq)) return;
      state.seenEvents.add(event.seq);
      state.events.push(event);
    }

    const data = event.data || {};
    switch (event.type) {
      case "stage":
        setStage(data.stage, data.progress);
        appendLog(event);
        break;
      case "agent_status":
        setAgentStatus(event.agent, data.status, data.detail);
        break;
      case "question":
        addQuestion(event.question || data.question);
        appendLog(event);
        break;
      case "source":
        addSource(data.source);
        appendLog(event);
        break;
      case "reading":
        updateSourceReading(data);
        appendLog(event);
        break;
      case "note":
        addNote(data.note);
        appendLog(event);
        break;
      case "critique":
        if (data.gaps?.length) {
          event.message = `${event.message}（缺口：${data.gaps.join("；")}）`;
        }
        appendLog(event);
        break;
      case "warning":
        appendLog(event);
        break;
      case "citation_check":
        state.stats = data.stats;
        appendLog(event);
        if (state.report) showCitationBar(state.stats);
        break;
      case "token":
        state.reportBuffer += data.text || "";
        if (el.reportContent.classList.contains("hidden")) {
          el.reportContent.classList.remove("hidden");
          el.reportPlaceholder.classList.add("hidden");
        }
        scheduleReportRender();
        break;
      case "report": {
        cancelScheduledRender();
        const reportBody = $(".report-body");
        const wasFollowing = reportBody.scrollHeight - reportBody.scrollTop - reportBody.clientHeight < 200;
        state.report = data.report || state.reportBuffer;
        state.stats = data.stats || state.stats;
        renderReport(state.report, false);
        showCitationBar(state.stats);
        el.reportContent.classList.remove("streaming");
        el.downloadBtn.disabled = false;
        el.copyBtn.disabled = false;
        updateReportMeta(state.stats);
        appendLog(event);
        if (wasFollowing) reportBody.scrollTop = 0;
        break;
      }
      case "done":
        finishRun(data);
        break;
      case "error":
        failRun(event.message || "研究失败");
        break;
      case "cancelled":
        state.status = "cancelled";
        closeStream();
        cancelScheduledRender();
        clearTimer();
        el.cancelBtn.classList.add("hidden");
        renderRunMeta();
        appendLog(event);
        toast("任务已取消", "error");
        break;
      case "stream_end":
        closeStream();
        if (state.status === "running" && !state.report) {
          refreshSnapshot();
        }
        break;
      case "run_started":
        if (data.mode === "mock") {
          appendSyntheticLog("system", "演示模式已启用：检索真实执行，报告由模拟模型生成");
        }
        break;
      default:
        break;
    }
  }

  async function refreshSnapshot() {
    if (!state.runId) return;
    try {
      const snapshot = await api(`/api/runs/${state.runId}`);
      if (snapshot.report) {
        state.report = snapshot.report;
        state.stats = snapshot.stats || null;
        renderReport(state.report, false);
        showCitationBar(state.stats);
        updateReportMeta(state.stats);
        el.downloadBtn.disabled = false;
        el.copyBtn.disabled = false;
      }
      if (["done", "error", "cancelled"].includes(snapshot.status)) {
        finishRun({ stats: snapshot.stats, duration_seconds: snapshot.stats?.duration_seconds });
      }
    } catch (error) {
      console.warn(error);
    }
  }

  function finishRun(data) {
    state.status = "done";
    state.stats = { ...(state.stats || {}), ...(data.stats || {}) };
    closeStream();
    cancelScheduledRender();
    clearTimer();
    setStage("done", 100);
    el.cancelBtn.classList.add("hidden");
    el.downloadBtn.disabled = !state.report;
    el.copyBtn.disabled = !state.report;
    renderRunMeta(data.duration_seconds);
    updateReportMeta(state.stats);
    if (state.report) {
      showCitationBar(state.stats);
      el.reportContent.classList.remove("streaming");
    } else {
      refreshSnapshot();
    }
    $$(".agent-row").forEach((row) => {
      if ($(".status-dot", row).classList.contains("thinking")) {
        setAgentStatus(row.dataset.agent, "done", "任务完成");
      }
    });
    toast("研究完成，报告已生成", "success");
  }

  function failRun(message) {
    state.status = "error";
    closeStream();
    cancelScheduledRender();
    clearTimer();
    el.cancelBtn.classList.add("hidden");
    renderRunMeta();
    setPlaceholderError(message);
    $$(".agent-row").forEach((row) => {
      if ($(".status-dot", row).classList.contains("thinking")) {
        setAgentStatus(row.dataset.agent, "error", "执行失败");
      }
    });
    appendSyntheticLog("system", message, "error");
    toast(message, "error");
  }

  function setPlaceholderError(message) {
    el.reportPlaceholder.classList.remove("hidden");
    el.reportContent.classList.add("hidden");
    el.reportPlaceholder.querySelector("p").textContent = message || "研究失败，请重试";
  }

  // ── rendering: run meta, stages, agents ───────────────────
  function formatDuration(seconds) {
    if (seconds === undefined || seconds === null) return "";
    const total = Math.max(0, Math.round(Number(seconds)));
    if (total < 60) return `${total}s`;
    return `${Math.floor(total / 60)}m ${total % 60}s`;
  }

  function currentElapsed() {
    if (!state.startedAt) return null;
    return (Date.now() - state.startedAt) / 1000;
  }

  function renderRunMeta(duration) {
    const map = { queued: "queued", running: "running", done: "done", error: "error", cancelled: "cancelled" };
    const statusClass = map[state.status] || "queued";
    const label = STATUS_LABEL[state.status] || "准备中";
    const elapsed = duration !== undefined && duration !== null
      ? formatDuration(duration)
      : (state.status === "running" ? formatDuration(currentElapsed()) : "");
    const modeTag = state.run?.mode === "mock" ? "演示" : (state.run?.mode === "live" ? "实时模型" : "");
    el.runMeta.innerHTML = [
      `<span class="run-status ${statusClass}"><span class="run-dot"></span>${label}</span>`,
      `<span>${state.sourceMap.size} 来源</span>`,
      modeTag ? `<span>${modeTag}</span>` : "",
      elapsed ? `<span>${elapsed}</span>` : "",
    ].filter(Boolean).join("");
  }

  function startTimer() {
    clearTimer();
    state.timer = setInterval(() => {
      if (state.status === "running") renderRunMeta();
    }, 1000);
  }

  function clearTimer() {
    if (state.timer) {
      clearInterval(state.timer);
      state.timer = null;
    }
  }

  function setProgress(percent) {
    const value = Math.max(0, Math.min(100, Number(percent) || 0));
    el.progressFill.style.width = `${value}%`;
    el.progressLabel.textContent = `${Math.round(value)}%`;
  }

  function setStage(stage, progress, quiet = false) {
    const index = Math.max(0, STAGE_ORDER.indexOf(stage));
    $$(".step", el.stepper).forEach((step, stepIndex) => {
      step.classList.toggle("done", stepIndex < index || stage === "done");
      step.classList.toggle("active", stepIndex === index && stage !== "done");
    });
    if (!quiet) {
      if (progress !== undefined) setProgress(progress);
      else setProgress(stage === "done" ? 100 : ((index + 1) / STAGE_ORDER.length) * 100);
    }
  }

  function setAgentStatus(agent, status, detail) {
    const row = $(`.agent-row[data-agent="${agent}"]`);
    if (!row) return;
    const dot = $(".status-dot", row);
    const text = $(".agent-info span", row);
    row.classList.toggle("active", status === "thinking");
    dot.className = `status-dot ${status === "thinking" ? "thinking" : status === "done" ? "done" : status === "error" ? "error" : "idle"}`;
    if (detail) text.textContent = detail;
    else if (status === "idle") text.textContent = AGENT_DEFAULT[agent];
  }

  // ── rendering: questions, sources, notes, log ────────────
  function addQuestion(question) {
    if (!question || !question.id) return;
    if (state.questions.some((q) => q.id === question.id)) return;
    state.questions.push(question);
    const chip = document.createElement("div");
    chip.className = "question-chip";
    chip.dataset.kind = question.kind || "plan";
    chip.textContent = `${question.id} · ${question.question}`;
    chip.title = question.question;
    el.questionChips.appendChild(chip);
  }

  function addSource(source) {
    if (!source || !source.id || state.sourceMap.has(source.id)) return;
    state.sourceMap.set(source.id, source);
    el.sourceCount.textContent = String(state.sourceMap.size);
    const empty = $(".empty-state", el.sourcesList);
    if (empty) empty.remove();

    const card = document.createElement("div");
    card.className = `source-card${source.fetched ? "" : " unfetched"}`;
    card.id = `src-${source.id}`;

    const idBadge = document.createElement("span");
    idBadge.className = "source-id";
    idBadge.textContent = source.id;

    const main = document.createElement("div");
    main.className = "source-main";

    const external = /^https?:\/\//i.test(source.url || "");
    const title = document.createElement(external ? "a" : "span");
    title.className = "source-title";
    title.textContent = source.title || source.url;
    if (external) {
      title.href = source.url;
      title.target = "_blank";
      title.rel = "noopener noreferrer";
    }
    main.appendChild(title);

    const sub = document.createElement("div");
    sub.className = "source-sub";
    const readState = document.createElement("span");
    readState.className = `read-state${source.fetched ? " ok" : ""}`;
    readState.textContent = source.fetched ? `已阅读 ${formatNum(source.chars)} 字` : "待阅读";
    sub.appendChild(readState);
    const domain = document.createElement("span");
    domain.textContent = source.domain || "";
    sub.appendChild(domain);
    main.appendChild(sub);

    if (source.snippet) {
      const snippet = document.createElement("div");
      snippet.className = "source-snippet";
      snippet.textContent = source.snippet;
      main.appendChild(snippet);
    }

    const ext = document.createElement(external ? "a" : "span");
    ext.className = "external-btn";
    if (external) {
      ext.href = source.url;
      ext.target = "_blank";
      ext.rel = "noopener noreferrer";
    }
    ext.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M7 17L17 7M9 7h8v8"/></svg>';

    card.append(idBadge, main, ext);
    el.sourcesList.appendChild(card);
  }

  function updateSourceReading(data) {
    if (!data || !data.source_id) return;
    const card = document.getElementById(`src-${data.source_id}`);
    const cached = state.sourceMap.get(data.source_id);
    if (cached) {
      cached.fetched = Boolean(data.ok);
      cached.chars = data.chars || 0;
    }
    if (!card) return;
    const readState = $(".read-state", card);
    if (!readState) return;
    if (data.ok) {
      card.classList.remove("unfetched");
      readState.classList.add("ok");
      readState.textContent = `已阅读 ${formatNum(data.chars)} 字`;
    } else {
      readState.classList.remove("ok");
      readState.textContent = "抓取失败，仅使用摘要";
    }
  }

  function addNote(note) {
    if (!note || !note.question_id) return;
    if (state.notes.some((n) => n.question_id === note.question_id)) return;
    state.notes.push(note);
    el.noteCount.textContent = String(state.notes.length);
    const empty = $(".empty-state", el.notesList);
    if (empty) empty.remove();

    const card = document.createElement("div");
    card.className = "note-card";

    const head = document.createElement("div");
    head.className = "note-head";
    const id = document.createElement("span");
    id.className = "note-id";
    id.textContent = note.question_id;
    const question = document.createElement("div");
    question.className = "note-q";
    question.textContent = note.question || "";
    head.append(id, question);

    const summary = document.createElement("p");
    summary.className = "note-summary";
    summary.textContent = note.summary || "";

    card.append(head, summary);

    if (note.key_points?.length) {
      const list = document.createElement("ul");
      list.className = "note-points";
      note.key_points.forEach((point) => {
        const item = document.createElement("li");
        item.textContent = point;
        list.appendChild(item);
      });
      card.appendChild(list);
    }

    if (note.source_ids?.length) {
      const cites = document.createElement("div");
      cites.className = "note-cites";
      note.source_ids.forEach((sid) => {
        const cite = document.createElement("span");
        cite.className = "note-cite";
        cite.textContent = sid;
        cites.appendChild(cite);
      });
      card.appendChild(cites);
    }
    el.notesList.appendChild(card);
  }

  function appendLog(event) {
    if (!event || ["token", "report", "agent_status", "stream_end", "run_started", "done"].includes(event.type)) return;
    let message = event.message || "";
    if (event.type === "citation_check" && event.data?.stats) {
      const stats = event.data.stats;
      message = `引用校验完成：${stats.cited_sources}/${stats.valid_sources} 个来源被引用，${Math.round((stats.grounded_ratio || 0) * 100)}% 内容行有来源支撑`;
    }
    if (!message) return;
    appendLogEntry(event.agent || "system", message, event.type === "warning" ? "warning" : event.type, event.ts);
  }

  function appendSyntheticLog(agent, message, kind = "log") {
    appendLogEntry(agent, message, kind, new Date().toISOString());
  }

  function appendLogEntry(agent, message, kind, ts) {
    const entry = document.createElement("div");
    entry.className = "log-entry";
    entry.dataset.kind = kind;

    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = formatClock(ts);

    const tag = document.createElement("span");
    tag.className = "log-tag";
    tag.dataset.agent = agent;
    tag.textContent = AGENT_LABEL[agent] || "系统";

    const text = document.createElement("span");
    text.className = "log-msg";
    text.textContent = message;

    entry.append(time, tag, text);
    const nearBottom = el.log.scrollHeight - el.log.scrollTop - el.log.clientHeight < 80;
    el.log.appendChild(entry);
    while (el.log.children.length > 400) el.log.removeChild(el.log.firstChild);
    if (nearBottom) el.log.scrollTop = el.log.scrollHeight;
  }

  function rebuildLog(events) {
    el.log.innerHTML = "";
    events.forEach((event) => {
      if (event.type === "token") return;
      if (event.type === "question") {
        appendSyntheticLog(event.agent || "planner", event.message, "log");
        return;
      }
      appendLog(event);
    });
  }

  // ── report rendering ──────────────────────────────────────
  function scheduleReportRender() {
    if (state.renderTimer) return;
    state.renderTimer = setTimeout(() => {
      state.renderTimer = null;
      if (["done", "error", "cancelled"].includes(state.status)) return;
      renderReport(state.reportBuffer, true);
      updateStreamingMeta();
    }, 90);
  }

  function cancelScheduledRender() {
    if (state.renderTimer) {
      clearTimeout(state.renderTimer);
      state.renderTimer = null;
    }
  }

  function renderReport(markdown, streaming) {
    if (!markdown) return;
    const body = $(".report-body");
    const nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 160;
    el.reportContent.innerHTML = renderMarkdown(markdown);
    el.reportContent.classList.remove("hidden");
    el.reportPlaceholder.classList.add("hidden");
    if (streaming) el.reportContent.classList.add("streaming");
    enhanceExternalLinks(el.reportContent);
    if (streaming && nearBottom) {
      requestAnimationFrame(() => { body.scrollTop = body.scrollHeight; });
    }
  }

  function renderMarkdown(markdown) {
    if (!window.marked || !window.DOMPurify) {
      const div = document.createElement("div");
      div.textContent = markdown;
      return `<p>${div.innerHTML.replace(/\n/g, "<br>")}</p>`;
    }
    let html;
    try {
      html = window.marked.parse(markdown, { gfm: true, breaks: false, headerIds: false, mangle: false });
    } catch (error) {
      const div = document.createElement("div");
      div.textContent = markdown;
      html = `<p>${div.innerHTML.replace(/\n/g, "<br>")}</p>`;
    }
    const clean = window.DOMPurify.sanitize(html, { ADD_ATTR: ["target"] });
    const wrapper = document.createElement("div");
    wrapper.innerHTML = clean;
    linkifyCitations(wrapper);
    return wrapper.innerHTML;
  }

  function linkifyCitations(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const parent = node.parentElement;
        if (!parent) return NodeFilter.FILTER_REJECT;
        if (["A", "CODE", "PRE", "SCRIPT", "STYLE"].includes(parent.tagName)) return NodeFilter.FILTER_REJECT;
        CITE_RE.lastIndex = 0;
        return CITE_RE.test(node.nodeValue || "") ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      },
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach((node) => {
      const text = node.nodeValue || "";
      const fragment = document.createDocumentFragment();
      let last = 0;
      CITE_RE.lastIndex = 0;
      let match;
      while ((match = CITE_RE.exec(text))) {
        if (match.index > last) fragment.appendChild(document.createTextNode(text.slice(last, match.index)));
        const link = document.createElement("a");
        link.className = "cite";
        link.href = `#src-${match[1]}`;
        link.dataset.cite = match[1];
        link.title = `查看来源 ${match[1]}`;
        link.textContent = `[${match[1]}]`;
        fragment.appendChild(link);
        last = CITE_RE.lastIndex;
      }
      if (last < text.length) fragment.appendChild(document.createTextNode(text.slice(last)));
      node.replaceWith(fragment);
    });
  }

  function enhanceExternalLinks(root) {
    $$('a[href^="http"]', root).forEach((link) => {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    });
  }

  function updateStreamingMeta() {
    if (!state.reportBuffer) return;
    el.reportMeta.innerHTML = `<span><b>${formatNum(state.reportBuffer.length)}</b> 字</span><span>写作中…</span>`;
  }

  function updateReportMeta(stats) {
    if (!stats) {
      el.reportMeta.innerHTML = "";
      return;
    }
    const invalid = Math.max((stats.invalid_citation_ids || []).length, (stats.invalid_citations || []).length);
    el.reportMeta.innerHTML = [
      `<span><b>${formatNum(stats.char_count)}</b> 字</span>`,
      `<span><b>${stats.cited_sources || 0}/${stats.valid_sources || 0}</b> 来源</span>`,
      `<span><b>${Math.round((stats.grounded_ratio || 0) * 100)}%</b> 有据</span>`,
      invalid ? `<span class="warn"><b>${invalid}</b> 无效引用</span>` : "",
      stats.duration_seconds ? `<span>${formatDuration(stats.duration_seconds)}</span>` : "",
    ].filter(Boolean).join("");
  }

  function showCitationBar(stats) {
    if (!stats) return;
    const invalid = Math.max((stats.invalid_citation_ids || []).length, (stats.invalid_citations || []).length);
    el.citationBar.innerHTML = [
      '<span class="check">✓</span>',
      `<span>引用回溯 <strong>${stats.cited_sources || 0}/${stats.valid_sources || 0}</strong> 个来源</span>`,
      `<span><strong>${Math.round((stats.grounded_ratio || 0) * 100)}%</strong> 内容行有来源支撑</span>`,
      invalid ? `<span class="warn">${invalid} 个无效编号已移除</span>` : "<span>未发现无效引用</span>",
      `<span>共 ${formatNum(stats.source_count ?? stats.valid_sources)} 条检索来源</span>`,
    ].join("");
    el.citationBar.classList.remove("hidden");
  }

  function switchTab(name) {
    state.activeTab = name;
    $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
    $$(".tab-pane").forEach((pane) => pane.classList.toggle("active", pane.id === `tab-${name}`));
    const body = $(".report-body");
    if (body) body.scrollTop = 0;
  }

  function openSource(sourceId) {
    switchTab("sources");
    const card = document.getElementById(`src-${sourceId}`);
    if (!card) {
      toast(`来源 ${sourceId} 不在本次返回结果中`, "error");
      return;
    }
    setTimeout(() => {
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      card.classList.remove("flash");
      void card.offsetWidth;
      card.classList.add("flash");
    }, 60);
  }

  async function copyReport() {
    if (!state.report) return;
    try {
      await navigator.clipboard.writeText(state.report);
      toast("报告已复制到剪贴板", "success");
    } catch {
      const area = document.createElement("textarea");
      area.value = state.report;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
      toast("报告已复制", "success");
    }
  }

  function downloadReport() {
    if (!state.report) return;
    const blob = new Blob([state.report], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const safeQuery = (state.run?.query || "research").slice(0, 24).replace(/[\\/:*?"<>|]/g, "");
    link.href = url;
    link.download = `deepscope-${safeQuery}.md`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
  }

  // ── history ───────────────────────────────────────────────
  async function openHistory() {
    el.drawerOverlay.classList.remove("hidden");
    el.historyDrawer.classList.add("open");
    el.historyList.innerHTML = '<div class="empty-state">加载中…</div>';
    try {
      const data = await api("/api/runs?limit=30");
      renderHistory(data.runs || []);
    } catch (error) {
      el.historyList.innerHTML = "";
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = error.message;
      el.historyList.appendChild(empty);
    }
  }

  function closeHistory() {
    el.drawerOverlay.classList.add("hidden");
    el.historyDrawer.classList.remove("open");
  }

  function renderHistory(runs) {
    el.historyList.innerHTML = "";
    if (!runs.length) {
      el.historyList.innerHTML = '<div class="empty-state">还没有运行记录</div>';
      return;
    }
    runs.forEach((run) => {
      const item = document.createElement("button");
      item.className = "history-item";
      if (run.id === state.runId) item.style.borderColor = "rgba(94,234,212,.3)";

      const query = document.createElement("div");
      query.className = "history-query";
      query.textContent = run.query || "未命名研究";

      const meta = document.createElement("div");
      meta.className = "history-meta";
      const status = document.createElement("span");
      status.className = `status-pill ${run.status}`;
      status.textContent = STATUS_LABEL[run.status] || run.status;
      const mode = document.createElement("span");
      mode.textContent = run.mode === "mock" ? "演示" : "实时";
      const sources = document.createElement("span");
      sources.textContent = `${run.sources || 0} 来源`;
      const time = document.createElement("span");
      time.textContent = formatDate(run.created_at);
      meta.append(status, mode, sources, time);

      item.append(query, meta);
      if (run.report_excerpt) {
        const excerpt = document.createElement("div");
        excerpt.className = "history-excerpt";
        excerpt.textContent = run.report_excerpt.replace(/[#>*`]/g, "").slice(0, 120);
        item.appendChild(excerpt);
      }
      item.addEventListener("click", () => {
        closeHistory();
        loadRun(run.id);
      });
      el.historyList.appendChild(item);
    });
  }

  async function loadRun(runId) {
    closeStream();
    clearTimer();
    resetWorkspace();
    state.runId = runId;
    state.status = "queued";
    el.hero.classList.add("hidden");
    el.workspace.classList.remove("hidden");
    el.runQuery.textContent = "加载中…";
    el.runIdShort.textContent = runId;
    history.replaceState(null, "", `?run=${runId}`);

    try {
      const snapshot = await api(`/api/runs/${runId}`);
      state.run = {
        id: snapshot.id,
        query: snapshot.query,
        mode: snapshot.mode,
        model: snapshot.model,
        deep: snapshot.deep,
      };
      state.status = snapshot.status || "done";
      state.stats = snapshot.stats || null;
      el.runQuery.textContent = snapshot.query || "未命名研究";
      el.runIdShort.textContent = snapshot.id;

      (snapshot.sources || []).forEach(addSource);
      (snapshot.plan || []).forEach(addQuestion);
      (snapshot.notes || []).forEach(addNote);
      rebuildLog(snapshot.events || []);

      if (snapshot.report) {
        state.report = snapshot.report;
        renderReport(snapshot.report, false);
        updateReportMeta(state.stats);
        showCitationBar(state.stats);
        el.downloadBtn.disabled = false;
        el.copyBtn.disabled = false;
      }

      const stageEvents = (snapshot.events || []).filter((event) => event.type === "stage");
      const lastStage = stageEvents.length ? stageEvents[stageEvents.length - 1].data?.stage : "planning";
      if (snapshot.status === "done") setStage("done", 100);
      else if (snapshot.status === "error" || snapshot.status === "cancelled") setStage(lastStage || "planning");
      else setStage(lastStage || "planning", 10);

      inferAgentStatuses(snapshot.events || [], snapshot.status);

      if (snapshot.status === "running" || snapshot.status === "queued") {
        state.startedAt = Date.parse(snapshot.started_at || snapshot.created_at) || Date.now();
        el.cancelBtn.classList.remove("hidden");
        startTimer();
        connectEvents(runId);
      } else {
        el.cancelBtn.classList.add("hidden");
        state.startedAt = null;
      }
      renderRunMeta(state.stats?.duration_seconds);
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (error) {
      toast(error.message, "error");
      newResearch();
    }
  }

  function inferAgentStatuses(events, status) {
    if (status === "done") {
      ["planner", "researcher", "critic", "writer"].forEach((agent) => setAgentStatus(agent, "done", "已完成"));
      return;
    }
    if (status === "error") {
      ["planner", "researcher", "critic", "writer"].forEach((agent) => setAgentStatus(agent, "error", "执行中断"));
      return;
    }
    const lastStatus = new Map();
    events.forEach((event) => {
      if (event.type === "agent_status") lastStatus.set(event.agent, event.data);
    });
    Object.keys(AGENT_DEFAULT).forEach((agent) => {
      const info = lastStatus.get(agent);
      if (info) setAgentStatus(agent, info.status, info.detail);
    });
  }

  // ── settings ──────────────────────────────────────────────
  function openSettings() {
    el.settingsModal.classList.remove("hidden");
    fillSettingsForm();
  }

  function closeSettings() {
    el.settingsModal.classList.add("hidden");
  }

  function fillSettingsForm() {
    const config = state.config || {};
    el.baseUrlInput.value = config.base_url || "";
    el.modelInput.value = config.model || "";
    el.searchProviderInput.value = config.search_provider || "auto";
    el.apiKeyInput.value = "";
    el.tavilyKeyInput.value = "";
    el.apiKeyInput.type = "password";
    el.toggleKey.textContent = "显示";
    el.settingsStatus.className = "modal-note";
    if (config.mode === "live") {
      el.settingsStatus.textContent = `当前：已接入 ${config.model} · 搜索源 ${PROVIDER_LABEL[config.search_provider] || config.search_provider}`;
    } else {
      el.settingsStatus.textContent = "当前：演示模式（未配置 API Key）。填入 Key 并保存后立即切换为真实模型。";
    }
    if (config.api_key_set) {
      el.apiKeyInput.placeholder = "已配置，留空则保持当前 Key";
    } else {
      el.apiKeyInput.placeholder = "sk-...";
    }
    el.tavilyKeyInput.placeholder = config.tavily_key_set ? "已配置，留空保持不变" : "tvly-...（可选）";
  }

  function toggleKeyVisibility() {
    const isPassword = el.apiKeyInput.type === "password";
    el.apiKeyInput.type = isPassword ? "text" : "password";
    el.toggleKey.textContent = isPassword ? "隐藏" : "显示";
  }

  async function saveSettings() {
    const provider = el.searchProviderInput.value;
    const payload = {
      base_url: el.baseUrlInput.value.trim(),
      model: el.modelInput.value.trim(),
      search_provider: provider,
    };
    const key = el.apiKeyInput.value.trim();
    const tavilyKey = el.tavilyKeyInput.value.trim();
    if (key) payload.api_key = key;
    if (tavilyKey) payload.tavily_api_key = tavilyKey;
    if (provider === "tavily" && !tavilyKey && !state.config.tavily_key_set) {
      setSettingsStatus("选择 Tavily 搜索需要填写 Tavily API Key。", "err");
      return;
    }

    el.saveSettings.disabled = true;
    el.saveSettings.textContent = "测试连接中…";
    setSettingsStatus("正在调用模型接口验证配置，请稍候…");
    try {
      const result = await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      state.config = { ...state.config, ...result };
      updateModeChip();
      setSettingsStatus(`连接成功，已切换为 ${result.model} · 搜索源 ${PROVIDER_LABEL[result.search_provider] || result.search_provider}`, "ok");
      toast("模型配置已生效", "success");
      setTimeout(closeSettings, 700);
    } catch (error) {
      setSettingsStatus(error.message, "err");
      toast("配置失败，请检查 Base URL、模型名和 Key", "error");
    } finally {
      el.saveSettings.disabled = false;
      el.saveSettings.textContent = "测试并保存";
    }
  }

  async function enableDemoMode() {
    if (!window.confirm("切换为演示模式将清除当前 API Key（仅本进程），确定继续？")) return;
    try {
      const result = await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: "" }),
      });
      state.config = { ...state.config, ...result };
      updateModeChip();
      setSettingsStatus("已切换为演示模式。", "ok");
      toast("已切换为演示模式", "success");
      setTimeout(closeSettings, 600);
    } catch (error) {
      setSettingsStatus(error.message, "err");
    }
  }

  function setSettingsStatus(message, type = "") {
    el.settingsStatus.className = `modal-note ${type}`.trim();
    el.settingsStatus.textContent = message;
  }

  // ── utils ─────────────────────────────────────────────────
  async function api(path, options) {
    let response;
    try {
      response = await fetch(path, options);
    } catch (error) {
      throw new Error("无法连接后端服务，请确认服务正在运行");
    }
    let body = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    if (!response.ok) {
      throw new Error(body?.detail || body?.message || `请求失败（HTTP ${response.status}）`);
    }
    return body;
  }

  function toast(message, type = "") {
    clearTimeout(state.toastTimer);
    el.toast.textContent = message;
    el.toast.className = `toast show ${type}`.trim();
    state.toastTimer = setTimeout(() => {
      el.toast.className = "toast";
    }, type === "error" ? 5200 : 3000);
  }

  function formatClock(ts) {
    const date = ts ? new Date(ts) : new Date();
    if (Number.isNaN(date.getTime())) return "--:--:--";
    return date.toLocaleTimeString("zh-CN", { hour12: false });
  }

  function formatDate(ts) {
    if (!ts) return "";
    const date = new Date(ts);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  function formatNum(value) {
    const number = Number(value || 0);
    return Number.isFinite(number) ? number.toLocaleString("zh-CN") : "0";
  }
})();
