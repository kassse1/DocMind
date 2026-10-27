"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type DragEvent } from "react";

import { UploadIcon } from "@/components/icons";
import { Alert, Badge, Button, ConfirmDialog } from "@/components/ui";
import { api } from "@/lib/api/client";
import { ApiError, describeError } from "@/lib/api/errors";
import {
  DEFAULT_MAX_UPLOAD_BYTES,
  SUPPORTED_EXTENSIONS,
  type DocumentRead,
  type DocumentStatus,
} from "@/lib/api/types";
import { classNames, formatBytes } from "@/lib/format";

const STATUS: Record<DocumentStatus, { label: string; tone: "neutral" | "info" | "success" | "danger" }> = {
  pending: { label: "Queued", tone: "neutral" },
  processing: { label: "Indexing", tone: "info" },
  ready: { label: "Ready", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
};

export function StatusBadge({ status }: { status: DocumentStatus }) {
  const { label, tone } = STATUS[status];
  return (
    <Badge tone={tone}>
      {(status === "pending" || status === "processing") && (
        <span className="size-1.5 animate-pulse rounded-full bg-current" aria-hidden="true" />
      )}
      {label}
    </Badge>
  );
}

export function isSettled(status: DocumentStatus) {
  return status === "ready" || status === "failed";
}

/** Poll every `intervalMs` while `active`, pausing while the tab is hidden. */
export function usePolling(active: boolean, poll: () => void, intervalMs = 3000) {
  const pollRef = useRef(poll);
  useEffect(() => {
    pollRef.current = poll;
  });
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => {
      if (document.visibilityState === "visible") pollRef.current();
    }, intervalMs);
    return () => window.clearInterval(id);
  }, [active, intervalMs]);
}

/** Client-side checks mirroring the backend; the server remains the authority. */
export function checkFile(file: File): string | null {
  const name = file.name.toLowerCase();
  if (!SUPPORTED_EXTENSIONS.some((ext) => name.endsWith(ext))) {
    return `“${file.name}” is not supported. Upload a PDF, DOCX or TXT file.`;
  }
  if (file.size === 0) return `“${file.name}” is empty.`;
  if (file.size > DEFAULT_MAX_UPLOAD_BYTES) {
    return `“${file.name}” is ${formatBytes(file.size)}; the limit is ${formatBytes(DEFAULT_MAX_UPLOAD_BYTES)}.`;
  }
  return null;
}

function uploadErrorMessage(error: unknown, file: File): { text: string; duplicateOf?: string } {
  if (error instanceof ApiError) {
    switch (error.code) {
      case "duplicate_document": {
        const details = error.details as { document_id?: string | null } | null;
        return { text: `“${file.name}” is already in your library.`, duplicateOf: details?.document_id ?? undefined };
      }
      case "file_too_large": {
        const details = error.details as { max_upload_bytes?: number } | null;
        const limit = details?.max_upload_bytes;
        return { text: `“${file.name}” is too large${limit ? `; the limit is ${formatBytes(limit)}` : ""}.` };
      }
      case "unsupported_file_type":
      case "content_type_mismatch":
        return { text: `“${file.name}” is not a supported PDF, DOCX or TXT file.` };
    }
  }
  return { text: `“${file.name}”: ${describeError(error)}` };
}

type UploadItem =
  | { key: string; file: File; state: "uploading" }
  | { key: string; file: File; state: "done"; document: DocumentRead }
  | { key: string; file: File; state: "error"; message: string; duplicateOf?: string };

export function Uploader({ onUploaded }: { onUploaded: (document: DocumentRead) => void }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [items, setItems] = useState<UploadItem[]>([]);
  const [dragging, setDragging] = useState(false);
  // Files currently being sent, by name+size+mtime: a second drop of the same file is ignored.
  const inFlight = useRef(new Set<string>());

  function update(key: string, item: UploadItem) {
    setItems((current) => current.map((i) => (i.key === key ? item : i)));
  }

  async function upload(file: File) {
    const key = `${file.name}:${file.size}:${file.lastModified}`;
    if (inFlight.current.has(key)) return;
    const problem = checkFile(file);
    const id = `${key}:${Date.now()}`;
    if (problem) {
      setItems((current) => [{ key: id, file, state: "error", message: problem }, ...current]);
      return;
    }
    inFlight.current.add(key);
    setItems((current) => [{ key: id, file, state: "uploading" }, ...current]);
    try {
      const result = await api.uploadDocument(file);
      update(id, { key: id, file, state: "done", document: result.document });
      onUploaded(result.document);
    } catch (error) {
      const { text, duplicateOf } = uploadErrorMessage(error, file);
      update(id, { key: id, file, state: "error", message: text, duplicateOf });
    } finally {
      inFlight.current.delete(key);
    }
  }

  function onFiles(files: FileList | null) {
    if (!files) return;
    for (const file of Array.from(files)) void upload(file);
    if (inputRef.current) inputRef.current.value = "";
  }

  function onDrop(event: DragEvent) {
    event.preventDefault();
    setDragging(false);
    onFiles(event.dataTransfer.files);
  }

  return (
    <div className="space-y-3">
      <div
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={classNames(
          "flex flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed px-6 py-8 text-center transition-colors",
          dragging ? "border-accent bg-accent-soft" : "border-border-strong bg-surface",
        )}
      >
        <span className="rounded-full bg-accent-soft p-3 text-accent">
          <UploadIcon />
        </span>
        <div>
          <p className="text-sm font-medium">Drop files here, or choose them</p>
          <p id="upload-hint" className="mt-1 text-xs text-muted">
            PDF, DOCX or TXT up to {formatBytes(DEFAULT_MAX_UPLOAD_BYTES)} (the server&apos;s default limit). Scanned PDFs
            without a text layer are not supported.
          </p>
        </div>
        {/* One focusable control: the visually hidden input, named by its visible label. */}
        <label
          className={classNames(
            "inline-flex h-8 cursor-pointer items-center rounded-md border border-border-strong bg-surface px-3 text-sm font-medium",
            "hover:bg-surface-muted has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-ring",
          )}
        >
          Choose files
          <input
            ref={inputRef}
            type="file"
            multiple
            accept={[...SUPPORTED_EXTENSIONS].join(",")}
            className="sr-only"
            aria-describedby="upload-hint"
            onChange={(event) => onFiles(event.target.files)}
          />
        </label>
      </div>

      {items.length > 0 && (
        <ul className="space-y-2" aria-live="polite">
          {items.map((item) => (
            <li
              key={item.key}
              className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border bg-surface px-4 py-2.5 text-sm"
            >
              <span className="min-w-0 truncate font-medium">{item.file.name}</span>
              {item.state === "uploading" && <Badge tone="info">Uploading…</Badge>}
              {item.state === "done" && (
                // The live indexing status is in the library below; this row is the upload result only.
                <span className="flex items-center gap-2 text-muted">
                  <Badge tone="success">Uploaded</Badge>
                  <Link href={`/documents/${item.document.id}`} className="font-medium text-accent hover:underline">
                    Details
                  </Link>
                </span>
              )}
              {item.state === "error" && (
                <span className="flex flex-wrap items-center gap-2 text-danger">
                  {item.message}
                  {item.duplicateOf && (
                    <Link href={`/documents/${item.duplicateOf}`} className="font-medium text-accent hover:underline">
                      Open it
                    </Link>
                  )}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Delete button with a confirmation dialog. */
export function DeleteDocumentButton({
  document,
  onDeleted,
  size = "sm",
}: {
  document: DocumentRead;
  onDeleted: () => void;
  size?: "sm" | "md";
}) {
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string>();

  async function confirm() {
    setPending(true);
    setError(undefined);
    try {
      await api.deleteDocument(document.id);
      setOpen(false);
      onDeleted();
    } catch (reason) {
      // Already gone (404) counts as deleted.
      if (reason instanceof ApiError && reason.status === 404) {
        setOpen(false);
        onDeleted();
      } else {
        setError(describeError(reason));
      }
    } finally {
      setPending(false);
    }
  }

  return (
    <>
      <Button variant="ghost" size={size} className="text-danger hover:text-danger" onClick={() => setOpen(true)}>
        Delete<span className="sr-only"> {document.filename}</span>
      </Button>
      <ConfirmDialog
        open={open}
        title="Delete this document?"
        confirmLabel="Delete document"
        pending={pending}
        error={error}
        onConfirm={confirm}
        onCancel={() => {
          setOpen(false);
          setError(undefined);
        }}
      >
        <p>
          <span className="font-medium text-foreground">{document.filename}</span> and its search index will be
          removed permanently. This cannot be undone.
        </p>
      </ConfirmDialog>
    </>
  );
}

export function ErrorWithRetry({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  return (
    <Alert
      title="Could not load data"
      action={
        <Button size="sm" variant="secondary" onClick={onRetry}>
          Retry
        </Button>
      }
    >
      {describeError(error)}
    </Alert>
  );
}
