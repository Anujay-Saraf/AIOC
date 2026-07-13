"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Languages, Mic, MicOff, Send, Sparkles, Square } from "lucide-react";
import { askAssistant } from "@/lib/api";
import type { AssistantAnswer, KnowledgeHit } from "@/lib/types";

type AssistantMessage = {
  role: "user" | "assistant";
  text: string;
  confidence?: number;
  citations?: KnowledgeHit[];
};

type CommandAssistantProps = {
  incidentId?: string;
  title?: string;
  description?: string;
  compact?: boolean;
};

const LANGUAGE_OPTIONS = [
  { value: "en", label: "English" },
  { value: "hi-IN", label: "हिंदी" },
  { value: "bn-IN", label: "বাংলা" },
  { value: "gu-IN", label: "ગુજરાતી" },
  { value: "mr-IN", label: "मराठी" },
  { value: "ta-IN", label: "தமிழ்" },
  { value: "te-IN", label: "తెలుగు" },
  { value: "kn-IN", label: "ಕನ್ನಡ" },
  { value: "es-ES", label: "Español" },
  { value: "fr-FR", label: "Français" },
  { value: "de-DE", label: "Deutsch" },
  { value: "ja-JP", label: "日本語" },
  { value: "ar-SA", label: "العربية" }
];

export function CommandAssistant({
  incidentId,
  title = "Incident Copilot",
  description = "Ask questions in your language. The assistant will retrieve evidence, cite sources, and speak the answer back.",
  compact = false
}: CommandAssistantProps) {
  const [message, setMessage] = useState("");
  const [history, setHistory] = useState<AssistantMessage[]>([]);
  const [language, setLanguage] = useState("en");
  const [busy, setBusy] = useState(false);
  const [listening, setListening] = useState(false);
  const [autoSpeak, setAutoSpeak] = useState(true);
  const recognitionRef = useRef<SpeechRecognitionInstance | null>(null);

  const languageLabel = useMemo(
    () => LANGUAGE_OPTIONS.find((option) => option.value === language)?.label || "English",
    [language]
  );

  useEffect(() => {
    return () => {
      recognitionRef.current?.stop();
    };
  }, []);

  async function submit(prompt: string) {
    const trimmed = prompt.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    setMessage("");
    setHistory((items) => [...items, { role: "user", text: trimmed }]);
    try {
      const response = await askAssistant({
        message: trimmed,
        incident_id: incidentId,
        language_code: language
      });
      appendAssistantResponse(response);
      if (autoSpeak) speak(response.answer, response.language || language);
    } catch (error) {
      setHistory((items) => [
        ...items,
        {
          role: "assistant",
          text: error instanceof Error ? error.message : "Assistant request failed."
        }
      ]);
    } finally {
      setBusy(false);
    }
  }

  function appendAssistantResponse(response: AssistantAnswer) {
    setHistory((items) => [
      ...items,
      {
        role: "assistant",
        text: response.answer,
        confidence: response.confidence,
        citations: response.citations
      }
    ]);
  }

  function speak(text: string, speechLanguage: string) {
    if (!window.speechSynthesis) return;
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = speechLanguage || language;
    utterance.rate = 1.0;
    utterance.pitch = 1.0;
    window.speechSynthesis.speak(utterance);
  }

  function toggleVoice() {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) {
      setHistory((items) => [...items, { role: "assistant", text: "Voice input is not supported in this browser." }]);
      return;
    }
    if (listening) {
      recognitionRef.current?.stop();
      return;
    }
    const recognition = new Recognition();
    recognition.lang = language;
    recognition.interimResults = false;
    recognition.continuous = false;
    recognition.onstart = () => setListening(true);
    recognition.onresult = (event: any) => {
      const transcript = Array.from(event.results)
        .map((result: any) => result[0]?.transcript || "")
        .join(" ")
        .trim();
      if (transcript) {
        setMessage(transcript);
        void submit(transcript);
      }
    };
    recognition.onerror = () => setListening(false);
    recognition.onend = () => setListening(false);
    recognitionRef.current = recognition;
    recognition.start();
  }

  return (
    <section className={`assistant-panel ${compact ? "assistant-compact" : ""}`}>
      <div className="assistant-header">
        <div>
          <p className="eyebrow">Agentic Copilot</p>
          <h3>{title}</h3>
          <p>{description}</p>
        </div>
        <div className="assistant-controls">
          <label className="assistant-select">
            <Languages size={16} />
            <select value={language} onChange={(event) => setLanguage(event.target.value)}>
              {LANGUAGE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <button type="button" className="ghost-button" onClick={() => setAutoSpeak((value) => !value)}>
            {autoSpeak ? "Voice On" : "Voice Off"}
          </button>
          <button type="button" className="ghost-button" onClick={toggleVoice}>
            {listening ? <Square size={16} /> : <Mic size={16} />}
            {listening ? "Stop" : "Speak"}
          </button>
        </div>
      </div>

      <div className="assistant-presets">
        {[
          "What is the most likely root cause?",
          "Summarize the business impact.",
          "What should we do next?",
          "Show relevant evidence and docs."
        ].map((preset) => (
          <button key={preset} className="assistant-chip" onClick={() => void submit(preset)} disabled={busy}>
            <Sparkles size={14} />
            {preset}
          </button>
        ))}
      </div>

      <div className="assistant-log">
        {history.length === 0 ? (
          <div className="assistant-empty">
            <strong>Ready for voice or text.</strong>
            <span>Ask in English or an Indian language. The assistant will ground answers in incident evidence and knowledge docs.</span>
          </div>
        ) : (
          history.map((entry, index) => (
            <article key={`${entry.role}-${index}`} className={`assistant-message ${entry.role}`}>
              <div className="assistant-message-head">
                <strong>{entry.role === "user" ? "You" : "Assistant"}</strong>
                {entry.confidence !== undefined && (
                  <span className="assistant-confidence">{Math.round(entry.confidence * 100)}%</span>
                )}
              </div>
              <p>{entry.text}</p>
              {entry.citations && entry.citations.length > 0 && (
                <div className="assistant-citations">
                  {entry.citations.map((citation, citationIndex) => (
                    <div key={`${citation.source_path}-${citationIndex}`} className="assistant-citation">
                      <strong>{citation.title || citation.source_path}</strong>
                      <span>{citation.kind || "doc"}</span>
                      <code>{citation.citation || citation.source_path}</code>
                      {citation.content && <p>{citation.content}</p>}
                    </div>
                  ))}
                </div>
              )}
            </article>
          ))
        )}
      </div>

      <div className="assistant-input-row">
        <input
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              void submit(message);
            }
          }}
          placeholder={`Ask in ${languageLabel}...`}
        />
        <button type="button" className="assistant-send" disabled={busy} onClick={() => void submit(message)}>
          <Send size={16} /> Ask
        </button>
      </div>
    </section>
  );
}

type SpeechRecognitionInstance = {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  onstart: (() => void) | null;
  onresult: ((event: any) => void) | null;
  onerror: ((event: any) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
};

declare global {
  interface Window {
    SpeechRecognition?: new () => SpeechRecognitionInstance;
    webkitSpeechRecognition?: new () => SpeechRecognitionInstance;
  }
}
