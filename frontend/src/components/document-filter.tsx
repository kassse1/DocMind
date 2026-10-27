"use client";

import { useId } from "react";

import { MAX_DOCUMENT_FILTER, MAX_DOCUMENT_PAGE, type DocumentRead } from "@/lib/api/types";
import { api } from "@/lib/api/client";
import { useApi } from "@/lib/use-api";

/** Ready documents the user can restrict a search or question to. */
export function useReadyDocuments() {
  const state = useApi((signal) => api.listDocuments({ limit: MAX_DOCUMENT_PAGE }, signal), "ready-documents");
  const ready = (state.data?.documents ?? []).filter((d) => d.status === "ready");
  return { ...state, ready };
}

export function DocumentFilter({
  documents,
  selected,
  onChange,
}: {
  documents: DocumentRead[];
  selected: string[];
  onChange: (ids: string[]) => void;
}) {
  const id = useId();
  const toggle = (docId: string) =>
    onChange(selected.includes(docId) ? selected.filter((s) => s !== docId) : [...selected, docId]);
  const summary = selected.length === 0 ? "All documents" : `${selected.length} selected`;

  return (
    <details className="group rounded-md border border-border-strong bg-surface text-sm">
      <summary className="flex h-10 cursor-pointer list-none items-center justify-between gap-2 px-3">
        <span>
          <span className="text-muted">Documents: </span>
          {summary}
        </span>
        <span aria-hidden="true" className="text-muted transition-transform group-open:rotate-180">
          ▾
        </span>
      </summary>
      <fieldset className="max-h-64 space-y-1 overflow-y-auto border-t border-border p-2" aria-describedby={`${id}-hint`}>
        <legend className="sr-only">Limit to these documents</legend>
        <p id={`${id}-hint`} className="px-2 py-1 text-xs text-muted">
          Leave all unchecked to use every ready document (up to {MAX_DOCUMENT_FILTER} can be selected).
        </p>
        {documents.length === 0 && <p className="px-2 py-1 text-muted">No ready documents yet.</p>}
        {documents.map((doc) => {
          const checked = selected.includes(doc.id);
          return (
            <label key={doc.id} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 hover:bg-surface-muted">
              <input
                type="checkbox"
                checked={checked}
                disabled={!checked && selected.length >= MAX_DOCUMENT_FILTER}
                onChange={() => toggle(doc.id)}
                className="size-4 accent-[var(--accent)]"
              />
              <span className="truncate">{doc.filename}</span>
            </label>
          );
        })}
        {selected.length > 0 && (
          <button type="button" onClick={() => onChange([])} className="px-2 py-1 text-xs font-medium text-accent hover:underline">
            Clear selection
          </button>
        )}
      </fieldset>
    </details>
  );
}
