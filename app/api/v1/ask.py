from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_embedder, get_generator, get_token_counter
from app.core.config import Settings, get_settings
from app.core.database import get_db_session
from app.core.errors import DatabaseUnavailableRoute
from app.embeddings.base import Embedder
from app.models import User
from app.rag.generation import Generator
from app.rag.grounding import GROUNDING_POLICY, Abstention, GroundedAnswer
from app.rag.tokens import TokenCounter
from app.schemas.ask import (
    AskRequest,
    AskResponse,
    Citation,
    CitedQuote,
    Grounding,
    RetrievalInfo,
    Timing,
)
from app.services import ask as ask_service
from app.services.ask import AskOutcome

router = APIRouter(prefix="/ask", tags=["ask"], route_class=DatabaseUnavailableRoute)


def _citations(answer: GroundedAnswer) -> list[Citation]:
    return [
        Citation(
            source_id=c.source.source_id,
            document_id=c.source.document_id,
            filename=c.source.filename,
            chunk_id=c.source.chunk_id,
            chunk_index=c.source.chunk_index,
            page_number=c.source.page_number,
            char_start=c.source.char_start,
            char_end=c.source.char_end,
            quotes=[
                CitedQuote(text=q.text, char_start=q.char_start, char_end=q.char_end)
                for q in c.quotes
            ],
        )
        for c in answer.citations
    ]


def _retrieval_info(
    outcome: AskOutcome,
    body: AskRequest,
    embedder: Embedder,
    counter: TokenCounter,
    selection: str,
) -> RetrievalInfo:
    context = outcome.context
    return RetrievalInfo(
        mode=body.retrieval_mode,
        top_k=body.top_k,
        embedding_model=embedder.model_name,
        retrieved_chunks=outcome.retrieved,
        excluded_by_selection=outcome.excluded_by_selection,
        context_selection=selection,
        context_chunks=len(context.sources) if context else 0,
        excluded_empty=context.excluded_empty if context else 0,
        excluded_over_budget=context.excluded_over_budget if context else 0,
        context_tokens=context.tokens if context else 0,
        context_token_budget=outcome.token_budget,
        token_counter=counter.name,
    )


@router.post(
    "",
    response_model=AskResponse,
    responses={
        401: {"description": "Authentication not configured (prod)"},
        404: {"description": "A document in document_ids does not exist or is not yours"},
        422: {"description": "Empty or too long question, top_k out of range, invalid filter"},
        503: {
            "description": "Generation provider not configured or unavailable, embedding "
            "model unavailable, or database unavailable"
        },
    },
)
def ask(
    body: AskRequest,
    # Declared first: with generation disabled the request fails before touching the DB.
    generator: Annotated[Generator, Depends(get_generator)],
    session: Annotated[Session, Depends(get_db_session)],
    user: Annotated[User, Depends(get_current_user)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    counter: Annotated[TokenCounter, Depends(get_token_counter)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AskResponse:
    """Answer a question from your documents, with citations checked by the server.

    abstained=true (with abstention_reason) means no grounded answer could be given; it is
    a normal outcome, not an error. Citations are verified to name passages the model was
    shown and to quote them; the server does not verify that the answer follows from them
    (grounding.entailment_verified is always false).
    """
    outcome = ask_service.ask(session, embedder, generator, counter, settings, user, body)
    common = {
        "retrieval": _retrieval_info(
            outcome, body, embedder, counter, settings.rag_context_selection
        ),
        "generation_model": outcome.generation_model,
        "prompt_version": settings.rag_prompt_version,
        "timing_ms": Timing(
            retrieval_ms=round(outcome.retrieval_ms, 1),
            generation_ms=None
            if outcome.generation_ms is None
            else round(outcome.generation_ms, 1),
            total_ms=round(outcome.total_ms, 1),
        ),
    }
    if isinstance(outcome.result, Abstention):
        return AskResponse(
            answer=None,
            abstained=True,
            abstention_reason=outcome.result.reason,
            citations=[],
            grounding=None,
            **common,
        )
    return AskResponse(
        answer=outcome.result.answer,
        abstained=False,
        abstention_reason=None,
        citations=_citations(outcome.result),
        # Reached only when every check passed (any failure abstains instead).
        grounding=Grounding(
            policy=GROUNDING_POLICY,
            citation_ids_valid=True,
            quotes_verified=True,
            numbers_verified=True,
        ),
        **common,
    )
