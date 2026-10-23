"use client";
import { useEffect, useRef, useState, FormEvent } from "react";
import { useAuth } from "@/lib/auth";
import type { ToolCallInfo } from "@/lib/types";

// ─── Types ────────────────────────────────────────────────────────────────────

interface ToolStatus {
  name: string;
  status: "running" | "success" | "error";
  summary?: string;
}

interface Msg {
  role: "user" | "bot";
  /** Accumulated reply text — grows token by token while streaming */
  text: string;
  tools?: ToolCallInfo[];
  /** Tool statuses shown while the message is still streaming */
  liveTools?: ToolStatus[];
  error?: boolean;
  usedLiveData?: boolean;
  /** True while this bot message is still being written */
  streaming?: boolean;
}

// ─── Suggestions ─────────────────────────────────────────────────────────────

const SUGGESTIONS = [
  "How much electricity did my household use in the last 7 days?",
  "Which device used the most energy this week?",
  "What is my electricity cost this month?",
  "Compare this week with last week.",
  "What are my savings opportunities?",
  "Show me my tariff and explain how my bill is calculated.",
];

// ─── Constants ────────────────────────────────────────────────────────────────

const BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

// ─── Helpers ─────────────────────────────────────────────────────────────────

function csrfToken(): string {
  if (typeof document === "undefined") return "";
  const m = document.cookie.match(/(?:^|; )csrf_token=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

function provenanceLine(tools: ToolCallInfo[]): string | null {
  const ok = tools.filter(t => t.status === "success");
  if (ok.length === 0) return null;
  const labels = [...new Set(ok.map(t => t.summary))];
  return labels.slice(0, 3).join(" · ") + (labels.length > 3 ? ` · +${labels.length - 3} more` : "");
}

function toolLabel(name: string): string {
  return name.replace(/_/g, " ");
}

// ─── Component ───────────────────────────────────────────────────────────────

export default function Assistant() {
  const { householdId } = useAuth();
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [cid, setCid] = useState<string | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth" });
  }, [msgs]);

  async function send(text: string) {
    if (!text.trim() || busy || !householdId) return;

    // Add user message
    setMsgs(m => [...m, { role: "user", text }]);
    setInput("");
    setBusy(true);

    // Add placeholder bot message that we'll update in-place
    const botIdx = (prev: Msg[]) => prev.length; // index of the bot message after append
    setMsgs(m => [...m, {
      role: "bot",
      text: "",
      liveTools: [],
      streaming: true,
    }]);

    const abort = new AbortController();
    abortRef.current = abort;

    try {
      const res = await fetch(`${BASE}/api/v1/households/${householdId}/chat/stream`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken(),
        },
        credentials: "include",
        body: JSON.stringify({ message: text, conversation_id: cid }),
        signal: abort.signal,
      });

      if (!res.ok) {
        const body = await res.json().catch(() => null);
        const errMsg = body?.error?.message ?? `Request failed (${res.status})`;
        setMsgs(m => {
          const updated = [...m];
          updated[updated.length - 1] = { role: "bot", text: errMsg, error: true };
          return updated;
        });
        return;
      }

      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // SSE frames are separated by \n\n
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";

        for (const frame of frames) {
          if (!frame.trim()) continue;

          // Parse event name and data
          let eventName = "message";
          let dataLine = "";
          for (const line of frame.split("\n")) {
            if (line.startsWith("event: ")) eventName = line.slice(7).trim();
            else if (line.startsWith("data: ")) dataLine = line.slice(6).trim();
          }

          let payload: Record<string, unknown>;
          try {
            payload = JSON.parse(dataLine);
          } catch {
            continue;
          }

          if (eventName === "tool") {
            const { name, status, summary } = payload as {
              name: string; status: string; summary?: string;
            };
            setMsgs(m => {
              const updated = [...m];
              const last = { ...updated[updated.length - 1] };
              const liveTools = [...(last.liveTools ?? [])];
              const existing = liveTools.findIndex(t => t.name === name);
              const entry: ToolStatus = {
                name,
                status: status as ToolStatus["status"],
                summary,
              };
              if (existing >= 0) liveTools[existing] = entry;
              else liveTools.push(entry);
              last.liveTools = liveTools;
              updated[updated.length - 1] = last;
              return updated;
            });

          } else if (eventName === "token") {
            const token = (payload as { text: string }).text ?? "";
            setMsgs(m => {
              const updated = [...m];
              const last = { ...updated[updated.length - 1] };
              last.text = last.text + token;
              updated[updated.length - 1] = last;
              return updated;
            });

          } else if (eventName === "done") {
            const { conversation_id, tool_calls } = payload as {
              conversation_id: string;
              tool_calls: ToolCallInfo[];
            };
            setCid(conversation_id);
            setMsgs(m => {
              const updated = [...m];
              const last = { ...updated[updated.length - 1] };
              last.streaming = false;
              last.liveTools = undefined;
              last.tools = tool_calls;
              last.usedLiveData = tool_calls.some(t => t.status === "success");
              updated[updated.length - 1] = last;
              return updated;
            });

          } else if (eventName === "error") {
            const errMsg = (payload as { message: string }).message ?? "An error occurred.";
            setMsgs(m => {
              const updated = [...m];
              updated[updated.length - 1] = { role: "bot", text: errMsg, error: true };
              return updated;
            });
          }
        }
      }

    } catch (e: unknown) {
      if ((e as Error).name === "AbortError") return;
      const errMsg = e instanceof Error ? e.message : "An unexpected error occurred.";
      setMsgs(m => {
        const updated = [...m];
        if (updated.length > 0 && updated[updated.length - 1].role === "bot") {
          updated[updated.length - 1] = { role: "bot", text: errMsg, error: true };
        } else {
          updated.push({ role: "bot", text: errMsg, error: true });
        }
        return updated;
      });
    } finally {
      setBusy(false);
      abortRef.current = null;
    }
  }

  const submit = (e: FormEvent) => { e.preventDefault(); send(input); };

  return (
    <div className="card chat">
      <h2>Energy assistant</h2>

      <div className="msgs">
        {/* Welcome state */}
        {msgs.length === 0 && (
          <>
            <p className="muted">
              Ask about your household's energy data. Answers are grounded in your
              actual readings and calculated using your configured tariff.
            </p>
            <div className="sugg">
              {SUGGESTIONS.map(s => (
                <button key={s} onClick={() => send(s)} disabled={busy}>{s}</button>
              ))}
            </div>
          </>
        )}

        {/* Message list */}
        {msgs.map((m, i) => (
          <div key={i} className={`msg ${m.role}`} style={m.error ? { color: "var(--red)" } : undefined}>
            {/* Reply text — grows token by token */}
            <span style={{ whiteSpace: "pre-wrap" }}>{m.text}</span>

            {/* Blinking cursor while streaming */}
            {m.streaming && m.text.length > 0 && (
              <span style={{ animation: "blink 1s step-end infinite", marginLeft: 1 }}>▍</span>
            )}

            {/* Live tool status during streaming */}
            {m.streaming && m.liveTools && m.liveTools.length > 0 && (
              <div style={{ marginTop: 6, display: "flex", flexWrap: "wrap", gap: 4 }}>
                {m.liveTools.map((t, j) => (
                  <span
                    key={j}
                    className="chip"
                    style={{
                      fontSize: 11,
                      opacity: t.status === "running" ? 0.65 : 1,
                      borderColor: t.status === "error" ? "var(--red)" : undefined,
                      color: t.status === "error" ? "var(--red)" : undefined,
                    }}
                  >
                    {t.status === "running" ? "⟳" : t.status === "success" ? "✓" : "✕"}{" "}
                    {toolLabel(t.name)}
                  </span>
                ))}
              </div>
            )}

            {/* Final tool chips (after streaming completes) */}
            {!m.streaming && m.tools && m.tools.length > 0 && (
              <div style={{ marginTop: 6, display: "flex", flexWrap: "wrap", gap: 4 }}>
                {m.tools.map((t, j) => (
                  <span
                    key={j}
                    className={`chip ${t.status === "error" ? "error" : ""}`}
                    title={t.summary}
                    style={{ fontSize: 11 }}
                  >
                    {t.status === "success" ? "✓" : "✕"} {toolLabel(t.name)}
                  </span>
                ))}
              </div>
            )}

            {/* Data-provenance badge */}
            {!m.streaming && m.role === "bot" && !m.error && m.usedLiveData && (() => {
              const line = provenanceLine(m.tools ?? []);
              return line ? (
                <div style={{
                  marginTop: 6,
                  fontSize: 11,
                  color: "var(--muted)",
                  borderTop: "1px solid var(--line)",
                  paddingTop: 4,
                }}>
                  📊 Live data · {line}
                </div>
              ) : null;
            })()}

            {/* Warning: tools attempted but all failed */}
            {!m.streaming && m.role === "bot" && !m.error
              && m.tools && m.tools.length > 0 && !m.usedLiveData && (
              <div style={{ marginTop: 6, fontSize: 11, color: "var(--orange)" }}>
                ⚠ No live data retrieved — answer may be general
              </div>
            )}
          </div>
        ))}

        {/* Generic loading indicator — shown before first token arrives */}
        {busy && msgs.length > 0 && msgs[msgs.length - 1].role === "bot"
          && msgs[msgs.length - 1].text === ""
          && !(msgs[msgs.length - 1].liveTools?.length) && (
          <div className="msg bot muted" style={{ fontStyle: "italic", opacity: 0.7, alignSelf: "flex-start" }}>
            Analysing your energy data…
          </div>
        )}

        <div ref={end} />
      </div>

      {/* Input form */}
      <form className="row" onSubmit={submit} style={{ marginTop: 8 }}>
        <input
          style={{ flex: 1 }}
          maxLength={1000}
          placeholder="Ask about your energy usage…"
          value={input}
          onChange={e => setInput(e.target.value)}
          disabled={busy}
          aria-label="Chat message"
        />
        <button disabled={busy || !input.trim()} aria-label="Send message">
          {busy ? "…" : "Send"}
        </button>
        {cid && (
          <button
            type="button"
            className="ghost"
            onClick={() => { setMsgs([]); setCid(null); }}
            disabled={busy}
            aria-label="Start new conversation"
          >
            New chat
          </button>
        )}
      </form>

      <style>{`
        @keyframes blink { 0%, 100% { opacity: 1 } 50% { opacity: 0 } }
      `}</style>
    </div>
  );
}
