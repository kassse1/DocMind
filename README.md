# DocMind

Upload PDF/DOCX/TXT documents, then run semantic search and RAG answers with validated citations.
Work in progress. Phase 1 is done: FastAPI skeleton, config, health endpoint, and tests.

## Quick start (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest -v
uvicorn app.main:app --reload
# open http://127.0.0.1:8000/api/v1/health
```
