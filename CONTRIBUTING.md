# Contributing to PrivAiTe

Thanks for your interest in contributing!

## Getting started

```bash
git clone https://github.com/crp4222/PrivAiTe.git
cd PrivAiTe
pip install -e ".[dev]"
python -m spacy download en_core_web_lg
python -m spacy download fr_core_news_md
```

## Running tests

```bash
python -m pytest tests/ -v
python -m ruff check privaite/ tests/
```

## Project structure

- `privaite/pii/`: PII detection, anonymization, de-anonymization
- `privaite/api/`: FastAPI endpoints (OpenAI-compatible)
- `privaite/streaming/`: SSE streaming with token-level de-anonymization
- `privaite/providers/`: LiteLLM-based provider routing
- `tests/`: Unit and integration tests

## What to work on

- Adding PII recognizers for other languages
- Improving false positive filtering
- Supporting more anonymization strategies
- Performance optimizations
- Documentation and examples

## Pull requests

1. Fork the repo
2. Create a branch (`git checkout -b feature/my-feature`)
3. Write tests for your changes
4. Make sure `pytest` and `ruff` pass
5. Open a PR with a clear description

## Code style

- Comments explain WHY (a constraint, a trap, a privacy invariant), never what
  the next line does. Look at `privaite/pii/engine.py` for the house style.
- Docstrings on non-obvious public behavior; skip them on self-explanatory code.
- Run `ruff check --fix` before committing
- Python 3.11+ features are fine
