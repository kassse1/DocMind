"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState, type FormEvent } from "react";

import { DocumentFilter, useReadyDocuments } from "@/components/document-filter";
import { SearchResults, type SearchOutcome } from "@/components/search-results";
import { Alert, Button, Card, Field, Input, PageHeader, Select } from "@/components/ui";
import { api } from "@/lib/api/client";
import { describeError } from "@/lib/api/errors";
import { DEFAULT_TOP_K, MAX_QUERY_CHARS, MAX_TOP_K, type HybridMode } from "@/lib/api/types";
import { classNames } from "@/lib/format";

type Mode = "semantic" | "hybrid";

const HYBRID_MODES: Array<{ value: HybridMode; label: string }> = [
  { value: "hybrid", label: "Vector + keyword (fused)" },
  { value: "vector", label: "Vector only" },
  { value: "full_text", label: "Keyword only" },
];

export default function SearchPage() {
  return (
    <Suspense>
      <SearchView />
    </Suspense>
  );
}

function SearchView() {
  const params = useSearchParams();
  const { ready } = useReadyDocuments();
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<Mode>("semantic");
  const [hybridMode, setHybridMode] = useState<HybridMode>("hybrid");
  const [topK, setTopK] = useState(DEFAULT_TOP_K);
  const [selected, setSelected] = useState<string[]>(() => {
    const doc = params.get("document");
    return doc ? [doc] : [];
  });
  const [outcome, setOutcome] = useState<SearchOutcome | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => () => controller.current?.abort(), []);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const text = query.trim();
    if (!text) return;
    controller.current?.abort();
    const current = new AbortController();
    controller.current = current;
    setPending(true);
    setError(null);
    const document_ids = selected.length ? selected : undefined;
    try {
      const next: SearchOutcome =
        mode === "semantic"
          ? { kind: "semantic", response: await api.search({ query: text, top_k: topK, document_ids }, current.signal) }
          : {
              kind: "hybrid",
              response: await api.hybridSearch({ query: text, top_k: topK, document_ids, mode: hybridMode }, current.signal),
            };
      setOutcome(next);
    } catch (reason) {
      if (!current.signal.aborted) {
        setError(reason);
        setOutcome(null);
      }
    } finally {
      if (!current.signal.aborted) setPending(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Search"
        description="Find passages in your ready documents. Semantic search matches meaning; hybrid search also uses keyword matching."
      />
      <Card className="p-5">
        <form onSubmit={onSubmit} className="space-y-4" role="search">
          <fieldset>
            <legend className="mb-2 text-sm font-medium">Mode</legend>
            <div className="inline-flex rounded-lg border border-border-strong p-1">
              {(["semantic", "hybrid"] as const).map((value) => (
                <label
                  key={value}
                  className={classNames(
                    "cursor-pointer rounded-md px-4 py-1.5 text-sm font-medium has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-ring",
                    mode === value ? "bg-accent text-accent-foreground" : "text-muted hover:text-foreground",
                  )}
                >
                  <input
                    type="radio"
                    name="mode"
                    value={value}
                    checked={mode === value}
                    onChange={() => setMode(value)}
                    className="sr-only"
                  />
                  {value === "semantic" ? "Semantic" : "Hybrid"}
                </label>
              ))}
            </div>
          </fieldset>

          <Field label="Query" hint={`Up to ${MAX_QUERY_CHARS} characters. Queries are sent in the request body, not the URL.`}>
            {({ id, describedBy }) => (
              <Input
                id={id}
                type="search"
                value={query}
                maxLength={MAX_QUERY_CHARS}
                onChange={(e) => setQuery(e.target.value)}
                aria-describedby={describedBy}
                placeholder="e.g. termination notice period"
                required
              />
            )}
          </Field>

          <div className="grid gap-4 sm:grid-cols-3">
            {mode === "hybrid" && (
              <Field label="Retrieval">
                {({ id }) => (
                  <Select id={id} value={hybridMode} onChange={(e) => setHybridMode(e.target.value as HybridMode)}>
                    {HYBRID_MODES.map((m) => (
                      <option key={m.value} value={m.value}>
                        {m.label}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
            )}
            <Field label="Results">
              {({ id }) => (
                <Select id={id} value={topK} onChange={(e) => setTopK(Number(e.target.value))}>
                  {Array.from({ length: MAX_TOP_K }, (_, i) => i + 1).map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <div className="space-y-1.5">
              <span className="block text-sm font-medium">Scope</span>
              <DocumentFilter documents={ready} selected={selected} onChange={setSelected} />
            </div>
          </div>

          <div className="flex justify-end">
            <Button type="submit" pending={pending} disabled={!query.trim()}>
              {pending ? "Searching" : "Search"}
            </Button>
          </div>
        </form>
      </Card>

      <div className="mt-6" aria-live="polite">
        {error ? <Alert title="Search failed">{describeError(error)}</Alert> : outcome && <SearchResults outcome={outcome} />}
      </div>
    </>
  );
}
