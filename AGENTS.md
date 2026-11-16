# AGENTS.md

Rules for every AI agent (and human) working on this project. If anything here conflicts with a ticket description, **this file wins**. Ask the project owner if you're unsure.

## Project in one paragraph

**SavedAgent** indexes posts a user saves on Instagram (later TikTok, X, Facebook) and lets them search by chat: places by district or "near me", movies by genre or streaming platform, study material by subject, and more. The full design is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). **Read it before starting any task**, and treat it as the source of truth.

## 1. Approval gates (never skip these)

You **must get explicit approval from the project owner** before you:

- **Push** to any remote (no `git push` of any branch, ever, without approval).
- **Open, merge or close a pull request.**
- **Deploy** anything or change any hosted environment.
- **Add a new dependency, paid API or external service.**
- **Change the architecture** in a way that differs from `docs/ARCHITECTURE.md`.
- **Delete or overwrite data**: database rows, migrations already applied, stored media, or files you didn't create in this task.
- **Change secrets, credentials, CI/CD or repository settings.**

Never, even with approval:
- Force-push, rewrite published history, or commit directly to `main`.
- Commit secrets, tokens, `.env` files or real user data.
- Store or log a user's social-media password, or log in to any platform as a user from the backend.

When you need approval, stop and post: what you want to do, why, what changes, and how to undo it.

## 2. Git workflow

- One ticket = one branch = one pull request. Branch from the latest `main`. Name branches `<ticket-key>-<short-topic>` in lowercase, e.g. `sav-11-instagram-webhook`. Tickets live in the Multica workspace "SavedAgent" (ticket keys `SAV-N`), project "SavedAgent: Instagram saved-posts agent".
- Agent flow in Multica: Jarvis plans and assigns, Developer builds, Reviewer reviews. Developer commits locally, asks the owner to approve the push, pushes only after that approval, then hands the branch to Reviewer. The pull request is opened only after the review passes and the owner approves it, and only the owner merges.
- **Commit locally as often as you like.** Pushing needs approval (see §1).
- Commit messages use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`. The first line is ≤ 72 characters and in the imperative mood.
- Before asking for approval to push, report on the ticket: the branch, head commit SHA, a short summary of changes, the test and lint results, and anything left open.

## 3. Clean code

**Language and tooling:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (async), `arq`, pytest.
All of these must pass before you ask for approval:

```bash
ruff format --check .
ruff check .
mypy app
pytest
```

**Rules**
- **Type hints everywhere.** No untyped public functions. Avoid `Any` unless there's a comment explaining why.
- **Small, single-purpose functions and modules.** If a function needs a comment to explain *what* it does, split it or rename it.
- **Clear names.** `resolve_place_candidates()`, not `process()`. No abbreviations except well-known ones (`id`, `url`, `db`).
- **No magic values.** Thresholds, model names, limits and URLs live in `app/config.py` (loaded from environment variables).
- **No dead code.** No commented-out code, unused imports or "just in case" functions. Git remembers.
- **Explicit errors.** No bare `except:` and no swallowed exceptions. Catch specific errors, add context, and re-raise or handle them.
- **Logging, not `print`.** Use structured logging. Never log secrets, tokens, raw webhook bodies or personal data.
- **Async I/O.** All network and database calls are `async`. Never block the event loop.
- **Respect the boundaries in `docs/ARCHITECTURE.md`:**
  - Model SDKs (Gemini, DeepSeek, Whisper, embeddings) are only called from `app/models/`, through the provider interfaces. Never import them elsewhere.
  - Platform-specific code stays in `app/adapters/`. Everything after the adapter uses `CaptureEvent`.
  - Content-type logic (schemas, decision questions, filters, resolvers) lives in `app/types/` and `app/resolvers/`. No `if type == "movie"` scattered through the pipeline.
  - The response agent never queries the database directly. It goes through tools in `app/agent/tools.py`.
- **Don't over-engineer.** Build what the ticket asks for. Add abstractions only when there's a second real use for them.

## 4. Tests

- Every change comes with tests. A bug fix starts with a test that fails without the fix.
- **No real external calls in tests.** Mock Meta, Google, TMDB, DeepSeek, Gemini and others. Use recorded, anonymized payloads in `tests/fixtures/`.
- Test behaviour, not implementation details. Name tests after what they check: `test_duplicate_share_does_not_create_second_item`.
- Model-quality changes (prompts, thresholds, model swaps) are also scored against `evals/labeled_saves.jsonl`. Report the before/after numbers.

## 5. Clean documentation

- **Docstrings** on every public module, class and function, in Google style. Say what it does, its arguments, what it returns and what it raises. Keep them short and accurate.
- **Comments explain *why*, not *what*.** Delete comments that just repeat the code.
- **Keep docs in sync with code in the same change:**
  - Design change (approved, see §1) → update `docs/ARCHITECTURE.md`.
  - Significant decision (a new model, service or data-model change) → add a short decision record in `docs/decisions/NNNN-title.md`: context, decision, alternatives, consequences.
  - New setup step, environment variable or command → update `README.md` and `.env.example`.
- Write in plain English. Short sentences, concrete examples, and no marketing language.

## 6. Security and privacy

- Secrets come only from environment variables. `.env` is git-ignored, and `.env.example` lists every variable with a dummy value.
- Verify every Meta webhook's `X-Hub-Signature-256` before processing it.
- Store only the data a feature needs. Every new user-data field must also be deleted by "delete my data".
- Long-term memory never stores sensitive categories (health, religion, politics, sexuality, finances).
- Platforms: server-side code uses **official APIs only**. No scraping, and no unofficial private APIs.

## 7. Working style

- **Ask when unsure.** Don't guess at requirements, and don't silently change a ticket's scope. Post a question on the ticket instead.
- Stay inside the ticket. If you find another problem, note it on the ticket (or suggest a new one). Don't fix it in the same branch.
- Report honestly: if tests fail or something was skipped, say so with the output.

## Definition of done

- [ ] The ticket's acceptance criteria are met.
- [ ] `ruff format`, `ruff check`, `mypy` and `pytest` pass.
- [ ] Tests were added or updated. Evals were run if model behaviour changed.
- [ ] Docs were updated (docstrings, README, ARCHITECTURE, decision records, as needed).
- [ ] There are no secrets, debug code or commented-out code.
- [ ] The branch, SHA, summary and test results are posted on the ticket, and **approval to push was requested, not assumed**.
