# 0013: No official API gives us the video of a reel shared by DM

- Status: spike result recorded. It confirms the caption-only path Muhammed approved for DM-shared reels in SAV-14 (record 0012, point 9). Nothing changes in code.
- Date: 2026-11-18
- Ticket: SAV-28 (0012 is taken by SAV-14)

## Context

A reel shared to the bot by DM arrives as `ig_reel` with the caption (`title`), the public page URL and `reel_video_id`, but no video file (record 0002, consequence 1). Fetching the page would be scraping (`AGENTS.md` §6). Record 0003 had already tried two official routes. A message lookup by the webhook's `mid` returned HTTP 500, and the conversation listing marked shares `is_unsupported` with no attachments. It left untested a media lookup by `reel_video_id`, other fields, other API versions, and the `graph.instagram.com` host.

This spike first read Meta's current documentation. Then, with Muhammed's approval of each call and of the token handling, it made one batch of read-only `GET` calls with the bot's existing token. The batch had a cap of 40 calls including retries, and 35 were used.

## What Meta's documentation says (read 18 Nov 2026)

| Topic | Documented | Page on `developers.facebook.com` |
|---|---|---|
| Webhook shares | "Only the URL for the shared media or post is included in the notification." `ig_reel` lists `url`, `title`, `reel_video_id`. `ig_post` lists the shared post's id. | `/docs/messenger-platform/reference/webhook-events/messages` |
| Message node | `GET /{message-id}`, with `attachments{image_data,video_data,file_url}`, `shares{name,description,type,url,id}` ("you need to request the sub-fields also"), and `is_unsupported`. Only "the 20 most recent messages" of a conversation can be read. It needs a Page-linked professional account, a Page token, and `instagram_basic`, `instagram_manage_messages`, `pages_manage_metadata` and `pages_messaging`. | `/docs/graph-api/reference/message` |
| IG Media node | `GET /{ig-media-id}` returns "only data for media owned by Instagram professional accounts". `owner` is returned only when the app user created the media. `media_url` is left out when the owner turned off downloads or the audio is copyrighted. Reading someone else's media by id is not documented. | `/docs/instagram-platform/reference/instagram-media` |
| Business Discovery | Another Business or Creator account's media, `media_url` included (not guaranteed). It takes that account's **username**, not a media id. Facebook Login only, and it needs `instagram_manage_insights` and `pages_read_engagement`. | `/docs/instagram-platform/instagram-graph-api/reference/ig-user/business_discovery` |
| oEmbed | Embed `html` only, with no video URL. It is "**only** meant to be used for embedding". Deriving information "for analytics purposes" is "strictly prohibited". | `/docs/instagram-platform/oembed` |
| Instagram Login (`graph.instagram.com`) | Needs its own Instagram User token from Business Login for Instagram. "Only the image or video URL for a share will be included in the data returned in a call to the API." It documents fewer message fields than the Message node and has no Business Discovery. | `/docs/instagram-platform/instagram-api-with-instagram-login/conversations-api` |
| App Review | `instagram_manage_messages` and `instagram_business_manage_messages` need App Review and Advanced Access (with Business Verification) for people without a role on the app. | `/docs/permissions`, `/docs/instagram-platform/app-review` |
| Versions | v27.0 (4 Nov 2026) is the latest. v26.0, v25.0 and v24.0 are still available. | `/docs/graph-api/changelog` |

## Test

Development mode, the bot `@savedagent2026`, and Muhammed's tester account. A signature-checking recorder ran behind ngrok on the owner's Mac while Muhammed shared:
- **S1:** a public reel by a third-party creator;
- **S3:** a public third-party photo post (control: a share id the docs document);
- **S4:** a reel published by the bot account itself (control: media the token's account owns).

S2, a private account's reel, was skipped because none was available. Three earlier shares were delivered late and were used as extras within the cap: the same S4 reel again (X1), another public photo (X2) and another public reel (X3). All six webhooks had valid signatures. S4's and X1's `ig_reel` had no `title`, because that reel has no caption.

Every call was a `GET` to `graph.facebook.com` with the bot's token from the "API setup with Unified login" page, in the `Authorization: Bearer` header. The only exception was B0. Raw bodies and responses were kept outside the repo and deleted once this record was written. Only request shapes, statuses, error codes and field names are recorded here.

| Step | Request | Samples | Status | What came back |
|---|---|---|---|---|
| A0 | `/me?fields=id,username` (v27.0) | — | 400, code 12 | "username field is deprecated": `/me` is a Facebook user, not the Instagram account |
| A0 | `/me/permissions` | — | 200 | Granted: `instagram_basic`, `instagram_manage_messages`, `instagram_manage_comments`, `instagram_content_publish`, `ads_management`, `ads_read`, `public_profile`. **Not granted:** `pages_manage_metadata`, `pages_messaging`, `pages_read_engagement`, `instagram_manage_insights` |
| A1 | `/{bot-ig-id}/conversations?platform=instagram&fields=id,updated_time` | — | 200 | `id`, `updated_time` |
| A2 | `/{conversation-id}?fields=messages.limit(20){id,created_time,from,to,message,is_unsupported,shares{name,description,type,url,id},attachments{id,name,file_url,image_data,video_data}}` | the tester's chat | 200 | `id`, `created_time`, `from{id,username}`, `to{data{id,username}}`, `message`, `is_unsupported`. **Every share in the chat was `is_unsupported: true`, with no `shares` and no `attachments`**, even with the sub-fields requested. Texts came back normally. |
| A3 | `/{webhook mid}?fields=<A2's message fields>` (v27.0) | S1, S3, S4, X1, X3 | 500, code 1 | "An unknown error has occurred", every time, also on one retry each |
| A4 | `/{webhook mid}/shares` and `/{webhook mid}/attachments` | S1, S3, S4 | 500, code 1 | the same |
| A5 | A3 on v25.0 and v24.0 | S1, S3, S4 | 500, code 1 | the same on both versions |
| A6 | `/{reel_video_id}?fields=id,media_type,media_product_type,media_url,thumbnail_url,permalink,shortcode,caption,username,timestamp` (v27.0, v24.0) | **S4 (own reel)** | **200** | `id`, `media_type` (VIDEO), `media_product_type` (REELS), `media_url` (an MP4 on Instagram's CDN), `thumbnail_url`, `permalink`, `shortcode`, `timestamp`, `username`. No `caption`, because the reel has none. |
| A6 | the same | **S1, X3 (third-party reels)** | **400, code 100, subcode 33** | "Object … does not exist, cannot be loaded due to missing permissions, or does not support this operation", on both versions |
| A7 | `/{ig_post_media_id}?fields=<A6's fields>` (v27.0) | S3, X2 (third-party photos) | 400, code 100, subcode 33 | the same |
| A8 | Business Discovery | S1, S4 | not run | Its approval required `instagram_manage_insights` and `pages_read_engagement`, and neither is granted |
| B0 | `GET graph.instagram.com/v27.0/me?fields=user_id,username`, existing token | — | 401, code 190 | "Invalid OAuth access token - Cannot parse access token" |

## Findings

1. **`reel_video_id` is the reel's IG Media id.** For the bot's own reel (S4), `GET /{reel_video_id}` returned a playable MP4 `media_url`, the permalink and the shortcode, on v27.0 and v24.0. The id itself is not the obstacle.
2. **Third-party reels and posts are not readable with this token.** The same request, token, fields and versions failed with code 100/33 for both public third-party reels and both public third-party photos. Only ownership differed from S4. This matches the documentation, which describes `GET /{ig-media-id}` only for media the account owns and offers no lookup of someone else's media by id. Error 100/33 by itself does not say which cause applies. Two possibilities remain untested:
   - whether App Review or Advanced Access would change the answer (nothing in the docs suggests it would);
   - whether S1's creator has a professional account (it was a public creator, but we didn't check the account type).
3. **The message routes don't add anything.** In the conversation, every share is `is_unsupported` with no `shares` and no `attachments`, even with the sub-fields the docs ask for. This is the same Message node a direct lookup would return.
4. **Direct lookup by the webhook's `mid` stays inconclusive.** It failed with HTTP 500 on every version, within minutes of the send, so the 20-message limit doesn't explain it. Two other explanations remain:
   - the webhook `mid` is **not the Graph message id**: the Conversations API ids have a different encoding (they decode to a `new_ig…` prefix, the webhook's to `ig_…`), and none of them matched;
   - the token lacks `pages_manage_metadata` and `pages_messaging`, which the Message node's documentation lists.

   A lookup by the Graph message id from A2 was not in the approved batch. Point 3 suggests it would return the same `is_unsupported` message.
5. **Business Discovery doesn't fit.** It needs the creator's username. Neither the webhook nor any API response above gives it: `from` is the sender, and the URL holds only the shortcode. It also needs scopes the token lacks, works only for Business and Creator accounts, and may leave out `media_url`.
6. **`graph.instagram.com` doesn't accept this token** (401, code 190). Using that host would mean a new Instagram-Login token, which wasn't approved. The documentation there promises less than the Message node: only "the image or video URL for a share".
7. **oEmbed and the reel's page are ruled out.** oEmbed's terms forbid analysis, and it has no video URL. Fetching the page would be scraping.

## Decision

**No: no official route gives us the video (or the caption) of a third-party reel shared by DM**, with this app, this token and its granted permissions, on the routes and versions we tried. DM-shared reels stay **caption-only** (record 0012, point 9), until a capture path that has the file provides it:
- the reel saved into a collaborative collection with the bot (record 0003), which does come with an MP4 link;
- the mobile share extension (Phase 3).

No follow-up ticket is proposed for the adapter or the pipeline.

## Alternatives considered

- **Asking for more permissions** (`pages_read_engagement`, `instagram_manage_insights`, Advanced Access) to retry A6 or run Business Discovery. The documentation gives no route for someone else's media by id, and Business Discovery still needs a username we don't get. It isn't worth an App Review request.
- **An Instagram-Login token on `graph.instagram.com`.** It needs a new login and token, and its documentation promises less than the route tested here.
- **oEmbed or the reel's page.** Ruled out by Meta's oEmbed terms and by `AGENTS.md` §6.

## Consequences

- **SAV-14 is unchanged.** DM reels are indexed from the caption only. Reels with no caption (S4 had none) arrive with no `title`, so for them the bot only has the page link. The "ask when unsure" path applies.
- **SAV-11, for later:** the webhook `mid` is the right id for `reply_to` (record 0004), but it doesn't look like the id the Conversations API uses. Any future message lookup has to map between the two first.
- **Open, not planned:**
  - a lookup by the Graph message id;
  - a private-account reel (S2);
  - the effect of App Review or Advanced Access.

  None of them is expected to give us a file. Each would need a new proposal and Muhammed's approval.
