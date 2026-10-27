"use client";

import Link from "next/link";

import { useAuth } from "@/components/auth-provider";
import { ErrorWithRetry, StatusBadge } from "@/components/documents";
import { AskIcon, FileIcon, SearchIcon } from "@/components/icons";
import { Badge, Card, CardHeader, EmptyState, LinkButton, LoadingBlock, PageHeader } from "@/components/ui";
import { api } from "@/lib/api/client";
import { MAX_DOCUMENT_PAGE, type DocumentStatus } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";
import { useApi } from "@/lib/use-api";

const COUNTS: Array<{ status: DocumentStatus; label: string }> = [
  { status: "ready", label: "Ready" },
  { status: "processing", label: "Indexing" },
  { status: "pending", label: "Queued" },
  { status: "failed", label: "Failed" },
];

const ACTIONS = [
  { href: "/documents", title: "Upload documents", text: "Add PDF, DOCX or TXT files.", icon: FileIcon },
  { href: "/search", title: "Search", text: "Find passages by meaning or keywords.", icon: SearchIcon },
  { href: "/ask", title: "Ask a question", text: "Get an answer with cited passages.", icon: AskIcon },
];

export default function DashboardPage() {
  const { user } = useAuth();
  const docs = useApi((signal) => api.listDocuments({ limit: MAX_DOCUMENT_PAGE }, signal), "dashboard-docs");
  const ready = useApi((signal) => api.readiness(signal), "readiness");
  const documents = docs.data?.documents ?? [];
  const capped = documents.length === MAX_DOCUMENT_PAGE;

  return (
    <>
      <PageHeader title="Dashboard" description={user ? `Signed in as ${user.email}.` : undefined} />

      <div className="grid gap-4 sm:grid-cols-3">
        {ACTIONS.map(({ href, title, text, icon: Icon }) => (
          <Link
            key={href}
            href={href}
            className="group rounded-xl border border-border bg-surface p-5 shadow-sm transition-colors hover:border-accent"
          >
            <span className="inline-flex rounded-lg bg-accent-soft p-2 text-accent">
              <Icon />
            </span>
            <p className="mt-3 font-medium group-hover:text-accent">{title}</p>
            <p className="mt-1 text-sm text-muted">{text}</p>
          </Link>
        ))}
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Card>
          <CardHeader
            title="Recent documents"
            action={
              <LinkButton href="/documents" size="sm">
                View all
              </LinkButton>
            }
          />
          {docs.error && !docs.data ? (
            <div className="p-5">
              <ErrorWithRetry error={docs.error} onRetry={docs.reload} />
            </div>
          ) : !docs.data ? (
            <LoadingBlock label="Loading documents" />
          ) : documents.length === 0 ? (
            <EmptyState
              title="Your library is empty"
              action={
                <LinkButton href="/documents" variant="primary" size="sm">
                  Upload a document
                </LinkButton>
              }
            >
              Upload a document to start searching and asking questions.
            </EmptyState>
          ) : (
            <ul className="divide-y divide-border">
              {documents.slice(0, 6).map((doc) => (
                <li key={doc.id} className="flex items-center justify-between gap-3 px-5 py-3">
                  <div className="min-w-0">
                    <Link href={`/documents/${doc.id}`} className="block truncate text-sm font-medium hover:text-accent">
                      {doc.filename}
                    </Link>
                    <p className="text-xs text-muted">{formatDateTime(doc.created_at)}</p>
                  </div>
                  <StatusBadge status={doc.status} />
                </li>
              ))}
            </ul>
          )}
        </Card>

        <div className="space-y-6">
          <Card>
            <CardHeader
              title="Library status"
              description={
                docs.data
                  ? capped
                    ? `Counts of your ${MAX_DOCUMENT_PAGE} newest documents.`
                    : `${documents.length} document${documents.length === 1 ? "" : "s"} in total.`
                  : undefined
              }
            />
            <dl className="grid grid-cols-2 gap-px bg-border">
              {COUNTS.map(({ status, label }) => (
                <div key={status} className="bg-surface px-5 py-4">
                  <dt className="text-xs text-muted">{label}</dt>
                  <dd className="mt-1 text-2xl font-semibold tabular-nums">
                    {docs.data ? documents.filter((d) => d.status === status).length : "—"}
                  </dd>
                </div>
              ))}
            </dl>
          </Card>

          <Card className="p-5">
            <h2 className="text-sm font-semibold">Service</h2>
            <div className="mt-3 flex items-center justify-between text-sm">
              <span className="text-muted">API and database</span>
              {ready.loading && !ready.data ? (
                <Badge>Checking…</Badge>
              ) : ready.data ? (
                <Badge tone="success">Ready</Badge>
              ) : (
                <Badge tone="danger">Unavailable</Badge>
              )}
            </div>
            <p className="mt-3 text-xs text-muted">
              Readiness covers the API and its database only. Question answering depends on a separately configured
              language model.
            </p>
          </Card>
        </div>
      </div>
    </>
  );
}
