"""An answer is sent as an intro, then one reply per result quoting the saved reel (record 0004)."""

import json
import uuid
from datetime import timedelta

import httpx
import pytest

from app.agent.replies.answers import AnswerMessages, AnswerSender, ResultCard
from app.agent.replies.messenger import (
    MessengerDeliveryUnknownError,
    MessengerRequestError,
)
from app.agent.replies.templates import reply_wording
from tests.agent.support import (
    NOW,
    RECIPIENT_ID,
    build_messenger,
    json_response,
    sent,
)

ENGLISH = reply_wording("en")
INTRO = "2 places in Beşiktaş from your Food collection"
MAPS_LINK = "https://maps.example.test/place/synthetic-1"
POST_URL = "https://www.instagram.com/reel/FAKEREEL101/"


def refused() -> httpx.Response:
    return json_response("synthetic_error_invalid_parameter.json", 400)


def card(
    name: str,
    *,
    message_id: str | None = None,
    post_url: str | None = None,
    link: str | None = None,
    area: str | None = None,
) -> ResultCard:
    return ResultCard(
        item_id=uuid.uuid4(),
        names=(name,),
        label="restaurant",
        area=area,
        link=link,
        post_url=post_url,
        source_message_id=message_id,
    )


def texts(bodies: list[object]) -> list[str]:
    return [body["message"]["text"] for body in bodies]  # type: ignore[index]


def replied_to(bodies: list[object]) -> list[str | None]:
    return [body.get("reply_to", {}).get("mid") for body in bodies]  # type: ignore[attr-defined]


async def test_intro_comes_first_then_one_reply_per_result_quoting_each_saved_reel() -> None:
    under_test = build_messenger([sent(), sent(), sent()])
    answer = AnswerMessages(
        intro=INTRO,
        results=(
            card("Meze Evi", message_id="mid-1", link=MAPS_LINK, area="Beşiktaş"),
            card("Kronotrop", message_id="mid-2", area="Bebek"),
        ),
        wording=ENGLISH,
    )

    report = await AnswerSender(under_test.messenger).send(RECIPIENT_ID, answer, NOW)

    bodies = under_test.api.bodies()
    assert texts(bodies) == [
        INTRO,
        f"1/2 · Meze Evi, Beşiktaş · {MAPS_LINK}",
        "2/2 · Kronotrop, Bebek",
    ]
    assert replied_to(bodies) == [None, "mid-1", "mid-2"]
    assert (report.sent, report.fallbacks, report.outside_reply_window) == (3, 0, False)


async def test_a_refused_quote_falls_back_to_the_post_link_as_a_preview_card() -> None:
    under_test = build_messenger([sent(), refused(), sent()])
    answer = AnswerMessages(
        intro=INTRO,
        results=(card("Meze Evi", message_id="old-mid", post_url=POST_URL, link=MAPS_LINK),),
        wording=ENGLISH,
    )

    report = await AnswerSender(under_test.messenger).send(RECIPIENT_ID, answer, NOW)

    bodies = under_test.api.bodies()
    assert replied_to(bodies) == [None, "old-mid", None]
    assert texts(bodies)[2] == f"1/1 · Meze Evi · {POST_URL}"
    assert report.fallbacks == 1


async def test_without_a_post_link_the_last_fallback_is_the_name_with_the_maps_link() -> None:
    under_test = build_messenger([sent(), refused(), sent()])
    answer = AnswerMessages(
        intro=INTRO,
        results=(card("Meze Evi", message_id="deleted-mid", link=MAPS_LINK),),
        wording=ENGLISH,
    )

    await AnswerSender(under_test.messenger).send(RECIPIENT_ID, answer, NOW)

    bodies = under_test.api.bodies()
    assert replied_to(bodies) == [None, "deleted-mid", None]
    assert texts(bodies)[2] == f"1/1 · Meze Evi · {MAPS_LINK}"


async def test_an_item_without_a_message_id_starts_with_its_post_link() -> None:
    under_test = build_messenger([sent(), sent()])
    answer = AnswerMessages(
        intro=INTRO, results=(card("Meze Evi", post_url=POST_URL),), wording=ENGLISH
    )

    await AnswerSender(under_test.messenger).send(RECIPIENT_ID, answer, NOW)

    assert replied_to(under_test.api.bodies()) == [None, None]
    assert texts(under_test.api.bodies())[1] == f"1/1 · Meze Evi · {POST_URL}"


async def test_when_every_step_is_refused_the_error_is_raised() -> None:
    under_test = build_messenger([sent(), refused(), refused()])
    answer = AnswerMessages(
        intro=INTRO,
        results=(card("Meze Evi", message_id="old-mid", post_url=POST_URL),),
        wording=ENGLISH,
    )

    with pytest.raises(MessengerRequestError):
        await AnswerSender(under_test.messenger).send(RECIPIENT_ID, answer, NOW)


async def test_a_possibly_delivered_reply_is_not_sent_again_as_a_fallback() -> None:
    under_test = build_messenger([sent(), httpx.Response(500)])
    answer = AnswerMessages(
        intro=INTRO,
        results=(card("Meze Evi", message_id="mid-1", post_url=POST_URL),),
        wording=ENGLISH,
    )

    with pytest.raises(MessengerDeliveryUnknownError):
        await AnswerSender(under_test.messenger).send(RECIPIENT_ID, answer, NOW)

    assert len(under_test.api.requests) == 2


async def test_nothing_more_is_sent_once_the_reply_window_has_closed() -> None:
    closed = json_response("synthetic_error_outside_window.json", 400)
    under_test = build_messenger([sent(), closed])
    answer = AnswerMessages(
        intro=INTRO,
        results=(card("Meze Evi", message_id="mid-1"), card("Kronotrop", message_id="mid-2")),
        wording=ENGLISH,
    )

    report = await AnswerSender(under_test.messenger).send(RECIPIENT_ID, answer, NOW)

    assert len(under_test.api.requests) == 2
    assert (report.sent, report.outside_reply_window) == (1, True)


async def test_an_answer_older_than_the_window_sends_nothing() -> None:
    under_test = build_messenger([])
    answer = AnswerMessages(intro=INTRO, results=(), wording=None)

    report = await AnswerSender(under_test.messenger).send(
        RECIPIENT_ID, answer, NOW - timedelta(hours=25)
    )

    assert under_test.api.requests == []
    assert report.outside_reply_window


async def test_an_answer_with_no_results_is_only_the_intro() -> None:
    under_test = build_messenger([sent()])
    answer = AnswerMessages(intro="Nothing saved in Beşiktaş yet.", results=(), wording=None)

    report = await AnswerSender(under_test.messenger).send(RECIPIENT_ID, answer, NOW)

    assert texts(under_test.api.bodies()) == ["Nothing saved in Beşiktaş yet."]
    assert report.sent == 1


async def test_a_list_post_shows_all_its_names_and_an_unnamed_item_its_label() -> None:
    under_test = build_messenger([sent(), sent(), sent()])
    list_post = ResultCard(
        item_id=uuid.uuid4(),
        names=("Kronotrop", "Meze Evi"),
        label="café list",
        area=None,
        link=None,
        post_url=POST_URL,
        source_message_id=None,
    )
    unnamed = ResultCard(
        item_id=uuid.uuid4(),
        names=(),
        label="car review",
        area=None,
        link=None,
        post_url=None,
        source_message_id="mid-3",
    )
    answer = AnswerMessages(intro=INTRO, results=(list_post, unnamed), wording=ENGLISH)

    await AnswerSender(under_test.messenger).send(RECIPIENT_ID, answer, NOW)

    assert texts(under_test.api.bodies())[1:] == [
        f"1/2 · Kronotrop / Meze Evi · {POST_URL}",
        "2/2 · car review",
    ]


async def test_results_need_wording_in_the_users_language() -> None:
    under_test = build_messenger([])
    answer = AnswerMessages(
        intro=INTRO, results=(card("Meze Evi", post_url=POST_URL),), wording=None
    )

    with pytest.raises(ValueError, match="wording"):
        await AnswerSender(under_test.messenger).send(RECIPIENT_ID, answer, NOW)

    assert json.dumps(under_test.api.bodies()) == "[]"
