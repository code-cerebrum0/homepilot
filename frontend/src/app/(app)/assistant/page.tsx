"use client";
import { useEffect, useRef, useState, FormEvent } from "react";
import { post } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { ChatReply, ToolCallInfo } from "@/lib/types";

interface Msg { role: "user" | "bot"; text: string; tools?: ToolCallInfo[]; error?: boolean }
const SUGGESTIONS = [
  "How much electricity did my household use in the last 24 hours?",
  "Which device used the most energy this week?",
  "Estimate my electricity cost this month.",
  "Compare this week with last week.",
  "Which devices have unusually high consumption?",
  "What can I change to reduce my bill?",
];

export default function Assistant() {
  const { householdId } = useAuth();
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState(""); const [busy, setBusy] = useState(false);
  const [cid, setCid] = useState<string | null>(null);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth" }); }, [msgs, busy]);

  async function send(text: string) {
    if (!text.trim() || busy || !householdId) return;
    setMsgs(m => [...m, { role: "user", text }]); setInput(""); setBusy(true);
    try {
      const r = await post<ChatReply>(`/api/v1/households/${householdId}/chat`, { message: text, conversation_id: cid });
      setCid(r.conversation_id);
      setMsgs(m => [...m, { role: "bot", text: r.reply, tools: r.tool_calls }]);
    } catch (e) {
      setMsgs(m => [...m, { role: "bot", text: (e as Error).message, error: true }]);
    } finally { setBusy(false); }
  }
  const submit = (e: FormEvent) => { e.preventDefault(); send(input); };

  return (
    <div className="card chat">
      <h2>Energy assistant</h2>
      <div className="msgs">
        {msgs.length === 0 && (
          <>
            <p className="muted">Ask about your household’s energy data. Answers are based on tool results from your own readings.</p>
            <div className="sugg">{SUGGESTIONS.map(s => <button key={s} onClick={() => send(s)}>{s}</button>)}</div>
          </>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={`msg ${m.role}`} style={m.error ? { color: "var(--red)" } : undefined}>
            {m.text}
            {m.tools?.map((t, j) => <span key={j} className={`chip ${t.status}`} title={t.summary}>{t.status === "success" ? "✓" : "✕"} {t.name}</span>)}
          </div>
        ))}
        {busy && <div className="msg bot muted">Analysing your data…</div>}
        <div ref={end} />
      </div>
      <form className="row" onSubmit={submit}>
        <input style={{ flex: 1 }} maxLength={1000} placeholder="Ask about your energy usage…" value={input} onChange={e => setInput(e.target.value)} />
        <button disabled={busy || !input.trim()}>Send</button>
        {cid && <button type="button" className="ghost" onClick={() => { setMsgs([]); setCid(null); }}>New chat</button>}
      </form>
    </div>
  );
}
