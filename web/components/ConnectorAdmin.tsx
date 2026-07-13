"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  Blocks,
  BrainCircuit,
  Cloud,
  Database,
  Link2,
  Mic2,
  Plus,
  Radio,
  RefreshCw,
  ShieldCheck,
  Trash2
} from "lucide-react";
import { checkConnector, getConnectorCatalog, listConnectors, removeConnector, saveConnector, getJarvisStatus, applyOllamaModel, testModelConnection } from "@/lib/api";
import type { Connector, ConnectorCatalogField, ConnectorCatalogItem } from "@/lib/types";

const ICONS: Record<string, typeof Cloud> = {
  ai: BrainCircuit,
  intelligence: BrainCircuit,
  llm: BrainCircuit,
  tools: Blocks,
  storage: Cloud,
  telemetry: Radio,
  voice: Mic2,
  knowledge: Database,
  collaboration: Link2
};
const PROVIDERS = ["openai", "gemini", "groq", "claude"];

export function ConnectorAdmin() {
  const [catalog, setCatalog] = useState<ConnectorCatalogItem[]>([]);
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [jarvisStatus, setJarvisStatus] = useState<any | null>(null);
  const [modelRouting, setModelRouting] = useState<"local" | "online">("local");
  const [selectedLocalModel, setSelectedLocalModel] = useState<string>("");
  const [selectedOnlineConnectorId, setSelectedOnlineConnectorId] = useState<string>("");
  const [availabilityOk, setAvailabilityOk] = useState<boolean | null>(null);
  const [countdown, setCountdown] = useState<number | null>(null);
  const [routingMessage, setRoutingMessage] = useState<string>("");
  const [selectedType, setSelectedType] = useState("");
  const [showModal, setShowModal] = useState<boolean>(false);
  const [category, setCategory] = useState("all");
  const [name, setName] = useState("");
  const [config, setConfig] = useState<Record<string, string>>({});
  const [modelTestResult, setModelTestResult] = useState<{ ok: boolean; model: string; provider: string; reply?: string; error?: string } | null>(null);
  const [modelTestBusy, setModelTestBusy] = useState(false);
  const [modelTestPrompt, setModelTestPrompt] = useState("");
  const [activeModel, setActiveModel] = useState<string>("");
  const [activeProvider, setActiveProvider] = useState<string>("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");

  async function refresh() {
    try {
      setError("");
      const [catalogResult, connectorResult] = await Promise.all([getConnectorCatalog(), listConnectors()]);
      setCatalog(catalogResult.connectors);
      setConnectors(connectorResult.connectors);
      setSelectedType((current) => current && catalogResult.connectors.some((item) => item.type === current)
        ? current
        : catalogResult.connectors[0]?.type || "");
      const activeOnline = connectorResult.connectors.find((item) => isLlmType(item.type) && String(item.config?.active) === "true");
      const activeSelection = selectedOnlineConnectorId && connectorResult.connectors.some((item) => item.id === selectedOnlineConnectorId)
        ? selectedOnlineConnectorId
        : activeOnline?.id || connectorResult.connectors.find((item) => isLlmType(item.type))?.id || "";
      setSelectedOnlineConnectorId(activeSelection);
      // refresh model status
      try {
        const status = await getJarvisStatus();
        if (status.model && status.model !== "unknown") setActiveModel(status.model);
        if (status.provider && status.provider !== "none") setActiveProvider(status.provider);
      } catch { /* ignore */ }
    } catch (exception) {
      setError(exception instanceof Error ? exception.message : "Unable to load connectors");
    }
  }

  useEffect(() => { void refresh(); }, []);

  useEffect(() => {
    let mounted = true;
    async function fetchStatus() {
      try {
        const status = await getJarvisStatus();
        if (!mounted) return;
        setJarvisStatus(status);
        // initialize default selected model from status
        const localModel = status?.routing?.local?.model || status?.ollama?.model;
        if (localModel) setSelectedLocalModel(localModel);
        setAvailabilityOk(status?.ollama?.model_available ?? null);
      } catch (err) {
        // ignore
      }
    }
    void fetchStatus();
    const timer = window.setInterval(() => void fetchStatus(), 15000);
    return () => { mounted = false; window.clearInterval(timer); };
  }, []);

  const selected = useMemo(() => catalog.find((item) => item.type === selectedType), [catalog, selectedType]);
  const categories = useMemo(() => [...new Set(catalog.map((item) => item.category))], [catalog]);
  const visibleCatalog = category === "all" ? catalog : catalog.filter((item) => item.category === category);
  const connectorOptions = useMemo(() => visibleCatalog.map((item) => ({ value: item.type, label: item.name })), [visibleCatalog]);
  const normalizedFields = useMemo(() => (selected?.fields || []).map(normalizeField), [selected]);
  const llmConnectors = connectors.filter((item) => isLlmType(item.type));
  const missingRequired = normalizedFields.some((field) => field.required && !(config[field.name] ?? field.default ?? "").trim());

  function selectConnector(item: ConnectorCatalogItem) {
    setSelectedType(item.type);
    setName("");
    setConfig(Object.fromEntries(item.fields.map(normalizeField).flatMap((field) => {
      if (field.default !== undefined) return [[field.name, field.default || ""]];
      if (field.name === "active" && isLlmType(item.type)) return [[field.name, "true"]];
      return [];
    })));
  }

  async function create() {
    if (!selected || missingRequired) return;
    setBusy("create");
    setError("");
    try {
      const finalConfig = Object.fromEntries(normalizedFields.map((field) => [field.name, config[field.name] ?? field.default ?? (field.name === "active" && isLlmType(selected.type) ? "true" : "")]));
      await saveConnector({ name: name.trim() || selected.name, type: selected.type, enabled: true, config: finalConfig });
      setName("");
      setConfig({});
      await refresh();
    } catch (exception) {
      setError(exception instanceof Error ? exception.message : "Could not save connector");
    } finally {
      setBusy(null);
    }
  }

  async function heartbeat(item: Connector) {
    setBusy(item.id);
    setError("");
    try {
      await checkConnector(item.id);
      await refresh();
    } catch (exception) {
      setError(exception instanceof Error ? exception.message : `Could not check ${item.name}`);
    } finally {
      setBusy(null);
    }
  }

  async function activateOnlineConnector(connectorId: string) {
    const conn = connectors.find((item) => item.id === connectorId);
    if (!conn) {
      setRoutingMessage("Selected online connector was not found.");
      return;
    }
    const finalConfig = { ...(conn.config || {}), active: "true" } as Record<string, string>;
    try {
      await saveConnector({ id: conn.id, name: conn.name, type: conn.type, enabled: true, config: finalConfig });
      setSelectedOnlineConnectorId(connectorId);
      await refresh();
      setRoutingMessage(`Online connector ${conn.name} activated for fallback.`);
    } catch (exc) {
      setRoutingMessage(exc instanceof Error ? exc.message : `Failed to activate ${conn.name}`);
    }
  }

  // Model routing actions
  async function applyLocalModel() {
    setRoutingMessage("");
    if (!selectedLocalModel) return;
    try {
      const result = await applyOllamaModel({ model: selectedLocalModel });
      // update status and connectors list
      setJarvisStatus((current: any) => ({ ...(current || {}), ollama: result.ollama }));
      setAvailabilityOk(Boolean(result.ollama?.model_available));
      setRoutingMessage(result.ollama?.model_available ? `Local model ${selectedLocalModel} is available and selected.` : `Local model ${selectedLocalModel} is not available.`);
      await refresh();
      if (!result.ollama?.model_available) {
        // show modal and start countdown fallback
        setShowModal(true);
        setCountdown(10);
        const interval = setInterval(() => setCountdown((c) => (c && c > 0 ? c - 1 : 0)), 1000);
        const timeout = setTimeout(async () => {
          clearInterval(interval);
          setCountdown(null);
          setShowModal(false);
          setModelRouting("online");
          const onlineConn = selectedOnlineConnectorId
            ? connectors.find((c) => c.id === selectedOnlineConnectorId)
            : connectors.find((c) => isLlmType(c.type));
          if (onlineConn) {
            setSelectedOnlineConnectorId(onlineConn.id);
            await activateOnlineConnector(onlineConn.id);
            setRoutingMessage(`Auto-activated online connector ${onlineConn.name}.`);
          } else if (jarvisStatus?.routing?.online?.configured) {
            setRoutingMessage("Local model unavailable; online fallback is configured via environment variables.");
          } else {
            setRoutingMessage("No online connector available; proceeding with system default LLM.");
          }
        }, 10000);

        (window as any).__modelRoutingCleanup = () => {
          clearTimeout(timeout);
          clearInterval(interval);
          setCountdown(null);
          setShowModal(false);
        };
      }
    } catch (exc) {
      setRoutingMessage(exceptionMessage(exc, "Failed to apply local model"));
    }
  }

  function cancelPendingRouting() {
    const cleanup = (window as any).__modelRoutingCleanup;
    if (typeof cleanup === "function") {
      cleanup();
    }
    setCountdown(null);
    setShowModal(false);
    setRoutingMessage("");
  }

  function exceptionMessage(exc: unknown, fallback: string) {
    return exc instanceof Error ? exc.message : fallback;
  }

  async function remove(item: Connector) {
    setBusy(item.id);
    setError("");
    try {
      await removeConnector(item.id);
      await refresh();
    } catch (exception) {
      setError(exception instanceof Error ? exception.message : `Could not remove ${item.name}`);
    } finally {
      setBusy(null);
    }
  }

  return <div className="screen-stack">
    <section className="hero-panel admin-hero">
      <div>
        <p className="eyebrow">Platform Administration</p>
        <h1>Connect your operational world.</h1>
        <p className="hero-copy">Manage model routing, MCP tools, cloud log stores, live telemetry, knowledge sources, and multilingual voice providers from one control plane.</p>
        
      </div>
      <div className="hero-metrics">
        <div><strong>{connectors.length}</strong><span>Connectors</span></div>
        <div><strong>{connectors.filter((item) => item.status === "online").length}</strong><span>Online</span></div>
        <div><strong>{llmConnectors.length}</strong><span>Online LLMs</span></div>
      </div>
    </section>

    <section className="content-section connector-builder">
      <div className="section-heading">
        <div><p className="eyebrow">Connector catalog</p><h2>Add an integration</h2></div>
        <span className="connector-security-badge"><ShieldCheck size={15} /> Server-side credentials</span>
      </div>

      <div className="catalog-tabs" aria-label="Connector categories">
        <button type="button" className={category === "all" ? "active" : ""} onClick={() => setCategory("all")}>All</button>
        {categories.map((item) => <button type="button" key={item} className={category === item ? "active" : ""} onClick={() => setCategory(item)}>{humanize(item)}</button>)}
      </div>

      <div className="connector-catalog-dropdown">
        <label>
          <span>Select connector type</span>
          <select value={selectedType} onChange={(event) => {
            const nextType = event.target.value;
            const nextItem = catalog.find((item) => item.type === nextType);
            if (nextItem) selectConnector(nextItem);
          }}>
            <option value="">Choose a connector</option>
            {connectorOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
          </select>
        </label>
        <p className="hint">Use the dropdown to add a provider, knowledge, storage, or telemetry connector with minimal UI noise.</p>
      </div>

      {selected && <form className="connector-form" onSubmit={(event) => { event.preventDefault(); void create(); }}>
        <div className="connector-form-heading">
          <span>{isLlmType(selected.type) ? <BrainCircuit size={20} /> : <Plus size={20} />}</span>
          <div><p>Configure</p><h3>{selected.name}</h3></div>
        </div>
        <div className="connector-fields">
          <label>
            <span>Connection name</span>
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder={selected.name} />
            <small>A recognizable name for health checks and routing.</small>
          </label>
          {normalizedFields.map((field) => <ConnectorField key={field.name} field={field} connectorType={selected.type} value={config[field.name] ?? field.default ?? ""} onChange={(value) => setConfig((current) => ({ ...current, [field.name]: value }))} />)}
        </div>
        <div className="connector-form-footer">
          <p><ShieldCheck size={15} /> Use environment-variable names for credentials. API keys stay in the backend.</p>
          <button className="primary-button" type="submit" disabled={busy === "create" || missingRequired}><Plus size={16} /> {busy === "create" ? "Connecting…" : "Add connector"}</button>
        </div>
      </form>}
      {error && <div className="error-panel">{error}</div>}
    </section>
    <section className="content-section model-routing">
      <div className="section-heading">
        <div><p className="eyebrow">Model routing</p><h2>On‑Prem or Online LLM</h2></div>
        <button type="button" className="ghost-button" onClick={() => { void refresh(); void getJarvisStatus().then(setJarvisStatus).catch(() => undefined); }}><RefreshCw size={15} /> Refresh status</button>
      </div>

      <div className="model-routing-grid">
        <label className="model-routing-option">
          <input type="radio" name="routing" checked={modelRouting === "local"} onChange={() => { cancelPendingRouting(); setModelRouting("local"); }} />
          <strong>On‑Prem SLM (Ollama)</strong>
        </label>
        <label className="model-routing-option">
          <input type="radio" name="routing" checked={modelRouting === "online"} onChange={() => { cancelPendingRouting(); setModelRouting("online"); }} />
          <strong>Online LLM</strong>
        </label>

        {modelRouting === "local" && (
          <div className="model-local">
            <label>
              <span>Select local model</span>
              <select value={selectedLocalModel} onChange={(e) => { setSelectedLocalModel(e.target.value); cancelPendingRouting(); }}>
                <option value="">Select model</option>
                {jarvisStatus?.ollama?.available_models?.map((m: string) => <option key={m} value={m}>{m}</option>)}
              </select>
            </label>
            <div className="model-status">
              <span>Status:</span>
              {availabilityOk === null ? <em>Checking…</em> : availabilityOk ? <span className="status-good">Available</span> : <span className="status-bad">Unavailable</span>}
              {countdown !== null && <small> Auto-switch in {countdown}s</small>}
            </div>
            <div className="model-actions">
              <button className="primary-button" onClick={() => { void applyLocalModel(); }} disabled={!selectedLocalModel}>Apply local model</button>
              <button className="ghost-button" onClick={() => { cancelPendingRouting(); setSelectedLocalModel(jarvisStatus?.ollama?.model || ""); setAvailabilityOk(jarvisStatus?.ollama?.model_available ?? null); }}>Reset</button>
            </div>
            {routingMessage && <div className="routing-message">{routingMessage}</div>}
          </div>
        )}

        {modelRouting === "online" && (
          <div className="model-online">
            <label>
              <span>Choose online connector</span>
              <select value={selectedOnlineConnectorId} onChange={(e) => { cancelPendingRouting(); setRoutingMessage(""); const id = e.target.value; setSelectedOnlineConnectorId(id); const conn = connectors.find((c) => c.id === id); if (conn) setRoutingMessage(`Selected online connector ${conn.name}`); }}>
                <option value="">Select connector</option>
                {connectors.filter((c) => isLlmType(c.type)).map((c) => <option key={c.id} value={c.id}>{c.name} · {c.status || "unknown"}</option>)}
              </select>
            </label>
            {jarvisStatus?.routing?.online?.configured && !connectors.filter((c) => isLlmType(c.type)).length && (
              <p className="hint">OpenAI provider is configured through environment variables and will be used as the online fallback.</p>
            )}
            <div className="model-actions">
              <button className="primary-button" onClick={async () => {
                cancelPendingRouting();
                if (selectedOnlineConnectorId) await activateOnlineConnector(selectedOnlineConnectorId);
                else if (jarvisStatus?.routing?.online?.configured) setRoutingMessage('Online provider fallback is configured via environment variables.');
                else setRoutingMessage('No connector selected.');
              }}>Use online</button>
            </div>
            {routingMessage && <div className="routing-message">{routingMessage}</div>}
          </div>
        )}
      </div>
    </section>

    {/* Active Model Status Bar */}
    <section className="content-section model-status-section" style={{ padding: "16px 24px", display: "flex", alignItems: "center", gap: 16, background: "rgba(14,165,233,0.06)", borderTop: "1px solid rgba(56,189,248,0.15)" }}>
      <BrainCircuit size={18} style={{ color: "#38bdf8", flexShrink: 0 }} />
      <div style={{ flex: 1 }}>
        <span style={{ fontSize: 11, color: "#64748b", textTransform: "uppercase", letterSpacing: 1 }}>Active Model</span>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <strong style={{ color: "#e2e8f0", fontSize: 14 }}>
            {activeModel || jarvisStatus?.model || "Unknown"}
          </strong>
          {(activeProvider || jarvisStatus?.provider) && (
            <span style={{ fontSize: 11, background: "rgba(56,189,248,0.1)", color: "#38bdf8", borderRadius: 6, padding: "2px 8px" }}>
              {activeProvider || jarvisStatus?.provider}
            </span>
          )}
          {jarvisStatus?.llm_ready === true && (
            <span style={{ fontSize: 11, background: "rgba(34,197,94,0.12)", color: "#22c55e", borderRadius: 6, padding: "2px 8px" }}>● Ready</span>
          )}
          {jarvisStatus?.llm_ready === false && (
            <span style={{ fontSize: 11, background: "rgba(239,68,68,0.12)", color: "#ef4444", borderRadius: 6, padding: "2px 8px" }}>○ Not configured</span>
          )}
        </div>
      </div>
    </section>

    {/* Model Test Chat */}
    <section className="content-section">
      <div className="section-heading">
        <div><p className="eyebrow">Model verification</p><h2>Test model response</h2></div>
      </div>
      <p style={{ fontSize: 13, color: "#64748b", marginBottom: 16 }}>
        Send a message directly to the configured LLM to confirm it is reachable and responding correctly.
      </p>
      <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
        <input
          style={{ flex: 1, background: "#1e293b", border: "1px solid #334155", borderRadius: 8, padding: "8px 14px", color: "#e2e8f0", fontSize: 13 }}
          placeholder="Type a test message (e.g. What is your model name?)"
          value={modelTestPrompt}
          onChange={(e) => setModelTestPrompt(e.target.value)}
          onKeyDown={async (e) => { if (e.key === "Enter") { setModelTestBusy(true); try { setModelTestResult(await testModelConnection(modelTestPrompt || undefined)); } catch (err) { setModelTestResult({ ok: false, model: "", provider: "", error: err instanceof Error ? err.message : "Unknown error" }); } finally { setModelTestBusy(false); } } }}
        />
        <button
          className="primary-button"
          disabled={modelTestBusy}
          onClick={async () => { setModelTestBusy(true); try { setModelTestResult(await testModelConnection(modelTestPrompt || undefined)); } catch (err) { setModelTestResult({ ok: false, model: "", provider: "", error: err instanceof Error ? err.message : "Unknown error" }); } finally { setModelTestBusy(false); } }}
        >
          {modelTestBusy ? "Pinging…" : "Send"}
        </button>
      </div>
      {modelTestResult && (
        <div style={{ background: modelTestResult.ok ? "rgba(34,197,94,0.07)" : "rgba(239,68,68,0.07)", border: `1px solid ${modelTestResult.ok ? "rgba(34,197,94,0.25)" : "rgba(239,68,68,0.25)"}`, borderRadius: 10, padding: "14px 18px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
            <span style={{ fontSize: 12, fontWeight: 600, color: modelTestResult.ok ? "#22c55e" : "#ef4444" }}>
              {modelTestResult.ok ? "✓ Connected" : "✗ Failed"}
            </span>
            {modelTestResult.model && <span style={{ fontSize: 11, color: "#64748b" }}>model: {modelTestResult.model}</span>}
            {modelTestResult.provider && <span style={{ fontSize: 11, color: "#64748b" }}>via {modelTestResult.provider}</span>}
          </div>
          {modelTestResult.reply && <p style={{ fontSize: 13, color: "#e2e8f0", margin: 0 }}>{modelTestResult.reply}</p>}
          {modelTestResult.error && <p style={{ fontSize: 13, color: "#ef4444", margin: 0 }}>{modelTestResult.error}</p>}
        </div>
      )}
    </section>

    {showModal && (
      <div className="modal-backdrop">
        <div className="modal">
          <h3>Local model unavailable</h3>
          <p>The selected local model <strong>{selectedLocalModel}</strong> is not available. Wait {countdown}s for manual change, or choose to switch to an online connector now.</p>
          <div className="modal-actions">
            <button className="ghost-button" onClick={() => { const fn = (window as any).__modelRoutingCleanup; if (fn) fn(); setShowModal(false); setRoutingMessage('Manual cancel - stay on local selection.'); }}>Cancel</button>
            <button className="primary-button" onClick={async () => { const onlineConn = connectors.find((c) => isLlmType(c.type) && c.status === 'online'); setShowModal(false); if (onlineConn) await activateOnlineConnector(onlineConn.id); else setRoutingMessage('No online connector available to activate.'); }}>Use Online Now</button>
          </div>
        </div>
      </div>
    )}

    <section className="content-section">
      <div className="section-heading">
        <div><p className="eyebrow">Runtime health</p><h2>Configured connections</h2></div>
        <button type="button" className="ghost-button" onClick={() => void refresh()}><RefreshCw size={15} /> Refresh</button>
      </div>
      <div className="connector-list">
        {connectors.length === 0 ? <div className="empty-state">No connectors yet. Add a model provider, cloud store, MCP endpoint, log stream, or voice provider above.</div> : connectors.map((item) => <article className={`connector-card ${isLlmType(item.type) ? "llm-connector" : ""}`} key={item.id}>
          <div className="connector-card-head">
            <div>
              <span className={`status-pill ${item.status || "pending"}`}>{item.status || "pending"}</span>
              {isLlmType(item.type) && <span className="connector-kind-pill"><BrainCircuit size={11} /> Model route</span>}
              <h3>{item.name}</h3>
              <p>{humanize(item.type)}</p>
            </div>
            <div className="button-row">
              <button type="button" className="ghost-button" disabled={busy === item.id} onClick={() => void heartbeat(item)}><Activity size={15} /> Heartbeat</button>
              <button type="button" className="icon-danger" disabled={busy === item.id} aria-label={`Delete ${item.name}`} onClick={() => void remove(item)}><Trash2 size={16} /></button>
            </div>
          </div>
          <div className="config-grid">{Object.entries(item.config).map(([key, value]) => <div key={key}><span>{humanize(key)}</span><code>{value || "—"}</code></div>)}</div>
          <small>{item.heartbeat_detail || "Heartbeat has not run yet."}</small>
        </article>)}
      </div>
    </section>
  </div>;
}

function ConnectorField({ field, connectorType, value, onChange }: { field: ConnectorCatalogField; connectorType: string; value: string; onChange: (value: string) => void }) {
  const options = field.options?.map((option) => typeof option === "string" ? { value: option, label: humanize(option) } : { value: option.value, label: option.label || humanize(option.value) });
  const providerField = field.name === "provider" && (isLlmType(connectorType) || connectorType === "online_llm");
  const booleanField = field.name === "active" || field.name === "enabled";
  const finalOptions = options?.length
    ? options
    : providerField
      ? PROVIDERS.map((provider) => ({ value: provider, label: provider === "openai" ? "OpenAI" : provider === "groq" ? "Groq" : provider === "claude" ? "Claude" : "Google Gemini" }))
      : booleanField
        ? [{ value: "true", label: "Enabled" }, { value: "false", label: "Disabled" }]
        : [];
  const label = field.label || humanize(field.name);
  const placeholder = field.placeholder || fieldPlaceholder(field.name);
  return <label>
    <span>{label}{field.required && <b aria-hidden="true"> *</b>}</span>
    {finalOptions.length > 0 || field.type === "select" ? <select value={value} required={field.required} onChange={(event) => onChange(event.target.value)}>
      <option value="">Select {label.toLowerCase()}</option>
      {finalOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
    </select> : <input type={field.type || "text"} value={value} required={field.required} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} autoComplete="off" />}
    {(field.help || field.name.endsWith("_env")) && <small>{field.help || "Name of the backend environment variable containing this credential."}</small>}
  </label>;
}

function normalizeField(field: string | ConnectorCatalogField): ConnectorCatalogField {
  return typeof field === "string" ? { name: field } : field;
}

function isLlmType(type: string) {
  const normalized = type.toLowerCase();
  return normalized.includes("llm") || normalized.includes("model") || PROVIDERS.some((provider) => normalized === provider || normalized.startsWith(`${provider}_`));
}

function humanize(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function connectorDescription(item: ConnectorCatalogItem) {
  if (isLlmType(item.type)) return "Choose a hosted model fallback";
  const descriptions: Record<string, string> = {
    storage: "Read operational files and logs",
    telemetry: "Stream live platform signals",
    knowledge: "Ground answers in your documents",
    voice: "Multilingual speech input and output",
    tools: "Attach a remote tool server",
    collaboration: "Connect team workflows"
    ,ai: "Configure local or hosted reasoning"
  };
  return descriptions[item.category] || "Extend platform capabilities";
}

function fieldPlaceholder(name: string) {
  if (name.endsWith("_env")) return "e.g. PROVIDER_API_KEY";
  if (name === "model") return "Provider model name";
  if (name.includes("endpoint") || name.includes("url")) return "https://…";
  return humanize(name);
}
