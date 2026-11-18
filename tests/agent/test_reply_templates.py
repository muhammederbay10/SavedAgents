import pytest

from app.agent.messenger import MAX_QUICK_REPLIES, QuickReply, build_message_body
from app.agent.reply_templates import (
    ChoiceOption,
    ImportCount,
    ResultLine,
    SavedItemSummary,
    error_message,
    import_progress,
    result_reply,
    save_confirmation,
    which_one_question,
    wording_language,
)
from tests.agent.support import RECIPIENT_ID, SAVED_MESSAGE_ID

INCEPTION = SavedItemSummary(
    category="movie",
    name="Inception",
    year=2010,
    details=("Sci-fi",),
    streaming_platforms=("Netflix",),
    emoji="🎬",
)
CAR_REVIEW = SavedItemSummary(category="car review", details=("BMW M3",), emoji="🚗")
KRONOTROP = ResultLine(
    position=1,
    total=3,
    name="Kronotrop",
    area="Beşiktaş",
    open_now=True,
    link="https://maps.example/kronotrop",
    emoji="🍽",
)


# --- language --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "expected"),
    [("tr", "tr"), ("tr-TR", "tr"), ("TR", "tr"), ("en_GB", "en"), ("ja", "en"), (None, "en")],
)
def test_wording_language_uses_the_primary_subtag_and_falls_back_to_english(
    language: str | None, expected: str
) -> None:
    assert wording_language(language) == expected


# --- save confirmation -----------------------------------------------------------------------


def test_save_confirmation_for_a_named_item_in_english() -> None:
    message = save_confirmation(INCEPTION, "en")

    assert message.text == "Saved: Inception (2010) · Sci-fi · on Netflix 🎬"
    assert message.quick_replies == ()
    assert message.reply_to_message_id is None


def test_save_confirmation_for_a_named_item_in_turkish() -> None:
    message = save_confirmation(INCEPTION, "tr")

    assert message.text == "Kaydedildi: Inception (2010) · Sci-fi · Netflix platformunda 🎬"


def test_save_confirmation_without_a_name_says_the_open_category() -> None:
    assert save_confirmation(CAR_REVIEW, "en").text == "Saved as 🚗 car review · BMW M3"
    assert save_confirmation(CAR_REVIEW, "tr").text == "🚗 car review olarak kaydedildi · BMW M3"


def test_save_confirmation_shows_open_values_exactly_as_given() -> None:
    summary = SavedItemSummary(
        category="ラーメン店",
        details=("Kore mutfağı", "Brooklyn, NY"),
        streaming_platforms=("Disney+", "MUBI"),
    )

    message = save_confirmation(summary, "ja")

    assert message.text == "Saved as ラーメン店 · Kore mutfağı · Brooklyn, NY · on Disney+, MUBI"


def test_save_confirmation_with_only_a_name() -> None:
    summary = SavedItemSummary(category="restaurant", name="Kronotrop")

    assert save_confirmation(summary, None).text == "Saved: Kronotrop"


# --- which one? ------------------------------------------------------------------------------


def test_which_one_question_numbers_the_options_and_adds_none_of_these() -> None:
    options = [
        ChoiceOption(label="Kronotrop Bebek", payload="choice:bebek"),
        ChoiceOption(label="Kronotrop Karaköy", payload="choice:karakoy"),
    ]

    message = which_one_question(options, none_payload="choice:none", language="en")

    assert message.text == "Which one did you save?\n1. Kronotrop Bebek\n2. Kronotrop Karaköy"
    assert message.quick_replies == (
        QuickReply(title="1. Kronotrop Bebek", payload="choice:bebek"),
        QuickReply(title="2. Kronotrop Karaköy", payload="choice:karakoy"),
        QuickReply(title="None of these", payload="choice:none"),
    )


def test_which_one_question_in_turkish() -> None:
    options = [ChoiceOption(label="Dune (2021)", payload="tmdb:1")]

    message = which_one_question(options, none_payload="none", language="tr-TR")

    assert message.text == "Hangisini kaydettin?\n1. Dune (2021)"
    assert message.quick_replies[-1] == QuickReply(title="Hiçbiri", payload="none")


@pytest.mark.parametrize("count", [0, MAX_QUICK_REPLIES])
def test_which_one_question_needs_room_for_every_button(count: int) -> None:
    options = [ChoiceOption(label=f"Option {n}", payload=str(n)) for n in range(count)]

    with pytest.raises(ValueError, match="options"):
        which_one_question(options, none_payload="none", language="en")


def test_which_one_question_accepts_as_many_options_as_buttons_allow() -> None:
    options = [ChoiceOption(label=f"Option {n}", payload=str(n)) for n in range(12)]

    message = which_one_question(options, none_payload="none", language="en")

    assert len(message.quick_replies) == MAX_QUICK_REPLIES


# --- import progress -------------------------------------------------------------------------


def test_import_progress_lists_what_was_found_so_far() -> None:
    counts = [
        ImportCount(label="places", count=61),
        ImportCount(label="movies", count=40),
        ImportCount(label="study posts", count=25),
        ImportCount(label="recipes", count=0),
    ]

    message = import_progress(230, 500, counts, "en")

    assert message.text == "Imported 230/500… 61 places, 40 movies, 25 study posts so far"


def test_import_progress_in_turkish_and_without_counts() -> None:
    counts = [ImportCount(label="mekan", count=3)]

    assert import_progress(5, 500, counts, "tr").text == (
        "5/500 içe aktarıldı… şu ana kadar 3 mekan"
    )
    assert import_progress(0, 500, [], "tr").text == "0/500 içe aktarıldı…"
    assert import_progress(0, 500, [], "en").text == "Imported 0/500…"


@pytest.mark.parametrize(("done", "total"), [(-1, 10), (11, 10)])
def test_import_progress_refuses_impossible_numbers(done: int, total: int) -> None:
    with pytest.raises(ValueError, match="done"):
        import_progress(done, total, [], "en")


# --- error -----------------------------------------------------------------------------------


def test_error_message_in_english_and_turkish() -> None:
    assert error_message("en").text.startswith("Sorry, something went wrong")
    assert error_message("tr").text.startswith("Üzgünüm, bir şeyler ters gitti")
    assert error_message("de").text == error_message("en").text


# --- result lines ----------------------------------------------------------------------------


def test_result_reply_quotes_the_saved_reel_with_one_short_line() -> None:
    message = result_reply(KRONOTROP, SAVED_MESSAGE_ID, "en")

    assert message.text == "🍽 1/3 · Kronotrop, Beşiktaş · open now · https://maps.example/kronotrop"
    assert build_message_body(RECIPIENT_ID, message) == {
        "recipient": {"id": RECIPIENT_ID},
        "message": {
            "text": "🍽 1/3 · Kronotrop, Beşiktaş · open now · https://maps.example/kronotrop"
        },
        "reply_to": {"mid": SAVED_MESSAGE_ID},
    }


def test_result_reply_in_turkish_for_a_closed_place() -> None:
    line = ResultLine(position=2, total=3, name="Kronotrop", area="Karaköy", open_now=False)

    message = result_reply(line, SAVED_MESSAGE_ID, "tr")

    assert message.text == "2/3 · Kronotrop, Karaköy · şu an kapalı"


def test_result_reply_without_a_message_id_is_a_plain_message_with_the_post_link() -> None:
    line = ResultLine(
        position=3,
        total=3,
        name="Inception",
        details=("Sci-fi",),
        link="https://www.instagram.com/reel/SYNTHETIC/",
        emoji="🎬",
    )

    message = result_reply(line, None, "en")

    assert message.text == "🎬 3/3 · Inception · Sci-fi · https://www.instagram.com/reel/SYNTHETIC/"
    assert "reply_to" not in build_message_body(RECIPIENT_ID, message)


@pytest.mark.parametrize("position", [0, 4])
def test_result_reply_refuses_a_position_outside_the_list(position: int) -> None:
    line = ResultLine(position=position, total=3, name="Kronotrop")

    with pytest.raises(ValueError, match="position"):
        result_reply(line, None, "en")
