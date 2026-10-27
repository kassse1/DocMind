"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";

import { DeleteDocumentButton, ErrorWithRetry, StatusBadge, isSettled, usePolling } from "@/components/documents";
import { Alert, Card, EmptyState, LinkButton, LoadingBlock, PageHeader } from "@/components/ui";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { formatBytes, formatDateTime } from "@/lib/format";
import { useApi } from "@/lib/use-api";

const STATUS_TEXT = {
  pending: "Waiting for the indexing worker.",
  processing: "Being split into passages and embedded.",
  ready: "Indexed: available to Search and Ask.",
  failed: "Indexing failed. Delete it and upload the file again, or try another file.",
} as const;

export default function DocumentPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { data: doc, error, reload } = useApi((signal) => api.getDocument(id, signal), `document:${id}`);
  usePolling(Boolean(doc && !isSettled(doc.status)), reload);

  if (error instanceof ApiError && error.status === 404) {
    return (
      <EmptyState
        title="Document not found"
        action={<LinkButton href="/documents">Back to documents</LinkButton>}
      >
        It does not exist or has been deleted.
      </EmptyState>
    );
  }
  if (error && !doc) return <ErrorWithRetry error={error} onRetry={reload} />;
  if (!doc) return <LoadingBlock label="Loading document" />;

  const rows: Array<[string, string]> = [
    ["Type", doc.content_type],
    ["Size", formatBytes(doc.file_size)],
    ["Uploaded", formatDateTime(doc.created_at)],
    ["Last updated", formatDateTime(doc.updated_at)],
    ["SHA-256", doc.content_hash],
  ];

  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-3 text-sm text-muted">
        <Link href="/documents" className="hover:text-foreground">
          Documents
        </Link>
        <span aria-hidden="true"> / </span>
        <span className="text-foreground">Details</span>
      </nav>
      <PageHeader
        title={doc.filename}
        action={<DeleteDocumentButton document={doc} size="md" onDeleted={() => router.replace("/documents")} />}
      />
      <div className="grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Card className="p-5">
          <dl className="grid gap-4 sm:grid-cols-2">
            {rows.map(([label, value]) => (
              <div key={label} className={label === "SHA-256" ? "sm:col-span-2" : undefined}>
                <dt className="text-xs font-medium tracking-wide text-muted uppercase">{label}</dt>
                <dd className={label === "SHA-256" ? "mt-1 font-mono text-xs break-all" : "mt-1 text-sm"}>{value}</dd>
              </div>
            ))}
          </dl>
        </Card>
        <Card className="space-y-3 p-5">
          <h2 className="text-sm font-semibold">Indexing</h2>
          <StatusBadge status={doc.status} />
          <p className="text-sm text-muted">{STATUS_TEXT[doc.status]}</p>
          {doc.status === "failed" && doc.error_message && <Alert title="Reason">{doc.error_message}</Alert>}
          {doc.status === "ready" && (
            <div className="flex flex-wrap gap-2 pt-1">
              <LinkButton size="sm" href={`/search?document=${doc.id}`}>
                Search this document
              </LinkButton>
              <LinkButton size="sm" href={`/ask?document=${doc.id}`}>
                Ask about it
              </LinkButton>
            </div>
          )}
        </Card>
      </div>
    </>
  );
}
