import Link from "next/link";

import { CheckIcon, DashIcon } from "@/components/icons";
import { Badge, Card } from "@/components/ui";
import type { AbstentionReason, AskResponse, Grounding } from "@/lib/api/types";

/** Plain-language explanations of app/rag/grounding.py AbstentionReason. */
export const ABSTENTION_TEXT: Record<AbstentionReason, string> = {
  no_retrieved_chunks: "No passage in your ready documents matched the question.",
  no_eligible_context: "Matching passages were found, but none could be used as context.",
  model_insufficient_evidence: "The model found that your documents do not contain enough evidence to answer.",
  empty_model_output: "The model returned an empty reply.",
  malformed_model_output: "The model's reply could not be read in the required format.",
  missing_citations: "The model answered without citing any passage, so the answer was withheld.",
  invalid_citation: "The model cited a passage it was not given, so the answer was withheld.",
  unverified_quote: "A quote in the answer was not found in the cited passage, so the answer was withheld.",
  unsupported_number: "The answer contained a number that does not appear in the cited passages, so it was withheld.",
  unsupported_claim: "An independent check judged the answer unsupported by the cited passages.",
  verification_inconclusive: "The independent support check could not reach a verdict, so the answer was withheld.",
};

function Check({ ok, children }: { ok: boolean; children: string }) {
  return (
    <li className="flex items-center gap-2">
      <span className={ok ? "text-success" : "text-muted"}>{ok ? <CheckIcon /> : <DashIcon />}</span>
      <span>{children}</span>
    </li>
  );
}

function GroundingChecks({ grounding }: { grounding: Grounding }) {
  return (
    <div className="space-y-2 text-sm">
      <h3 className="font-medium">What the server checked</h3>
      <ul className="space-y-1.5 text-muted">
        <Check ok={grounding.citation_ids_valid}>Every citation names a passage the model was given</Check>
        <Check ok={grounding.quotes_verified}>Every quote appears in its cited passage</Check>
        <Check ok={grounding.numbers_verified}>Every number in the answer appears in a cited passage</Check>
        <Check ok={grounding.support_check === "model_assisted"}>
          {grounding.support_check === "model_assisted"
            ? "A second model pass judged the passages to support the answer"
            : "Model-assisted support check not enabled"}
        </Check>
      </ul>
      <p className="text-xs text-muted">
        These checks do not prove the answer is correct: nothing verifies that it follows from the quotes. Read the cited
        passages before relying on it.
      </p>
    </div>
  );
}

export function AnswerView({ response }: { response: AskResponse }) {
  const meta = [
    response.generation_model && `Model: ${response.generation_model}`,
    `Retrieval: ${response.retrieval.mode}, ${response.retrieval.context_chunks} of ${response.retrieval.retrieved_chunks} passages in context`,
    `${Math.round(response.timing_ms.total_ms)} ms`,
  ].filter(Boolean);

  if (response.abstained) {
    const reason = response.abstention_reason;
    return (
      <Card className="space-y-3 p-5" >
        <div className="flex items-center gap-2">
          <Badge tone="warning">No answer</Badge>
          <h2 className="text-sm font-semibold">Not enough evidence to answer</h2>
        </div>
        <p className="text-sm">
          {reason ? ABSTENTION_TEXT[reason] : "The question could not be answered from your documents."}
        </p>
        <p className="text-sm text-muted">
          DocMind answers only from your documents and declines rather than guess. Try rephrasing, widening the document
          scope, or check that the relevant document is ready.
        </p>
        <p className="text-xs text-muted">{meta.join(" · ")}</p>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      <Card className="space-y-4 p-5">
        <div className="flex items-center gap-2">
          <Badge tone="success">Answer</Badge>
          <span className="text-xs text-muted">Generated from {response.citations.length} cited passage{response.citations.length === 1 ? "" : "s"}</span>
        </div>
        {/* Model output is untrusted: plain text only, never HTML or markdown. */}
        <p className="text-base leading-relaxed whitespace-pre-wrap">{response.answer}</p>
        {response.grounding && <GroundingChecks grounding={response.grounding} />}
        <p className="text-xs text-muted">{meta.join(" · ")}</p>
      </Card>

      <section aria-labelledby="citations-heading" className="space-y-3">
        <h2 id="citations-heading" className="text-sm font-semibold">
          Sources
        </h2>
        <ol className="space-y-3">
          {response.citations.map((citation) => (
            <li key={`${citation.source_id}-${citation.chunk_id}`}>
              <Card className="p-5">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <Link href={`/documents/${citation.document_id}`} className="block truncate font-medium hover:text-accent">
                      {citation.filename}
                    </Link>
                    <p className="text-xs text-muted">
                      {citation.page_number !== null ? `Page ${citation.page_number} · ` : ""}Passage {citation.chunk_index + 1}
                    </p>
                  </div>
                  <Badge className="font-mono">{citation.source_id}</Badge>
                </div>
                {citation.quotes.length > 0 && (
                  <div className="mt-3 space-y-2">
                    {citation.quotes.map((quote) => (
                      <blockquote
                        key={`${quote.char_start}-${quote.char_end}`}
                        className="border-l-2 border-accent pl-3 text-sm leading-relaxed whitespace-pre-wrap text-foreground/90"
                      >
                        {quote.text}
                      </blockquote>
                    ))}
                  </div>
                )}
              </Card>
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}
