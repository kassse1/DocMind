"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState, type FormEvent } from "react";

import { AnswerView } from "@/components/answer";
import { DocumentFilter, useReadyDocuments } from "@/components/document-filter";
import { Alert, Button, Card, Field, PageHeader, Select, Textarea } from "@/components/ui";
import { api } from "@/lib/api/client";
import { ApiError, describeError } from "@/lib/api/errors";
import { DEFAULT_TOP_K, MAX_QUERY_CHARS, MAX_TOP_K, type AskResponse, type RetrievalMode } from "@/lib/api/types";

export default function AskPage() {
  return (
    <Suspense>
      <AskView />
    </Suspense>
  );
}

function AskView() {
  const params = useSearchParams();
  const { ready } = useReadyDocuments();
  const [question, setQuestion] = useState("");
  const [retrieval, setRetrieval] = useState<RetrievalMode>("semantic");
  const [topK, setTopK] = useState(DEFAULT_TOP_K);
  const [selected, setSelected] = useState<string[]>(() => {
    const doc = params.get("document");
    return doc ? [doc] : [];
  });
  const [response, setResponse] = useState<AskResponse | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => () => controller.current?.abort(), []);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const text = question.trim();
    if (!text || pending) return;
    const current = new AbortController();
    controller.current = current;
    setPending(true);
    setError(null);
    setResponse(null);
    try {
      setResponse(
        await api.ask(
          { question: text, top_k: topK, retrieval_mode: retrieval, document_ids: selected.length ? selected : undefined },
          current.signal,
        ),
      );
    } catch (reason) {
      if (!current.signal.aborted) setError(reason);
    } finally {
      if (!current.signal.aborted) setPending(false);
    }
  }

  function cancel() {
    controller.current?.abort();
    setPending(false);
  }

  const unavailable = error instanceof ApiError && error.code === "generation_unavailable";

  return (
    <>
      <PageHeader
        title="Ask"
        description="Ask a question about your documents. Answers come only from your ready documents and cite the passages they rely on; when the evidence is not there, DocMind says so."
      />
      <Card className="p-5">
        <form onSubmit={onSubmit} className="space-y-4">
          <Field label="Question" hint={`${question.length}/${MAX_QUERY_CHARS} characters`}>
            {({ id, describedBy }) => (
              <Textarea
                id={id}
                rows={3}
                value={question}
                maxLength={MAX_QUERY_CHARS}
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) e.currentTarget.form?.requestSubmit();
                }}
                aria-describedby={describedBy}
                placeholder="e.g. What is the notice period for ending the lease?"
                required
              />
            )}
          </Field>
          <div className="grid gap-4 sm:grid-cols-3">
            <Field label="Retrieval">
              {({ id }) => (
                <Select id={id} value={retrieval} onChange={(e) => setRetrieval(e.target.value as RetrievalMode)}>
                  <option value="semantic">Semantic (default)</option>
                  <option value="hybrid">Hybrid</option>
                </Select>
              )}
            </Field>
            <Field label="Passages retrieved">
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
          <div className="flex flex-wrap items-center justify-end gap-3">
            <span className="mr-auto text-xs text-muted">Ctrl+Enter to submit. Answers from a local model can take a while.</span>
            {pending && (
              <Button variant="ghost" onClick={cancel}>
                Cancel
              </Button>
            )}
            <Button type="submit" pending={pending} disabled={!question.trim()}>
              {pending ? "Answering" : "Ask"}
            </Button>
          </div>
        </form>
      </Card>

      <div className="mt-6" aria-live="polite">
        {error ? (
          <Alert tone={unavailable ? "warning" : "danger"} title={unavailable ? "Question answering is unavailable" : "Could not answer"}>
            {describeError(error)}
            {unavailable && " Search still works."}
          </Alert>
        ) : (
          response && <AnswerView response={response} />
        )}
      </div>
    </>
  );
}
