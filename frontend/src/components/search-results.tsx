import Link from "next/link";

import { Badge, Card, EmptyState } from "@/components/ui";
import type { HybridSearchResponse, HybridSearchResult, SearchResponse, SearchResult } from "@/lib/api/types";
import { formatScore } from "@/lib/format";

export type SearchOutcome =
  | { kind: "semantic"; response: SearchResponse }
  | { kind: "hybrid"; response: HybridSearchResponse };

function Location({ result }: { result: SearchResult | HybridSearchResult }) {
  return (
    <p className="text-xs text-muted">
      {result.page_number !== null ? `Page ${result.page_number} · ` : ""}Passage {result.chunk_index + 1}
    </p>
  );
}

function Score({ result, kind }: { result: SearchResult | HybridSearchResult; kind: SearchOutcome["kind"] }) {
  if (kind === "semantic") {
    const r = result as SearchResult;
    return (
      <Badge tone="accent" className="font-mono">
        <span className="font-sans">Cosine similarity</span> {formatScore(r.similarity)}
      </Badge>
    );
  }
  const r = result as HybridSearchResult;
  return (
    <div className="flex flex-wrap justify-end gap-1.5">
      <Badge tone="accent" className="font-mono">
        <span className="font-sans">RRF</span> {formatScore(r.rrf_score, 4)}
      </Badge>
      {r.vector_rank !== null && (
        <Badge className="font-mono">
          <span className="font-sans">Vector #{r.vector_rank}</span>
          {r.vector_similarity !== null && ` · ${formatScore(r.vector_similarity)}`}
        </Badge>
      )}
      {r.full_text_rank !== null && (
        <Badge className="font-mono">
          <span className="font-sans">Keyword #{r.full_text_rank}</span>
        </Badge>
      )}
    </div>
  );
}

export function SearchResults({ outcome }: { outcome: SearchOutcome }) {
  const results: Array<SearchResult | HybridSearchResult> = outcome.response.results;
  const note =
    outcome.kind === "semantic"
      ? "Ranked by cosine similarity between the query and passage embeddings. A ranking signal, not a probability."
      : "Ranked by Reciprocal Rank Fusion of vector and keyword results. RRF scores only order results within this search.";

  return (
    <section aria-labelledby="results-heading" className="space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="results-heading" className="text-sm font-semibold">
          {results.length} result{results.length === 1 ? "" : "s"}
        </h2>
        <p className="text-xs text-muted">Model: {outcome.response.embedding_model}</p>
      </div>
      <p className="text-xs text-muted">{note}</p>
      {results.length === 0 ? (
        <Card>
          <EmptyState title="No matching passages">
            Nothing in your ready documents matched. Try different wording, another mode, or check that your documents
            have finished indexing.
          </EmptyState>
        </Card>
      ) : (
        <ol className="space-y-3">
          {results.map((result) => (
            <li key={result.chunk_id}>
              <Card className="p-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <Link href={`/documents/${result.document_id}`} className="block truncate font-medium hover:text-accent">
                      {result.filename}
                    </Link>
                    <Location result={result} />
                  </div>
                  <Score result={result} kind={outcome.kind} />
                </div>
                {/* Document text is untrusted: rendered as plain text only. */}
                <p className="mt-3 text-sm leading-relaxed whitespace-pre-wrap text-foreground/90">{result.content}</p>
              </Card>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
