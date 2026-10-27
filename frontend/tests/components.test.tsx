// Fixture data below is invented for tests and labelled as such; it is never shown in the app.
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AnswerView } from "@/components/answer";
import { StatusBadge, Uploader, checkFile } from "@/components/documents";
import { SearchResults } from "@/components/search-results";
import type { AskResponse, HybridSearchResponse, SearchResponse } from "@/lib/api/types";
import { safeNextPath } from "@/lib/format";
import { FIXTURE_TOKEN, errorResponse, jsonResponse, mockFetch } from "./helpers";

const DOC_ID = "00000000-0000-4000-8000-0000000000d1";
const PASSAGE = {
  document_id: DOC_ID,
  filename: "fixture-contract.txt",
  chunk_id: "00000000-0000-4000-8000-0000000000c1",
  chunk_index: 2,
  page_number: 4,
  char_start: 0,
  char_end: 40,
  content: "Fixture passage <script>alert(1)</script> <b>not bold</b>",
};

const RETRIEVAL = {
  mode: "semantic" as const,
  top_k: 5,
  embedding_model: "intfloat/multilingual-e5-small",
  retrieved_chunks: 5,
  excluded_by_selection: 0,
  context_selection: "rank",
  context_chunks: 3,
  excluded_empty: 0,
  excluded_over_budget: 2,
  context_tokens: 700,
  context_token_budget: 3000,
  token_counter: "bytes",
};

describe("documents", () => {
  it("labels every status", () => {
    const { rerender } = render(<StatusBadge status="pending" />);
    expect(screen.getByText("Queued")).toBeInTheDocument();
    rerender(<StatusBadge status="processing" />);
    expect(screen.getByText("Indexing")).toBeInTheDocument();
    rerender(<StatusBadge status="failed" />);
    expect(screen.getByText("Failed")).toBeInTheDocument();
  });

  it("checks type and size before uploading", () => {
    expect(checkFile(new File(["x"], "notes.md"))).toMatch(/not supported/);
    expect(checkFile(new File([], "empty.txt"))).toMatch(/empty/);
    const big = new File(["x"], "big.pdf");
    Object.defineProperty(big, "size", { value: 11 * 1024 * 1024 });
    expect(checkFile(big)).toMatch(/limit is 10 MB/);
    expect(checkFile(new File(["x"], "Report.PDF"))).toBeNull();
  });

  it("shows a duplicate upload with a link to the existing document", async () => {
    mockFetch((url) =>
      url === "/bff/auth/refresh"
        ? jsonResponse(FIXTURE_TOKEN)
        : errorResponse(409, "duplicate_document", "This file has already been uploaded.", {}, { document_id: DOC_ID }),
    );
    const onUploaded = vi.fn();
    render(<Uploader onUploaded={onUploaded} />);
    await userEvent.upload(screen.getByLabelText("Choose files"), new File(["fixture"], "fixture.txt", { type: "text/plain" }));
    expect(await screen.findByText(/already in your library/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open it" })).toHaveAttribute("href", `/documents/${DOC_ID}`);
    expect(onUploaded).not.toHaveBeenCalled();
  });

  it("rejects an unsupported file without calling the API", async () => {
    const { calls } = mockFetch(() => jsonResponse({}));
    render(<Uploader onUploaded={vi.fn()} />);
    const input = screen.getByLabelText("Choose files");
    await userEvent.upload(input, new File(["x"], "image.png", { type: "image/png" }), { applyAccept: false });
    expect(await screen.findByText(/“image\.png” is not supported/)).toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });
});

describe("search results", () => {
  it("labels cosine similarity and renders document text as text", () => {
    const response: SearchResponse = {
      results: [{ ...PASSAGE, similarity: 0.81234 }],
      metric: "cosine",
      embedding_model: "intfloat/multilingual-e5-small",
    };
    const { container } = render(<SearchResults outcome={{ kind: "semantic", response }} />);
    expect(screen.getByText("Cosine similarity")).toBeInTheDocument();
    expect(screen.getByText(/0\.812/)).toBeInTheDocument();
    expect(screen.getByText(/Page 4 · Passage 3/)).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getByText(/<script>alert\(1\)<\/script>/)).toBeInTheDocument();
  });

  it("shows RRF and branch ranks for hybrid results, never a similarity it did not get", () => {
    const response: HybridSearchResponse = {
      results: [
        { ...PASSAGE, rrf_score: 0.0325, vector_rank: 1, vector_similarity: 0.8, full_text_rank: null, full_text_score: null },
      ],
      mode: "hybrid",
      fusion: "rrf",
      rrf_k: 60,
      candidate_pool: 50,
      embedding_model: "intfloat/multilingual-e5-small",
      full_text_configs: ["simple"],
    };
    render(<SearchResults outcome={{ kind: "hybrid", response }} />);
    expect(screen.getByText("RRF")).toBeInTheDocument();
    expect(screen.getByText(/Vector #1/)).toBeInTheDocument();
    expect(screen.queryByText(/Keyword #/)).toBeNull();
    expect(screen.queryByText("Cosine similarity")).toBeNull();
  });

  it("explains an empty result", () => {
    render(<SearchResults outcome={{ kind: "semantic", response: { results: [], metric: "cosine", embedding_model: "m" } }} />);
    expect(screen.getByText("No matching passages")).toBeInTheDocument();
  });
});

describe("answers", () => {
  const base = {
    retrieval: RETRIEVAL,
    generation_model: "qwen2.5:3b-instruct",
    generation_attempts: 1,
    prompt_version: "grounded-qa-v2",
    timing_ms: { retrieval_ms: 20, generation_ms: 900, verification_ms: null, total_ms: 925 },
  };

  it("renders an answer with its citations and the limits of the checks", () => {
    const response: AskResponse = {
      ...base,
      answer: "Fixture answer <img src=x onerror=alert(1)>",
      abstained: false,
      abstention_reason: null,
      citations: [{ ...PASSAGE, source_id: "S1", quotes: [{ text: "Fixture quote", char_start: 0, char_end: 13 }] }],
      grounding: {
        policy: "fixture",
        citation_ids_valid: true,
        quotes_verified: true,
        numbers_verified: true,
        entailment_verified: false,
        support_check: "not_run",
        support_check_version: null,
      },
    };
    const { container } = render(<AnswerView response={response} />);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText(/Fixture answer <img/)).toBeInTheDocument();
    const sources = screen.getByRole("region", { name: "Sources" });
    expect(within(sources).getByText("Fixture quote")).toBeInTheDocument();
    expect(within(sources).getByRole("link", { name: "fixture-contract.txt" })).toHaveAttribute("href", `/documents/${DOC_ID}`);
    expect(screen.getByText(/do not prove the answer is correct/)).toBeInTheDocument();
    expect(screen.getByText(/support check not enabled/)).toBeInTheDocument();
  });

  it("presents abstention as a normal outcome with the reason", () => {
    const response: AskResponse = {
      ...base,
      answer: null,
      abstained: true,
      abstention_reason: "model_insufficient_evidence",
      citations: [],
      grounding: null,
    };
    render(<AnswerView response={response} />);
    expect(screen.getByText("Not enough evidence to answer")).toBeInTheDocument();
    expect(screen.getByText(/do not contain enough evidence/)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Sources" })).toBeNull();
  });
});

describe("redirect targets", () => {
  it("accepts only local paths", () => {
    expect(safeNextPath("/documents")).toBe("/documents");
    expect(safeNextPath("//evil.example")).toBe("/dashboard");
    expect(safeNextPath("https://evil.example")).toBe("/dashboard");
    expect(safeNextPath("/\\evil.example")).toBe("/dashboard");
    expect(safeNextPath(null)).toBe("/dashboard");
  });

  it("refuses targets that URL parsing turns into another origin (Phase 12 finding)", () => {
    // Tabs and newlines are removed by the URL parser: "/\t/evil" becomes "//evil".
    for (const value of ["/\t/evil.example", "/\n/evil.example", "/\r\n/evil.example", "/\u0000/x"]) {
      expect(safeNextPath(value)).toBe("/dashboard");
      expect(new URL(safeNextPath(value), "http://localhost:3080").origin).toBe("http://localhost:3080");
    }
    // Percent-encoded separators stay inside the path.
    expect(safeNextPath("/%09/evil.example")).toBe("/%09/evil.example");
    expect(safeNextPath("/%2F%2Fevil.example")).toBe("/%2F%2Fevil.example");
    expect(safeNextPath("/documents?x=1#top")).toBe("/documents?x=1#top");
  });

  it("the login page never navigates off-site for a crafted next parameter", () => {
    // What useSearchParams hands over for ?next=/%09/evil.example: the decoded tab.
    const decoded = new URLSearchParams("next=/%09/evil.example").get("next");
    expect(decoded).toBe("/\t/evil.example");
    expect(safeNextPath(decoded)).toBe("/dashboard");
  });
});
