# AGENTS.md

Rules for every AI agent (and human) working on this project. If anything here conflicts with a ticket description, **this file wins**. Ask the project owner if you're unsure.

## About the project

### The problem
People save posts on Instagram all the time: a restaurant to try, a movie to watch, a study tip, a recipe. Instagram lets you sort saves into collections (Food, Movies, Studying), but it doesn't let you search or filter inside them. You can't stand in Beşiktaş and ask "which restaurants did I save around here?", or ask "which of my saved horror movies are on Netflix?". The saves pile up and get forgotten, so saving loses its point.

### What we're building
**SavedAgent** is a chat agent for the things you save.
1. You share a post or reel to our Instagram bot by DM, as easily as tapping Save.
2. SavedAgent works out what the post is about (a place, a movie, a study tip, anything). It looks up the real thing behind it, such as the restaurant on Google Maps or the movie on TMDB, and indexes it once.
3. Later you ask in plain language, in any language: "restaurants in Beşiktaş in my Food collection", "cafés in Brooklyn I saved", "horror movies on Netflix", "my car review videos". It answers from your own saves, with links.

Instagram comes first. TikTok, X and Facebook follow through the same backend.

### Why it's worth building
- **It solves a real, daily problem.** Collections are where saves go to be forgotten. Being able to filter by place, genre, platform or subject turns them into something useful.
- **It works for any kind of content, for anyone, anywhere.** Every save gets an open category ("restaurant", "car review", "guitar lesson"), so nothing is forced into a fixed list. Places work in any country, and questions and answers work in any language. Places, movies and the like get extra behaviour as built-in types (decision record 0005).
- **It understands the video**, not just the caption. Many reels only say the restaurant's name out loud.
- **It stays on the right side of the platforms.** It uses official APIs only and never asks for a user's password, so it can't get users' accounts banned.
- **It's cheap to run.** Each post is analyzed once, when it's saved, so answering a question costs almost nothing.

### What it isn't
- Not a scraper, and not a tool that logs in to anyone's account.
- Not a general chatbot. It answers questions about the user's own saves.
- Not a social feature. Nothing a user saves is shown to other users.

### Where things are
- **Design (the source of truth):** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). **Read it before starting any task.**
- **Shared contract for all tickets:** SAV-22 in the Multica workspace "SavedAgent". It holds owner-approved decisions that may be newer than this file.
- **Owner:** Muhammed. Only he approves pushes, pull requests and merges.

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
- **Team loop in Multica** (details in SAV-22 §7 and SAV-24). Jarvis orchestrates, the Developer builds and the Reviewer reviews:
  1. The Developer commits locally (no push) and hands the exact SHA, the base SHA and its worktree path to the Reviewer.
  2. The Reviewer reviews that SHA in its own detached worktree and replies APPROVE or CHANGES REQUESTED.
  3. Jarvis runs the fix and re-review rounds. After APPROVE, Jarvis asks the owner once to approve the push and the pull request.
  4. Only then the Developer pushes and opens the pull request, and only the owner merges.
- **Handoffs happen only by mention.** Agents start work when a comment mentions them; a finished run wakes nobody up. Every handoff or stop is a comment that mentions the next agent and Jarvis.
- **Work in a git worktree next to the main repo, never in hidden folders.** The owner's checkout of the main repo is not touched: no edits, branch switches or commits there. Each ticket gets one worktree named after its branch, all in one folder next to the repo:
  ```bash
  git -C <main repo> fetch origin
  git -C <main repo> worktree add ../SavedAgents-worktrees/sav-11-instagram-webhook -b sav-11-instagram-webhook origin/main
  ```
  Parallel tickets get separate worktrees in that same folder. Reviews use a detached `../SavedAgents-worktrees/review-<ticket>` worktree that is removed afterwards. Each worktree has its own `.venv` and Docker Compose project name; `.env` is never copied into it. After a merge, the ticket's worktree is removed with `git worktree remove`. Full rule: SAV-22, 17 Nov 2026 update.
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
- **No closed lists of real-world values, and nothing tied to one place or language.** Categories, cuisines, genres, subjects, platforms and areas come from real data through the vocabulary, Google Places or TMDB, never from an enum or list in code. Code must work for any country and any language (decision record 0005).
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
