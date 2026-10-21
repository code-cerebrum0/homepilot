"use client";
import { createContext, useCallback, useContext, useEffect, useState, ReactNode } from "react";
import { api, post } from "./api";
import type { Me } from "./types";

interface Ctx {
  me: Me | null; loading: boolean; householdId: number | null;
  login: (email: string, password: string) => Promise<void>;
  register: (name: string, email: string, password: string) => Promise<void>;
  logout: () => Promise<void>; reload: () => Promise<void>;
}
const AuthCtx = createContext<Ctx | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    try { setMe(await api<Me>("/api/v1/users/me")); } catch { setMe(null); } finally { setLoading(false); }
  }, []);
  useEffect(() => { reload(); }, [reload]);

  const login = async (email: string, password: string) => { await post("/api/v1/auth/login", { email, password }); await reload(); };
  const register = async (name: string, email: string, password: string) => { await post("/api/v1/auth/register", { name, email, password }); await reload(); };
  const logout = async () => { try { await post("/api/v1/auth/logout"); } finally { setMe(null); } };

  const householdId = me?.memberships[0]?.household_id ?? null;
  return <AuthCtx.Provider value={{ me, loading, householdId, login, register, logout, reload }}>{children}</AuthCtx.Provider>;
}
export const useAuth = () => { const c = useContext(AuthCtx); if (!c) throw new Error("AuthProvider missing"); return c; };
