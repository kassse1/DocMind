"use client";

import Link from "next/link";
import { useCallback, useState } from "react";

import {
  DeleteDocumentButton,
  ErrorWithRetry,
  StatusBadge,
  Uploader,
  isSettled,
  usePolling,
} from "@/components/documents";
import { Button, Card, CardHeader, EmptyState, LoadingBlock, PageHeader } from "@/components/ui";
import { api } from "@/lib/api/client";
import { MAX_DOCUMENT_PAGE, type DocumentRead } from "@/lib/api/types";
import { formatBytes, formatDateTime } from "@/lib/format";
import { useApi } from "@/lib/use-api";

const PAGE = 50;

export default function DocumentsPage() {
  const [pages, setPages] = useState(1);
  // The API caps limit at 200 and returns no total; this page shows up to the 200 newest.
  const limit = Math.min(PAGE * pages, MAX_DOCUMENT_PAGE);
  const { data, error, loading, reload } = useApi(
    (signal) => api.listDocuments({ limit }, signal),
    `documents:${limit}`,
  );
  const documents = data?.documents ?? [];
  const working = documents.some((d) => !isSettled(d.status));
  usePolling(working, reload);
  const onChanged = useCallback(() => reload(), [reload]);

  return (
    <>
      <PageHeader
        title="Documents"
        description="Upload files to make them searchable. Each file is indexed in the background; search and Ask use it once it is ready."
      />
      <div className="space-y-6">
        <Uploader onUploaded={onChanged} />
        <Card>
          <CardHeader
            title="Your library"
            description={data ? `${documents.length} document${documents.length === 1 ? "" : "s"} shown, newest first` : undefined}
            action={
              <Button variant="secondary" size="sm" onClick={reload} pending={loading && Boolean(data)}>
                Refresh
              </Button>
            }
          />
          {error && !data ? (
            <div className="p-5">
              <ErrorWithRetry error={error} onRetry={reload} />
            </div>
          ) : !data ? (
            <LoadingBlock label="Loading documents" />
          ) : documents.length === 0 ? (
            <EmptyState title="No documents yet">Upload a PDF, DOCX or TXT file above to get started.</EmptyState>
          ) : (
            <>
              <DocumentTable documents={documents} onDeleted={onChanged} />
              {documents.length === limit && (
                <div className="border-t border-border p-4 text-center text-sm text-muted">
                  {limit < MAX_DOCUMENT_PAGE ? (
                    <Button variant="secondary" size="sm" onClick={() => setPages((p) => p + 1)} pending={loading}>
                      Load more
                    </Button>
                  ) : (
                    `Showing the ${MAX_DOCUMENT_PAGE} newest documents.`
                  )}
                </div>
              )}
            </>
          )}
        </Card>
      </div>
    </>
  );
}

function DocumentTable({ documents, onDeleted }: { documents: DocumentRead[]; onDeleted: () => void }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">Your documents</caption>
        <thead className="border-b border-border text-xs font-medium tracking-wide text-muted uppercase">
          <tr>
            <th scope="col" className="px-5 py-3">Name</th>
            <th scope="col" className="px-5 py-3">Status</th>
            <th scope="col" className="hidden px-5 py-3 md:table-cell">Size</th>
            <th scope="col" className="hidden px-5 py-3 md:table-cell">Uploaded</th>
            <th scope="col" className="px-5 py-3 text-right">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {documents.map((doc) => (
            <tr key={doc.id} className="hover:bg-surface-muted/60">
              <td className="max-w-[18rem] px-5 py-3">
                <Link href={`/documents/${doc.id}`} className="block truncate font-medium text-foreground hover:text-accent">
                  {doc.filename}
                </Link>
                {doc.status === "failed" && doc.error_message && (
                  <p className="mt-0.5 truncate text-xs text-danger">{doc.error_message}</p>
                )}
              </td>
              <td className="px-5 py-3">
                <StatusBadge status={doc.status} />
              </td>
              <td className="hidden px-5 py-3 text-muted tabular-nums md:table-cell">{formatBytes(doc.file_size)}</td>
              <td className="hidden px-5 py-3 text-muted md:table-cell">{formatDateTime(doc.created_at)}</td>
              <td className="px-5 py-3 text-right">
                <DeleteDocumentButton document={doc} onDeleted={onDeleted} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
