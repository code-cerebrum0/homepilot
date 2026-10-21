"use client";
import { useState, FormEvent } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";

export default function Register() {
  const { register } = useAuth(); const router = useRouter();
  const [f, setF] = useState({ name: "", email: "", password: "" });
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  async function submit(e: FormEvent) {
    e.preventDefault(); setError("");
    if (f.password.length < 10) return setError("Password must be at least 10 characters.");
    setBusy(true);
    try { await register(f.name, f.email, f.password); router.replace("/dashboard"); }
    catch (err) { setError(err instanceof Error ? err.message : "Registration failed"); }
    finally { setBusy(false); }
  }
  return (
    <form className="card auth" onSubmit={submit}>
      <h2>Create your account</h2>
      <label>Name</label><input required maxLength={100} value={f.name} onChange={e => setF({ ...f, name: e.target.value })} autoComplete="name" />
      <label>Email</label><input type="email" required value={f.email} onChange={e => setF({ ...f, email: e.target.value })} autoComplete="email" />
      <label>Password (min 10 characters)</label><input type="password" required value={f.password} onChange={e => setF({ ...f, password: e.target.value })} autoComplete="new-password" />
      {error && <div className="err">{error}</div>}
      <button disabled={busy} style={{ width: "100%", marginTop: 12 }}>{busy ? "Creating…" : "Register"}</button>
      <p className="muted">Already registered? <Link href="/login"><b>Sign in</b></Link></p>
    </form>
  );
}
