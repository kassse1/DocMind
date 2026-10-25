"""Evaluation CLI helpers that need neither a database nor a model."""

import pytest

from app.evaluation.run_search_eval import (
    EvaluationError,
    comparison_rows,
    main,
    parse_ks,
    parse_retrievals,
    render_markdown,
    require_test_database,
    resolve_models,
)

E5 = "intfloat/multilingual-e5-small"
BGE = "BAAI/bge-m3"


def test_resolve_models_aliases_and_current() -> None:
    assert resolve_models("current,bge-m3", E5) == [E5, BGE]
    assert resolve_models("e5-small, current", E5) == [E5]  # duplicates collapse
    assert resolve_models(BGE, E5) == [BGE]


@pytest.mark.parametrize("spec", ["", "gpt", "someone/model"])
def test_resolve_models_rejects_unknown(spec: str) -> None:
    with pytest.raises(ValueError):
        resolve_models(spec, E5)


def test_parse_ks() -> None:
    assert parse_ks("5,1,3,3") == [1, 3, 5]
    for bad in ["", "0,1", "21", "a"]:
        with pytest.raises(ValueError):
            parse_ks(bad)


def test_database_guard_requires_test_suffix() -> None:
    require_test_database("postgresql+psycopg://u:p@127.0.0.1:5432/docmind_test")
    with pytest.raises(EvaluationError, match="_test"):
        require_test_database("postgresql+psycopg://u:p@127.0.0.1:5432/docmind")


def test_cli_refuses_non_test_database(monkeypatch, capsys) -> None:
    monkeypatch.setenv("DOCMIND_TEST_DATABASE_URL", "postgresql+psycopg://u:SECRET@h:1/docmind")
    with pytest.raises(SystemExit) as exc_info:
        main(["--models", "current"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "must end with '_test'" in err
    assert "SECRET" not in err


def test_cli_requires_database_url(monkeypatch, capsys) -> None:
    monkeypatch.delenv("DOCMIND_TEST_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        main([])
    assert "DOCMIND_TEST_DATABASE_URL" in capsys.readouterr().err


def _report() -> dict:
    metrics = {"recall@1": 0.5, "precision@1": 0.5, "mrr": 0.75}
    return {
        "settings": {"k": [1]},
        "models": [
            {
                "model": E5,
                "status": "ok",
                "metrics": {
                    "overall": metrics,
                    "by_language": {"en": metrics},
                    "macro_language": metrics,
                },
                "queries": [
                    {
                        "id": "en-q01",
                        "first_relevant_rank": 2,
                        "retrieved": [{"passage_id": "en-x", "similarity": 0.9}],
                    }
                ],
            },
            {"model": BGE, "status": "unavailable", "error": "OSError: not cached"},
        ],
    }


def test_comparison_rows_cover_every_scope_metric_and_k() -> None:
    rows = comparison_rows(_report()["models"], [1])
    assert {(r["scope"], r["metric"], r["k"]) for r in rows} == {
        (scope, metric, 1)
        for scope in ("overall", "en", "macro_language")
        for metric in ("recall", "precision", "mrr")
    }
    assert all(r["model"] == E5 for r in rows)  # unavailable models are not compared


def test_markdown_lists_failures_and_unavailable_models() -> None:
    text = render_markdown(_report())
    assert "| intfloat/multilingual-e5-small | semantic | overall | 0.500 | 0.500 | 0.750 |" in text
    assert "unavailable: OSError: not cached" in text
    assert "en-q01: first relevant rank 2, top-1 en-x (0.9)" in text
    assert "intfloat/multilingual-e5-small [semantic]: 1 queries" in text


def test_parse_retrievals() -> None:
    assert parse_retrievals("semantic") == ["semantic"]
    assert parse_retrievals("hybrid, full_text,hybrid") == ["hybrid", "full_text"]
    for bad in ["", "bm25", "semantic,vector"]:
        with pytest.raises(ValueError):
            parse_retrievals(bad)
