"use client";

import { useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { ErrorWithRetry } from "@/components/documents";
import { Alert, Badge, Button, Card, CardHeader, ConfirmDialog, LoadingBlock, PageHeader } from "@/components/ui";
import { api } from "@/lib/api/client";
import { formatDateTime } from "@/lib/format";
import { useApi } from "@/lib/use-api";

export default function AccountPage() {
  const { user, signOut } = useAuth();
  const sessions = useApi((signal) => api.sessions(signal), "sessions");
  const [confirmAll, setConfirmAll] = useState(false);
  const [pending, setPending] = useState<"one" | "all" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function end(all: boolean) {
    setPending(all ? "all" : "one");
    setNotice(null);
    try {
      await signOut(all);
    } catch {
      // Signed out locally; the server could not confirm (signOut always clears state).
      setNotice("Signed out on this device, but the server could not confirm it.");
    } finally {
      setPending(null);
    }
  }

  return (
    <>
      <PageHeader title="Account" />
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Card>
          <CardHeader title="Profile" />
          <dl className="space-y-4 p-5 text-sm">
            <div>
              <dt className="text-xs font-medium tracking-wide text-muted uppercase">Email</dt>
              <dd className="mt-1 break-all">{user?.email}</dd>
            </div>
            <div>
              <dt className="text-xs font-medium tracking-wide text-muted uppercase">Member since</dt>
              <dd className="mt-1">{user ? formatDateTime(user.created_at) : "—"}</dd>
            </div>
          </dl>
        </Card>

        <Card>
          <CardHeader
            title="Sessions"
            description="Devices where you are signed in. With the default server settings a session ends after 7 days of inactivity or 30 days in total."
          />
          <div className="p-5">
            {sessions.error && !sessions.data ? (
              <ErrorWithRetry error={sessions.error} onRetry={sessions.reload} />
            ) : !sessions.data ? (
              <LoadingBlock label="Loading sessions" />
            ) : (
              <ul className="divide-y divide-border text-sm">
                {sessions.data.map((s) => (
                  <li key={s.id} className="flex flex-wrap items-center justify-between gap-2 py-3 first:pt-0 last:pb-0">
                    <div>
                      <p>Signed in {formatDateTime(s.created_at)}</p>
                      <p className="text-xs text-muted">
                        {s.last_used_at ? `Last active ${formatDateTime(s.last_used_at)} · ` : ""}Expires {formatDateTime(s.expires_at)}
                      </p>
                    </div>
                    {s.current && <Badge tone="accent">This device</Badge>}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="flex flex-wrap justify-end gap-2 border-t border-border p-5">
            {notice && <Alert tone="warning">{notice}</Alert>}
            <Button variant="secondary" pending={pending === "one"} disabled={pending !== null} onClick={() => end(false)}>
              Sign out
            </Button>
            <Button variant="danger" disabled={pending !== null} onClick={() => setConfirmAll(true)}>
              Sign out everywhere
            </Button>
          </div>
        </Card>
      </div>

      <ConfirmDialog
        open={confirmAll}
        title="Sign out of every session?"
        confirmLabel="Sign out everywhere"
        pending={pending === "all"}
        onConfirm={() => end(true)}
        onCancel={() => setConfirmAll(false)}
      >
        <p>All your sessions end immediately, including this one. You will need to sign in again on every device.</p>
      </ConfirmDialog>
    </>
  );
}
