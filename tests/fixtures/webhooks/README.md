# Instagram webhook fixtures

Real `messages` webhooks recorded on 16 Nov 2026 for SAV-5 (DM shares) and SAV-6 (saves into a
collaborative collection), then anonymized. They are the reference input for the Instagram adapter
(SAV-11). What they show is written up in
[`docs/decisions/0002-instagram-webhook-payloads.md`](../../../docs/decisions/0002-instagram-webhook-payloads.md)
and [`docs/decisions/0003-collaborative-collections.md`](../../../docs/decisions/0003-collaborative-collections.md).

| File | What was sent to the bot |
|---|---|
| `instagram_reel_share.json` | A reel, with a long Turkish caption |
| `instagram_post_share_photo.json` | A photo post with a short caption |
| `instagram_post_share_carousel.json` | A carousel post. It had a caption, but the webhook delivered one image and no `title` |
| `instagram_text_short.json` | The text "Test" |
| `instagram_text_question.json` | The text "Restaurants in beşiktaş" (lowercase "b", as typed) |
| `instagram_reel_with_hint_1_reel.json` | A reel ... |
| `instagram_reel_with_hint_2_text.json` | ... followed 9 seconds later by "For my movies collection" |
| `instagram_collection_save_reel_1.json` | SAV-6: a reel saved into a collaborative collection shared with the bot. Arrives as `unsupported_type` with only a link to the MP4 file |
| `instagram_collection_save_reel_2.json` | SAV-6: a second reel saved into that collection, same shape |
| `instagram_collection_save_photo.json` | SAV-6: a photo post saved into that collection. Arrives as a normal `ig_post` with its caption |
| `instagram_collection_save_reel_with_caption.json` | SAV-6: a reel that has a caption, saved into that collection. Still only the MP4 link; the caption is not sent |
| `meta_dashboard_test_messages.json` | The sample Meta sends from the dashboard's **Test** button. It has a different shape (`changes`, not `messaging`) and is not what real DMs look like |

## How they were anonymized

Only the values were changed; every key, nesting level and value type is exactly as Meta sent it.

| Field | Replaced with |
|---|---|
| Bot account id (`entry.id`, `recipient.id`) | `17840000000000001` |
| Sender id (`sender.id`) | `1000000000000001` |
| Message id (`mid`) | `aWdfZAGFAKEMID<n>` padded to the real length (164) |
| `reel_video_id`, `ig_post_media_id` | `1800000000000<n><m>` (17 digits) |
| Reel `url` | `https://www.instagram.com/reel/FAKEREEL<n>/` |
| Post and `unsupported_type` `url` | `lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=<fake id>&signature=FAKE_SIGNATURE_<n>` |
| Timestamps | Shifted to start at 2026-01-15 12:00 UTC; the real gaps between events are kept |
| Captions (`title`) | Synthetic captions with the same shape: length, language, line breaks, hashtags and emoji |

The texts the tester typed ("Test", "Restaurants in beşiktaş", "For my movies collection") are kept
as sent. `tests/test_webhook_fixtures.py` fails if a fixture contains an id, URL or signature that
doesn't follow these fake patterns.

To record new payloads, follow the steps in the decision record and anonymize the same way. Never
commit a raw recording.
