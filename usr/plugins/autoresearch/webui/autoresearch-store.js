import { createStore } from "/js/AlpineStore.js";
import { callJsonApi } from "/js/api.js";

const PLUGIN_API = "/api/plugins/autoresearch";
const POLL_INTERVAL_MS = 3000;
const DEBOUNCE_MS = 300;
const MAX_TRACE_LINES = 500;

const initial = {
  programMdPath: "",
  starting: false,
  stopping: false,
  pollTimer: null,
  visible: true,
  observer: null,
  panelEl: null,
  startError: "",
  activeRunId: "",
  state: null,
  pastRuns: [],
  pastRunsLoading: false,
  pastRunsExpanded: false,
  trace: [],
  traceSource: null,
  traceExpN: 0,
  traceExhausted: false,
  inputDebounce: null,
};

const model = {
  ...initial,

  onMount(el) {
    this.panelEl = el;
    this.programMdPath = globalThis.localStorage?.getItem("autoresearch.programMdPath") || "";
    this._setupVisibility();
    this.refreshStatus();
  },

  cleanup() {
    this._clearPollTimer();
    this._stopTraceStream();
    if (this.observer) {
      try { this.observer.disconnect(); } catch {}
      this.observer = null;
    }
  },

  _setupVisibility() {
    if (!this.panelEl || typeof IntersectionObserver === "undefined") {
      this._startPolling();
      return;
    }
    this.observer = new IntersectionObserver((entries) => {
      const entry = entries[0];
      this.visible = entry?.isIntersecting ?? true;
      if (this.visible) this._startPolling();
      else this._clearPollTimer();
    }, { threshold: 0.05 });
    this.observer.observe(this.panelEl);
    this._startPolling();
  },

  _startPolling() {
    this._clearPollTimer();
    this.pollTimer = globalThis.setInterval(() => {
      if (!this.visible) return;
      this.refreshStatus();
    }, POLL_INTERVAL_MS);
  },

  _clearPollTimer() {
    if (this.pollTimer) {
      globalThis.clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  },

  onProgramMdInput(value) {
    if (this.inputDebounce) globalThis.clearTimeout(this.inputDebounce);
    this.inputDebounce = globalThis.setTimeout(() => {
      this.programMdPath = String(value || "").trim();
      try {
        globalThis.localStorage?.setItem("autoresearch.programMdPath", this.programMdPath);
      } catch {}
    }, DEBOUNCE_MS);
  },

  canStart() {
    return Boolean(this.programMdPath) && !this.starting && !this.activeRunId;
  },

  async start() {
    if (!this.canStart()) return;
    this.starting = true;
    this.startError = "";
    try {
      const result = await callJsonApi(`${PLUGIN_API}/start`, {
        program_md_path: this.programMdPath,
      });
      if (result?.run_id) {
        this.activeRunId = result.run_id;
        this.trace = [];
        this.traceExpN = 0;
        await this.refreshStatus();
      } else if (result?.error) {
        this.startError = result.error;
      }
    } catch (err) {
      this.startError = String(err?.message || err);
    } finally {
      this.starting = false;
    }
  },

  async stop() {
    if (!this.activeRunId || this.stopping) return;
    this.stopping = true;
    try {
      await callJsonApi(`${PLUGIN_API}/stop`, { run_id: this.activeRunId });
      await this.refreshStatus();
    } catch (err) {
      this.startError = String(err?.message || err);
    } finally {
      this.stopping = false;
    }
  },

  async refreshStatus() {
    const runId = this.activeRunId || (await this._discoverActiveRun());
    if (!runId) {
      this.state = null;
      this.activeRunId = "";
      this._stopTraceStream();
      return;
    }
    try {
      const url = `${PLUGIN_API}/status?run_id=${encodeURIComponent(runId)}`;
      const resp = await fetch(url, { method: "GET" });
      if (!resp.ok) {
        if (resp.status === 404) {
          this.activeRunId = "";
          this.state = null;
        }
        return;
      }
      const data = await resp.json();
      this.state = data;
      this.activeRunId = data.run_id;
      const lastExp = (data.experiments || []).length;
      const currentExp = data.status === "running" ? lastExp + 1 : lastExp;
      if (currentExp !== this.traceExpN && currentExp > 0) {
        this.traceExpN = currentExp;
        this._restartTraceStream();
      }
    } catch (err) {
      // Network blip — next poll will retry.
    }
  },

  async _discoverActiveRun() {
    try {
      const resp = await fetch(`${PLUGIN_API}/runs`, { method: "GET" });
      if (!resp.ok) return "";
      const data = await resp.json();
      const running = (data.runs || []).find((r) => r.status === "running");
      return running?.run_id || "";
    } catch {
      return "";
    }
  },

  recentExperiments() {
    const exps = this.state?.experiments || [];
    return exps.slice(-5).reverse();
  },

  isRunning() {
    return this.state?.status === "running";
  },

  outcomeBadgeClass(outcome) {
    if (outcome === "kept") return "ar-badge ar-badge-success";
    if (outcome === "reverted") return "ar-badge ar-badge-neutral";
    return "ar-badge ar-badge-warn";
  },

  statusBadgeClass(status) {
    if (status === "running") return "ar-badge ar-badge-info";
    if (status === "completed") return "ar-badge ar-badge-success";
    if (status === "stopped" || status === "cost_capped") return "ar-badge ar-badge-warn";
    return "ar-badge ar-badge-neutral";
  },

  passDelta(record) {
    const baseline = this.state?.experiments?.find((e) => e.outcome === "kept" && e.n < record.n);
    const baselinePassed = baseline?.eval_result?.passed ?? null;
    const passed = record.eval_result?.passed ?? null;
    if (baselinePassed === null || passed === null) return "";
    const delta = passed - baselinePassed;
    return delta > 0 ? `+${delta}` : String(delta);
  },

  formatSpend(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return String(value || "0");
    return `$${n.toFixed(4)}`;
  },

  shortSha(sha) {
    return sha ? String(sha).slice(0, 8) : "";
  },

  _restartTraceStream() {
    this._stopTraceStream();
    if (!this.activeRunId || !this.traceExpN) return;
    if (typeof EventSource === "undefined") return;
    this.trace = [];
    this.traceExhausted = false;
    const url = `${PLUGIN_API}/trace?run_id=${encodeURIComponent(this.activeRunId)}&exp=${this.traceExpN}`;
    const source = new EventSource(url);
    source.onmessage = (evt) => {
      this.trace.push(evt.data);
      if (this.trace.length > MAX_TRACE_LINES) {
        this.trace = this.trace.slice(-MAX_TRACE_LINES);
      }
    };
    source.addEventListener("end", () => {
      this.traceExhausted = true;
      source.close();
    });
    source.addEventListener("timeout", () => {
      source.close();
    });
    source.onerror = () => {
      // EventSource will auto-reconnect unless we close it.
      // Keep it alive so transient blips heal themselves.
    };
    this.traceSource = source;
  },

  _stopTraceStream() {
    if (this.traceSource) {
      try { this.traceSource.close(); } catch {}
      this.traceSource = null;
    }
  },

  async togglePastRuns() {
    this.pastRunsExpanded = !this.pastRunsExpanded;
    if (this.pastRunsExpanded && !this.pastRuns.length) {
      this.pastRunsLoading = true;
      try {
        const resp = await fetch(`${PLUGIN_API}/runs`, { method: "GET" });
        if (resp.ok) {
          const data = await resp.json();
          this.pastRuns = (data.runs || []).reverse();
        }
      } finally {
        this.pastRunsLoading = false;
      }
    }
  },
};

export const store = createStore("autoresearch", model);
