"use client";

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Activity,
  AudioLines,
  Bot,
  BrainCircuit,
  CloudCog,
  DatabaseZap,
  Keyboard,
  Languages,
  Mic,
  MicOff,
  Radio,
  RefreshCw,
  Send,
  ShieldCheck,
  Volume2,
  VolumeX,
  Square,
  Waves
} from "lucide-react";
import { askAssistant, getJarvisStatus, listConnectors, listIncidents } from "@/lib/api";
import type { AssistantAnswer, AssistantRoute, Connector, Incident, JarvisStatus, KnowledgeHit } from "@/lib/types";

type Exchange = {
  id: string;
  question: string;
  response?: AssistantAnswer;
  error?: string;
};

const LANGUAGES = [
  { value: "en-IN", label: "English (India)" },
  { value: "hi-IN", label: "हिन्दी" },
  { value: "bn-IN", label: "বাংলা" },
  { value: "gu-IN", label: "ગુજરાતી" },
  { value: "mr-IN", label: "मराठी" },
  { value: "ta-IN", label: "தமிழ்" },
  { value: "te-IN", label: "తెలుగు" },
  { value: "kn-IN", label: "ಕನ್ನಡ" },
  { value: "ml-IN", label: "മലയാളം" },
  { value: "pa-IN", label: "ਪੰਜਾਬੀ" },
  { value: "es-ES", label: "Español" },
  { value: "fr-FR", label: "Français" },
  { value: "de-DE", label: "Deutsch" },
  { value: "ar-SA", label: "العربية" },
  { value: "ja-JP", label: "日本語" }
];

export function JarvisAssistant() {
  const [language, setLanguage] = useState("en-IN");
  const [listening, setListening] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [autoSpeak, setAutoSpeak] = useState(true);
  const [liveTranscript, setLiveTranscript] = useState("");
  const [typedPrompt, setTypedPrompt] = useState("");
  const [voiceError, setVoiceError] = useState("");
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [visualLevels, setVisualLevels] = useState<number[]>(new Array(8).fill(0));
  const [systemStatus, setSystemStatus] = useState<JarvisStatus | null>(null);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [statusLoading, setStatusLoading] = useState(true);
  const [showFallbackPrompt, setShowFallbackPrompt] = useState(false);
  const [fallbackSeconds, setFallbackSeconds] = useState<number>(10);
  const recognitionRef = useRef<{ stop: () => void } | null>(null);
  const submittedRef = useRef(false);
  const conversationEndRef = useRef<HTMLDivElement | null>(null);
  const visualIntervalRef = useRef<number | null>(null);

  const latestRoute = [...exchanges].reverse().map((item) => item.response ? answerRoute(item.response) : undefined).find(Boolean);
  const activeIncidents = useMemo(
    () => incidents.filter((incident) => incident.current_status !== "complete").length,
    [incidents]
  );
  const onlineConnectors = useMemo(
    () => connectors.filter((connector) => connector.status === "online").length,
    [connectors]
  );
  const languageLabel = LANGUAGES.find((item) => item.value === language)?.label || "English";

  useEffect(() => {
    void refreshSystemStatus();
    const timer = window.setInterval(() => void refreshSystemStatus(), 30000);
    return () => {
      window.clearInterval(timer);
      recognitionRef.current?.stop();
      window.speechSynthesis?.cancel();
    };
  }, []);

  useEffect(() => {
    conversationEndRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [exchanges, busy]);

  async function refreshSystemStatus() {
    setStatusLoading(true);
    const [statusResult, incidentResult, connectorResult] = await Promise.allSettled([
      getJarvisStatus(),
      listIncidents(),
      listConnectors()
    ]);
    if (statusResult.status === "fulfilled") setSystemStatus(statusResult.value);
    if (incidentResult.status === "fulfilled") setIncidents(incidentResult.value);
    if (connectorResult.status === "fulfilled") setConnectors(connectorResult.value.connectors);
    setStatusLoading(false);
  }

  async function submitQuery(question: string) {
    const prompt = question.trim();
    if (!prompt || busy) return;
    const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    setBusy(true);
    setVoiceError("");
    setLiveTranscript("");
    setExchanges((items) => [...items, { id, question: prompt }]);
    try {
      const response = await askAssistant({ message: prompt, language_code: language });
      setExchanges((items) => items.map((item) => (item.id === id ? { ...item, response } : item)));
      if (autoSpeak) speak(response.answer, response.language || language);
      void refreshSystemStatus();
    } catch (error) {
      const message = error instanceof Error ? error.message : "Jarvis could not answer this request.";
      setExchanges((items) => items.map((item) => (item.id === id ? { ...item, error: message } : item)));
    } finally {
      setBusy(false);
    }
  }

  function toggleListening() {
    if (listening) {
      recognitionRef.current?.stop();
      return;
    }
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) {
      setVoiceError("Voice recognition is unavailable in this browser. Open Jarvis in a current Chrome or Edge browser and allow microphone access.");
      return;
    }
    window.speechSynthesis?.cancel();
    setSpeaking(false);
    setVoiceError("");
    setLiveTranscript("");
    submittedRef.current = false;
    const recognition = new Recognition();
    recognition.lang = language;
    recognition.interimResults = true;
    recognition.continuous = false;
    recognition.onstart = () => setListening(true);
    recognition.onresult = (event: any) => {
      let finalTranscript = "";
      let interimTranscript = "";
      for (let index = event.resultIndex || 0; index < event.results.length; index += 1) {
        const transcript = event.results[index][0]?.transcript || "";
        if (event.results[index].isFinal) finalTranscript += transcript;
        else interimTranscript += transcript;
      }
      setLiveTranscript((finalTranscript || interimTranscript).trim());
      if (finalTranscript.trim() && !submittedRef.current) {
        submittedRef.current = true;
        void submitQuery(finalTranscript);
      }
    };
    recognition.onerror = (event: any) => {
      setListening(false);
      if (event?.error !== "aborted") {
        setVoiceError(voiceErrorFor(event?.error));
      }
    };
    recognition.onend = () => {
      setListening(false);
      recognitionRef.current = null;
    };
    recognitionRef.current = recognition;
    recognition.start();
  }

  function speak(text: string, speechLanguage: string) {
    if (!window.speechSynthesis) {
      setVoiceError("Spoken output is unavailable in this browser.");
      return;
    }
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = speechLanguage;
    utterance.rate = 0.98;
    utterance.pitch = 1;
    utterance.onstart = () => setSpeaking(true);
    utterance.onend = () => setSpeaking(false);
    utterance.onerror = () => setSpeaking(false);
    window.speechSynthesis.speak(utterance);
  }

  function toggleSpokenOutput() {
    setAutoSpeak((enabled) => {
      if (enabled) {
        window.speechSynthesis?.cancel();
        setSpeaking(false);
      }
      return !enabled;
    });
  }

  // Audio visualizer: animate bars while listening/speaking/busy
  useEffect(() => {
    const mode = listening ? "listening" : speaking ? "speaking" : busy ? "busy" : "idle";
    if (visualIntervalRef.current) {
      window.clearInterval(visualIntervalRef.current);
      visualIntervalRef.current = null;
    }
    if (mode === "idle") {
      setVisualLevels(new Array(8).fill(0));
      return;
    }
    const amp = mode === "speaking" ? 1.0 : mode === "listening" ? 0.8 : 0.5;
    visualIntervalRef.current = window.setInterval(() => {
      setVisualLevels(() => Array.from({ length: 8 }).map(() => {
        const base = Math.random() * 40 * amp;
        const spike = Math.random() > 0.85 ? 40 * amp : 0;
        return Math.min(100, Math.round(base + spike + (Math.random() * 20)));
      }));
    }, 120) as unknown as number;
    return () => {
      if (visualIntervalRef.current) window.clearInterval(visualIntervalRef.current);
      visualIntervalRef.current = null;
    };
  }, [listening, speaking, busy]);

  const localAvailable = systemStatus?.ollama
    ? Boolean(systemStatus.ollama.enabled && systemStatus.ollama.reachable && systemStatus.ollama.model_available)
    : systemStatus?.local_model?.available ?? systemStatus?.routing?.local_available;
  const localFeasible = systemStatus?.ollama?.feasible;
  const cacheEnabled = systemStatus?.cache
    ? Boolean(systemStatus.cache.backend)
    : systemStatus?.routing?.cache_enabled;
  const cloudAvailable = systemStatus?.routing?.online?.configured ?? systemStatus?.online_fallback?.available;
  const cloudProvider = systemStatus?.routing?.online?.provider || systemStatus?.online_fallback?.provider || systemStatus?.routing?.provider;
  const cloudModel = systemStatus?.routing?.online?.model || systemStatus?.online_fallback?.model || systemStatus?.routing?.model;
  const localState = localFeasible === undefined ? localAvailable : localFeasible;

  useEffect(() => {
    if (localState === false && !cloudAvailable) {
      setShowFallbackPrompt(true);
      setFallbackSeconds(10);
      return;
    }
    setShowFallbackPrompt(false);
  }, [localState, cloudAvailable]);

  useEffect(() => {
    if (!showFallbackPrompt) return;
    if (fallbackSeconds <= 0) {
      return;
    }
    const timer = window.setTimeout(() => setFallbackSeconds((seconds) => seconds - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [showFallbackPrompt, fallbackSeconds]);

  return (
    <div className="jarvis-screen">
      <section className="jarvis-stage" aria-labelledby="jarvis-title">
        <div className="jarvis-ambient jarvis-ambient-one" />
        <div className="jarvis-ambient jarvis-ambient-two" />

        <header className="jarvis-header">
          <div>
            <p className="jarvis-kicker"><Radio size={14} /> System-aware audio RAG</p>
            <h1 id="jarvis-title">Speak to Jarvis.</h1>
            <p>Ask about live incidents, evidence, runbooks, recurring failures, and platform health. Speak naturally or type when audio is not practical.</p>
          </div>
          <div className="jarvis-header-actions">
            <label className="jarvis-language">
              <Languages size={17} />
              <span className="sr-only">Spoken language</span>
              <select value={language} onChange={(event) => setLanguage(event.target.value)} disabled={listening || busy}>
                {LANGUAGES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
              </select>
            </label>
            <button className={`jarvis-audio-toggle ${autoSpeak ? "active" : ""}`} type="button" onClick={toggleSpokenOutput} aria-pressed={autoSpeak}>
              {autoSpeak ? <Volume2 size={17} /> : <VolumeX size={17} />}
              <span>{autoSpeak ? "Replies aloud" : "Audio muted"}</span>
            </button>
          </div>
        </header>

        <div className="jarvis-console">
          <aside className="jarvis-context" aria-label="Live system context">
            <div className="jarvis-panel-heading">
              <div><span>Live context</span><strong>Background awareness</strong></div>
              <button type="button" onClick={() => void refreshSystemStatus()} disabled={statusLoading} aria-label="Refresh system context">
                <RefreshCw size={15} className={statusLoading ? "spin" : ""} />
              </button>
            </div>
            <div className="jarvis-context-grid">
              <ContextMetric icon={<Activity size={17} />} value={activeIncidents} label="Active incidents" tone={activeIncidents ? "warn" : "good"} />
              <ContextMetric icon={<ShieldCheck size={17} />} value={`${onlineConnectors}/${connectors.length}`} label="Connectors online" tone={onlineConnectors ? "good" : "neutral"} />
            </div>
            <div className="jarvis-routing-stack">
              <RouteStatus
                icon={<DatabaseZap size={17} />}
                title="Graph memory"
                detail={systemStatus?.cache?.backend ? `${systemStatus.cache.backend} · ${systemStatus.cache.active_entries ?? systemStatus.cache.entries ?? 0} active` : "Semantic answer cache"}
                state={cacheEnabled === undefined ? "discovering" : cacheEnabled ? "ready" : "offline"}
              />
              <RouteStatus
                icon={<BrainCircuit size={17} />}
                title="Local reasoning"
                detail={systemStatus?.ollama?.detail || systemStatus?.ollama?.model || systemStatus?.routing?.local?.model || systemStatus?.local_model?.model || "qwen2.5:7b via Ollama"}
                state={localState === undefined ? "discovering" : localState ? "ready" : "offline"}
              />
              <RouteStatus
                icon={<CloudCog size={17} />}
                title="Online fallback"
                detail={[cloudProvider, cloudModel].filter(Boolean).join(" · ") || "Configured provider"}
                state={cloudAvailable === undefined ? "discovering" : cloudAvailable ? "ready" : "offline"}
              />
            </div>
            {latestRoute && (
              <div className="jarvis-last-route">
                <span>Last answer route</span>
                <strong>{formatTier(latestRoute.tier)}</strong>
                <small>
                  {latestRoute.cache_hit ? "Memory hit" : [latestRoute.provider, latestRoute.model].filter(Boolean).join(" · ") || "Runtime selected"}
                </small>
              </div>
            )}
            {showFallbackPrompt && (
              <div className="jarvis-alert-banner">
                <div>
                  <strong>Optional local model unavailable.</strong>
                  <p>
                    Ollama can be used when available, but Jarvis will continue with the configured online provider or heuristic mode if it is not present. You can optionally configure an online provider in Admin within {fallbackSeconds}s.
                  </p>
                </div>
                <button type="button" onClick={() => { window.location.href = "/admin"; }}>Open Admin</button>
              </div>
            )}
          </aside>

          <main className="jarvis-conversation" aria-live="polite">
            <div className="jarvis-orb-zone">
              <button
                type="button"
                className={`jarvis-orb ${listening ? "listening" : ""} ${busy ? "thinking" : ""} ${speaking ? "speaking" : ""}`}
                onClick={toggleListening}
                disabled={busy}
                aria-label={listening ? "Stop interaction" : `Interact with Jarvis (visual)`}>
                <span className="jarvis-orb-ring ring-one" />
                <span className="jarvis-orb-ring ring-two" />
                <span className="jarvis-orb-core">
                  {listening ? <Square size={29} /> : busy ? <BrainCircuit size={31} /> : speaking ? <AudioLines size={31} /> : <Waves size={31} />}
                </span>
              </button>
              <div className="audio-visualizer" aria-hidden style={{display: 'flex', gap: 8, alignItems: 'end', marginTop: 12}}>
                {visualLevels.map((lvl, idx) => {
                  const height = Math.max(2, Math.round((lvl / 100) * 40));
                  return <div key={idx} style={{width: 6, height: `${height}px`, background: 'linear-gradient(180deg,#00E0A8,#0066FF)', borderRadius: 4, transition: 'height 120ms linear'}} />;
                })}
              </div>
              <div className="jarvis-listen-state">
                <strong>{listening ? "Listening..." : busy ? "Reasoning across the system..." : speaking ? "Speaking..." : "Tap to interact"}</strong>
                <span>{listening ? `Continue in ${languageLabel}` : "Graphical electronics visualization with typed input available"}</span>
              </div>
              {liveTranscript && <p className="jarvis-live-transcript"><Waves size={16} /> "{liveTranscript}"</p>}
              {voiceError && <p className="jarvis-voice-error"><MicOff size={16} /> {voiceError}</p>}
              <form className="jarvis-typed-query" onSubmit={(event) => {
                event.preventDefault();
                const prompt = typedPrompt.trim();
                if (!prompt || busy) return;
                setTypedPrompt("");
                void submitQuery(prompt);
              }}>
                <Keyboard size={16} />
                <label className="sr-only" htmlFor="jarvis-typed-prompt">Type a question for Jarvis</label>
                <input id="jarvis-typed-prompt" value={typedPrompt} onChange={(event) => setTypedPrompt(event.target.value)} placeholder="Or type a system-aware question..." disabled={busy || listening} />
                <button type="submit" disabled={busy || listening || !typedPrompt.trim()} aria-label="Ask Jarvis"><Send size={15} /></button>
              </form>
            </div>

            <div className="jarvis-transcript" aria-label="Conversation transcript">
              {exchanges.length === 0 ? (
                <div className="jarvis-transcript-empty">
                  <Bot size={24} />
                  <div><strong>Jarvis is standing by.</strong><p>Your spoken questions and grounded answers will appear here as a transcript.</p></div>
                </div>
              ) : exchanges.map((exchange) => (
                <article className="jarvis-exchange" key={exchange.id}>
                  <div className="jarvis-question"><span>You</span><p>{exchange.question}</p></div>
                  <div className="jarvis-answer">
                    <div className="jarvis-answer-head">
                      <span><Bot size={15} /> Jarvis</span>
                      {exchange.response && <AnswerRoute response={exchange.response} />}
                    </div>
                    {exchange.response ? (
                      <>
                        <p>{exchange.response.answer}</p>
                        {exchange.response.citations && exchange.response.citations.length > 0 && <CitationList citations={exchange.response.citations} />}
                      </>
                    ) : exchange.error ? <p className="jarvis-answer-error">{exchange.error}</p> : <div className="jarvis-answer-loading"><i /><i /><i /></div>}
                  </div>
                </article>
              ))}
              <div ref={conversationEndRef} />
            </div>
          </main>
        </div>
      </section>
    </div>
  );
}

function ContextMetric({ icon, value, label, tone }: { icon: ReactNode; value: number | string; label: string; tone: "good" | "warn" | "neutral" }) {
  return <div className={`jarvis-context-metric ${tone}`}>{icon}<strong>{value}</strong><span>{label}</span></div>;
}

function RouteStatus({ icon, title, detail, state }: { icon: ReactNode; title: string; detail: string; state: "ready" | "offline" | "discovering" }) {
  return <div className="jarvis-route-status"><span className={`jarvis-route-icon ${state}`}>{icon}</span><div><strong>{title}</strong><small>{detail}</small></div><i className={state} title={state} /></div>;
}

function AnswerRoute({ response }: { response: AssistantAnswer }) {
  const route = answerRoute(response);
  const tags = [
    route?.cache_hit ? "Cache hit" : route?.cache_hit === false ? "Cache miss" : undefined,
    formatTier(route?.tier),
    route?.provider,
    route?.model,
    route?.latency_ms !== undefined ? `${Math.round(route.latency_ms)} ms` : undefined,
    response.source
  ].filter((item, index, values): item is string => Boolean(item) && values.indexOf(item) === index);
  return <div className="jarvis-route-tags">{tags.map((tag) => <span key={tag}>{tag}</span>)}</div>;
}

function answerRoute(response: AssistantAnswer): AssistantRoute | undefined {
  return response.route || response.routing;
}

function CitationList({ citations }: { citations: KnowledgeHit[] }) {
  return <div className="jarvis-sources"><span>Grounded in</span>{citations.slice(0, 4).map((citation, index) => <div key={`${citation.source_path || citation.title}-${index}`}><strong>{citation.title || citation.source_path || "Operational evidence"}</strong><small>{citation.kind || citation.citation || "knowledge"}</small>{citation.content && <p>{citation.content}</p>}</div>)}</div>;
}

function formatTier(tier?: string) {
  if (!tier) return "";
  return tier.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function voiceErrorFor(error?: string) {
  if (error === "not-allowed" || error === "service-not-allowed") return "Microphone access was blocked. Allow microphone permission and try again.";
  if (error === "no-speech") return "I did not hear a question. Tap the microphone and try again.";
  if (error === "audio-capture") return "No microphone was detected.";
  if (error === "network") return "Voice recognition could not reach the speech service.";
  return "Voice recognition stopped unexpectedly. Please try again.";
}
