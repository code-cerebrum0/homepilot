"use client";
import { useState, FormEvent } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";

export default function Login() {
  const { login } = useAuth(); const router = useRouter();
  const [email, setEmail] = useState(""); const [password, setPassword] = useState("");
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  async function submit(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError("");
    try { await login(email, password); router.replace("/dashboard"); }
    catch (err) { setError(err instanceof Error ? err.message : "Login failed"); }
    finally { setBusy(false); }
  }
  return (
    <form className="card auth" onSubmit={submit}>
      <h2>Sign in to HomePilot</h2>
      <label>Email</label><input type="email" required value={email} onChange={e => setEmail(e.target.value)} autoComplete="email" />
      <label>Password</label><input type="password" required value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" />
      {error && <div className="err">{error}</div>}
      <button disabled={busy} style={{ width: "100%", marginTop: 12 }}>{busy ? "Signing in…" : "Sign in"}</button>
      <p className="muted">No account? <Link href="/register"><b>Register</b></Link></p>
    </form>
  );
}
