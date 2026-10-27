"use client";

import { createContext, useCallback, useContext, useEffect, useState, useSyncExternalStore, type ReactNode } from "react";

import { api } from "@/lib/api/client";
import { isAuthError } from "@/lib/api/errors";
import type { UserRead } from "@/lib/api/types";
import * as session from "@/lib/auth/session";

interface AuthContextValue {
  /** unknown: still restoring the session from the refresh cookie. */
  status: session.SessionStatus;
  user: UserRead | null;
  /** Restoring the session failed for a reason other than "not signed in". */
  restoreError: unknown;
  retryRestore: () => void;
  signIn: (email: string, password: string) => Promise<void>;
  signOut: (allSessions?: boolean) => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const status = useSyncExternalStore(session.subscribe, session.getStatus, () => "unknown" as const);
  const [user, setUser] = useState<UserRead | null>(null);
  const [restoreError, setRestoreError] = useState<unknown>(null);
  const [attempt, setAttempt] = useState(0);

  // On load, exchange the HttpOnly refresh cookie (if any) for an access token.
  useEffect(() => {
    if (session.getStatus() !== "unknown") return;
    let cancelled = false;
    api
      .me()
      .then((me) => {
        if (!cancelled) {
          setUser(me);
          setRestoreError(null);
        }
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        if (isAuthError(error)) session.clearSession();
        else setRestoreError(error);
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const signIn = useCallback(async (email: string, password: string) => {
    await session.login(email, password);
    setUser(await api.me());
  }, []);

  const signOut = useCallback(async (allSessions = false) => {
    try {
      await session.logout(allSessions);
    } finally {
      setUser(null);
    }
  }, []);

  const retryRestore = useCallback(() => {
    setRestoreError(null);
    setAttempt((n) => n + 1);
  }, []);

  return (
    <AuthContext.Provider
      value={{ status, user: status === "authenticated" ? user : null, restoreError, retryRestore, signIn, signOut }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside <AuthProvider>");
  return value;
}
