# 0015: The response agent and its tools

- Status: proposed with SAV-17. It takes effect when SAV-17's pull request is merged. The points under [For Muhammed](#for-muhammed) are open questions, not approved exceptions.
- Date: 2026-11-20
- Ticket: SAV-17

## Context

ARCHITECTURE §3.5 says the response agent (DeepSeek V4.1 Flash) understands a DM, calls tools and writes the reply, and never queries the database itself. Record 0004 says answers that list saved items are an intro plus one reply per result that quotes the saved reel, capped at about 5, with fallbacks. Record 0005 says replies work in any language and a correction moves one item without creating a synonym. Record 0011 says a language without built-in reply wording needs wording from the caller. Record 0014 gives search's result: `relaxed`, `level`, `fallbacks`, `incomplete`, `normalized` and `availability_gaps`.

Two parts of the ticket depend on tickets that have not started: `resolve_choice` needs SAV-15's "which one?" flow, and "the second one" needs the last shown results, which short-term memory (SAV-18) keeps.

## Decision

1. **The database is reached only through `app/agent/tools.py`.** `Toolbox.for_user(ToolContext(user_id, country))` binds the tools to the DM's sender; no tool takes a user id, and another user's item is answered exactly like a missing one. `tests/agent/test_agent_boundary.py` fails if the agent's loop, its prompts or the answer delivery import the database layer, search or the pipeline.
2. **Tools.** `search_items`, `near`, `get_item`, `update_item` and `set_status`. Arguments are Pydantic models with unknown keys refused, and search filters are checked against the content type by `plan_search`. A wrong argument, an unknown item or a missing country comes back to the model as an error it can act on (`ask_user_for: country`). The tools read the registry and never name a type or a filter: `near` searches the type that declares a `LOCATION` filter, `open_now` the filter whose source is `OPENING_HOURS`.
3. **The final answer is JSON** (`language`, `reply`, `notice`, `item_ids`) and is checked before it is accepted: only items a tool returned this turn or in the working set, a notice when a search fell back, and a reply that fits in one DM. A failed check is sent back to the model as a system message. The loop is capped at `AGENT_MAX_STEPS`, and the last call has `tool_choice="none"`, so it ends with an answer or `AgentStepLimitError`.
4. **Notices** come from search's result, not from the model's judgement: `backup_mode` when a step went to L1, `broader_results` when a result was found only at L2, `may_be_incomplete` when the time budget ran out. The model writes the sentence in the user's language; the agent makes sure there is one.
5. **Language.** The model replies in the language of the user's message and names it with a BCP 47 tag. For a language without built-in wording (record 0011), the agent asks the model once for the `ReplyWording` fields in that language, translated from the English ones. Wording that breaks the placeholder rules raises `AgentWordingError`; the user is never answered in another language.
6. **Delivery** (`app/agent/replies/answers.py`): the intro, then one message per result, at most `AGENT_MAX_RESULTS_PER_ANSWER`. Per result: a reply quoting the saved message with the Maps or TMDB link; if Meta refuses it (`MessengerRequestError`), the post link as a preview card; then the name with the link. A closed window stops the answer. Any other messenger error is raised and not retried as another step, so a message that may have been delivered is not sent twice.
7. **Working set (proposal for SAV-18).** `AgentTurn.working_set` is the ordered ids of the user's last answer, and `AgentAnswer.working_set` the ordered ids of this one, shown and "show more" alike. The agent shows the model the list by number, so "the second one" is resolved by the model to an id. The agent keeps nothing between messages; storing the list is SAV-18's.
8. **Corrections** (`app/pipeline/vocabulary/corrections.py`) normalize the user's wording through the vocabulary's normal steps, put it on that item only with `user_set: true` in `raw_values`, and replace the path's provenance in the normalize step. The alias that produced the old value gets `correction_count + 1`. `verify_alias` now refuses a candidate with any correction. Flagging a verified alias for review needs corrections from **several users** (ARCHITECTURE §3.4); the count alone can't tell users apart, so nothing is flagged until the owner decides how to keep that evidence (Q4).
9. **Links.** The Maps or TMDB link is read from `entity.data["link"]`. The resolvers (SAV-15) store it there: Google Places returns `googleMapsUri`, and the TMDB page URL is built from the title's id. The agent builds no URL itself.
10. **"Delete my data" from the DM** (owner update of 20 Nov 2026; revised after interim review round 1). The model can never delete on its own word; code decides. `request_data_deletion` stores nothing: it makes the answer the fixed `deletion_confirmation` question and prepares the fixed `deletion_done` message in the same language (`AgentAnswer.deletion_confirmation`). `send_answer` (`app/agent/send.py`) stores the request in Redis (`app/agent/deletion_requests.py`) only after the whole answer was sent, with its deadline (`DATA_DELETION_CONFIRM_SECONDS` after delivery), language and "done" text; a question that failed to generate, translate or send leaves nothing to confirm. `UserTools.begin_turn` takes the request out at the start of the user's **next** turn, so "no" or any other message uses it up and a same-turn request never counts. `confirm_data_deletion` takes no arguments and erases only when the turn also carries the platform's explicit confirmation (`AgentTurn.deletion_confirmed`, Q9) and the deadline has not passed, checked right before erasing. It reads the sender's platform ids, then runs `UserDataEraser` (`app/db/deletion.py`): every store outside Postgres in order (ingestion's Redis state and jobs, the pending request, the media store), then `delete_user_data`, in one transaction under the user's row lock. A successful erasure ends the turn at once with the prepared "done" message and `deleted_recipients`: no model call, translation or other tool runs after it. Both templates are `ReplyWording` fields with built-in English and Turkish; other languages are translated with the rest of the wording, before anything is deleted.
11. **Prompt wording** lives in `app/agent/prompts.py` as `AGENT_PROMPTS`, version `2026-11-20.3`, proposed and not yet approved by the owner (Q3 below).

## For Muhammed

| # | Choice made here | Why, and what the other option is |
|---|---|---|
| Q1 | `resolve_choice` is not built | Jarvis asked for a proposal first. Proposal: SAV-15 stores each pending "which one?" question with its item id and candidate ids, and the quick reply's payload is `choice:<item id>:<candidate number>`; `resolve_choice(item_id, option)` then links the chosen candidate as the item's entity, or marks "none of these". SAV-15 or a follow-up adds it |
| Q2 | The working set is an input and an output of the agent (point 7) | Without SAV-18 the test passes the last results in. Another option: SAV-18 stores it in Redis and a tool reads it; the agent's contract would stay the same |
| Q3 | Prompt wording version `2026-11-20.3` is new and not approved | There is no eval set for the agent yet (SAV-19/20), so there are no before/after numbers. The tests only use a fake model |
| Q4 | No review flag for verified aliases yet | §3.4 asks for corrections from several users. `correction_count` can't tell users apart, and one user correcting several items is not that evidence. A count-only shortcut is not approved. Proposal: record `corrected_alias_id` in the corrected path's normalize-step provenance (the run's JSON output, no schema change) and count distinct `saved_item.user_id` over it, like the verification query does; or a new table. Either needs the owner's decision |
| Q5 | No chat worker yet | Ingestion queues `answer_chat_message` jobs, but nothing runs them. The worker needs the history and working set (SAV-18) and the user's IGSID and last message time. Suggest it with SAV-18, or a small follow-up ticket |
| Q6 | `set_status(item_id, status, entity_position?)` instead of §3.5's `set_status(entity_id, status)` | The model only sees item ids; a list post needs a position. Items without an entity (study posts) can't store a status: `USER_ENTITY` needs an entity. Should study items get a "done" status elsewhere? |
| Q7 | Whether Instagram showed the quote cannot be seen | Meta answers 200 with a message id either way. Only a refused reply falls back. Record 0004 leaves old or deleted messages untested |
| Q9 | No deletion runs until the platform reports an explicit confirmation (`AgentTurn.deletion_confirmed`), which nothing sets yet | The owner update wants no keyword list, and the interim review showed the model alone can't be trusted to tell "yes" from "no". Proposal: the confirmation question carries a "Yes, delete" quick-reply button with a fixed payload; the adapter reads `message.quick_reply.payload`, and the chat worker (Q5) sets `deletion_confirmed` only for that payload on the message right after the delivered question. Until the owner decides, deletion from the DM fails closed |
| Q8 | `entity.data["link"]` as SAV-15's storage contract (point 9) | Alternative: build the Maps and TMDB URLs in code from `(source, external_id)`; that is resolver logic outside `app/resolvers/` |

## Alternatives considered

- **A "reply" tool for the final answer** instead of a JSON output schema: one more tool for the model to learn, and the answer would still need the same checks.
- **Trusting the model to write notices:** a model that forgets one would hide that search fell back, which §3.6 forbids.
- **Showing the hits of the last search automatically** instead of the model's `item_ids`: the model could not drop a wrong hit or order results from two searches.
- **Treating every Send API error as a reason to fall back:** a timeout or 5xx may have delivered the message, so a fallback would duplicate it (record 0011).
- **A fixed English result line for languages without wording:** breaks record 0005 §9.

## Consequences

- **SAV-15:** stores `entity.data["link"]`, and fetches the rows in `AgentAnswer.availability_gaps`. `resolve_choice` per Q1.
- **SAV-18:** stores `AgentAnswer.working_set` and the conversation, passes them in `AgentTurn`, and stores the user's country once asked.
- **New settings:** `AGENT_MAX_STEPS`, `AGENT_MAX_RESULTS_PER_ANSWER`, `AGENT_MAX_OUTPUT_TOKENS`, `DATA_DELETION_CONFIRM_SECONDS`.
- **SAV-18** adds its Redis keys (history, working set) to `UserDataEraser`'s stores.
- **Costs:** one text-model call per step; one more per answer only for a language without built-in wording.
