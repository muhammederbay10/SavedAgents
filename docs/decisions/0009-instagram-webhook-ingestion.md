# 0009: How the Instagram webhook stores shares and queues jobs

- Status: proposed with SAV-11. It takes effect when SAV-11's pull request is merged. The points under [For Muhammed](#for-muhammed) are choices made inside the ticket's scope that he may want to change.
- Date: 2026-11-17
- Ticket: SAV-11 (the number 0008 is taken by SAV-7's record on model choices, which is not merged yet)

## Context

`docs/ARCHITECTURE.md` §3.1 and §3.2 say what the webhook does: check the signature, skip duplicates, map the message to a `CaptureEvent` or a chat message, enqueue, and answer 200 at once. They don't say where the saved item is created, what a job looks like, or how "a text sent right after a share" is recognized. Three records already narrow the answers:

- **0002 and 0003:** what the payloads hold. A share and the text after it are two separate webhooks, 9 seconds apart in the recording, with nothing linking them but the sender and the time.
- **0004:** every saved item keeps the id of its message (`mid`).
- **0007:** an item is stored once per post id and once per message id. `post_url` and `platform_post_id` may be missing.

The tickets that run the jobs come later: the indexing pipeline (SAV-14) and the response agent (SAV-17). Their workers don't exist yet, so this ticket defines what they will find in the queue.

## Decision

1. **The webhook writes the saved item itself, and the job only indexes it.** `CaptureEvent.user_id` is our user, so the user has to be found before the event exists. The request therefore does three short pieces of work: find or create the user, insert the `saved_item` as `pending`, and queue one job. Each is a few milliseconds. Everything slow (downloads, models) runs in the job. The item's `saved_at` is the time the user sent the message, as Meta reports it.
2. **Duplicates are stopped by the unique keys of record 0007, not by a separate list of seen message ids.** The item is inserted with `ON CONFLICT DO NOTHING`. When nothing is inserted, the existing item is looked up by post id or message id.
3. **Every job has an id made from what it works on:** `index:<saved item id>` and `chat:<platform>:<message id>`. `arq` refuses a second job with an id it still knows. So a text that Meta delivers twice is one chat job, and two shares of one post are one indexing job.
4. **Database first, queue second, and an error means "send it again".** The item is committed before its job is queued. If Postgres or Redis fails, the endpoint answers 500 and Meta delivers the webhook again. Handling a webhook twice is safe:
   - the item already exists, so no second item is created;
   - its job is queued again only while the item is still `pending`. This covers a first delivery that stored the item and then failed to queue it;
   - a collection hint that is already on the item is left as it is.
5. **Job contract.** Workers register functions with these names and keyword arguments:

   | Job | Keyword arguments |
   |---|---|
   | `index_saved_item` | `saved_item_id` (text), `capture_event` (the `CaptureEvent` as JSON) |
   | `answer_chat_message` | `chat_message` (the `ChatMessage` as JSON) |

   The indexing job gets the whole capture event because `saved_item` has no column for the media link. The signed image or video link exists only in the event, and the pipeline must download it before it expires.
6. **A repeated share changes nothing on the item.** The first share's message id, caption and save time stay. Answers reply to the first message.
7. **The collection hint is a time rule.** A text is the hint of the sender's **latest** captured item when that item was sent at most `COLLECTION_HINT_WINDOW_SECONDS` before the text and has no collection yet. The times are Meta's message times, not our clock, so a slow or repeated delivery doesn't change the result. The text is stored in `saved_item.collection` exactly as the user wrote it ("For my movies collection"), and no chat job is queued for it.
8. **First contact takes a short lock.** Two first messages from one account can arrive together. A Postgres advisory lock on the account makes the second wait and find the first one's user, so an account never gets two users.
9. **One saved item per message.** If a message ever holds two shares, the first is captured and the rest are logged as a warning (`instagram_extra_shares_ignored`). No recorded message has two.
10. **Link or file is decided by the host.** A `url` on `instagram.com` is the post's public page and becomes `post_url`. Any other `url` is a file and becomes `media_url`. This holds for every attachment type, so a signed link can't end up in `post_url` even if Meta changes a payload.
11. **Answers Meta gets:** 200 when the webhook was handled, also when it held nothing for us (read receipts, reactions, the bot's own messages, the dashboard's test sample); 401 for a missing or wrong signature; 400 for a signed body that isn't a webhook object; 500 when Postgres or Redis failed.
12. **Nothing personal is logged.** Logs hold our own ids (user, saved item), the outcome and the attachment type. They never hold the sender's Instagram id, a message id, a caption, a text or a link. The API's database engine hides statement values in its errors for the same reason.

## For Muhammed

These follow the ticket and the earlier records, but each is a product choice you may see differently.

| # | Choice made here | Why, and what the other option is |
|---|---|---|
| Q1 | A question typed within the window after a share is taken as that share's collection, and gets no answer | The ticket asks for a time rule, and code can't tell "For my movies collection" from "Restaurants in Beşiktaş" without a language rule or a model. `.env.example` sets the window to 30 seconds (9 seconds was recorded). A shorter window loses slow typers; a longer one swallows more questions. SAV-14 could ask the decision model "is this a collection name?" instead |
| Q2 | The hint is stored as the user's whole sentence, not as "Movies" | Pulling the name out of the sentence needs a model or a rule for one language (record 0005 rules that out). The pipeline (SAV-14) can normalize it when it indexes the item |
| Q3 | Only the latest share gets the hint | The ticket says "that item". If a user shares three reels and then types one hint, the first two get none |
| Q4 | A hint after a **repeated** share is not recognized | The repeated share creates no new item, and the first share is outside the window. The text becomes a chat message |
| Q5 | `arq` is added to `pyproject.toml` | It is the queue named in the ticket and in SAV-22 §6, but it is a new line in the dependency list (it brings `redis` and `hiredis`) |
| Q6 | The legacy `share` fixture is synthetic | Meta never sent us a `share` attachment (record 0002, finding 7), so none could be recorded. The file follows Meta's documented shape and is named `synthetic_…` |

## Alternatives

- **Queue the raw event and let a worker write the item.** The webhook would be even shorter, but a worker would have to exist in this ticket, `CaptureEvent.user_id` couldn't be filled by the adapter's caller, and a share would be invisible to search until a worker ran.
- **A list of seen message ids in Redis** (`SET NX` with a lifetime). It reads like "skip duplicate message ids", but it has to be written before the work and removed again when the work fails, or a failed webhook is lost on redelivery. The unique keys and job ids need no cleanup and can't disagree with the database.
- **Run the work after answering 200** (a background task). Faster by milliseconds, but a failure after the answer is never redelivered by Meta, so the share is lost.
- **Update the item on a repeated share** (new message id, new time). Then a hint after a repeated share would work (Q4), but the item's save time would move and an old webhook delivered late could move it back.
- **Give the hint to every share in the window.** It fits "three reels, one hint", but also labels an unrelated share that happens to be recent. Left as Q3.
- **Catch the unique-key error instead of locking on first contact.** It works, but the error path only runs when two requests overlap, which is hard to keep tested. The lock has one path.
- **Positional job arguments.** Keyword arguments keep a worker from reading them in the wrong order.

## Consequences

- **New settings:** `META_APP_SECRET`, `META_WEBHOOK_VERIFY_TOKEN` (both secrets) and `COLLECTION_HINT_WINDOW_SECONDS`, in `.env.example`, the README and `docker-compose.yml`. Like every setting they have no default, so the API and `alembic` refuse to start without them.
- **New dependency:** `arq` (Q5).
- **New code:** `app/adapters/`, `app/api/webhooks.py`, `app/ingestion/` and `app/db/session.py`. `app/ingestion/` is new in `docs/ARCHITECTURE.md` §6.
- **The API opens no connection at startup.** Postgres and Redis are first contacted by the first webhook, so `/health` still answers without them.
- **SAV-12 (Send API):** nothing is sent from the webhook. A confirmation or an "already saved" reply belongs to the jobs.
- **SAV-14 (pipeline):**
  - register `index_saved_item` with the arguments above, and move the item on from `pending`;
  - download `capture_event.media_url` at once;
  - read `saved_item.collection` when classifying. The hint arrives seconds after the share, so it is on the row, not in the job's `capture_event`;
  - a collection-saved reel has no caption, post id or link, only the video.
- **SAV-17 (response agent):** register `answer_chat_message`. Storing the conversation (`conversation_msg`, Redis history) is not done here.
- **Job ids are only refused while arq knows them:** while the job is queued or running, and afterwards for as long as the worker keeps its result (one hour by default). A worker that keeps no results could answer a text twice if Meta delivers it again later. Keep the default for chat jobs.
- **Until the workers exist,** queued jobs wait in Redis and expire after a day (arq's default), and items stay `pending`. Sharing the post again queues it again.
- **Tests with a real Redis** are skipped unless `TEST_REDIS_URL` is set, like the database tests (record 0007, P4).
- **Delete my data:** a queued job holds the capture event or the text until a worker runs it, for at most a day. `delete_user_data()` covers Postgres only (record 0007) and doesn't remove queued jobs. A job whose user or item is gone must stop without doing anything (SAV-14, SAV-17).
- **Not handled:** a message the user unsends (`is_deleted`) is ignored and its item stays. Message edits, reactions and read receipts are ignored.
