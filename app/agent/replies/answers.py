"""Sends an answer that lists saved items: a short intro, then one reply per result.

ARCHITECTURE §3.5 and decision record 0004. Each result is its own message
that replies to the message the item was saved with (`reply_to.mid` = the
item's `source_message_id`), so Instagram shows the saved reel above the line.

When that is not possible, each result falls back, in this order:

1. a reply quoting the saved message, with the item's Maps or TMDB link;
2. the post link, which Instagram shows as a preview card;
3. the name with the Maps or TMDB link.

A step is skipped when the item lacks what it needs (no message id, no post
link). A step that Meta refuses (`MessengerRequestError`, e.g. an old or
deleted message id) moves on to the next one. Whether Instagram actually
showed the quote cannot be seen in Meta's answer, so a reply that was accepted
counts as delivered.

When the reply window has closed, nothing more is sent. Any other messenger
error (throttled, unreachable, delivery unknown) is raised; it is never retried
here as another step, so a message that may have been delivered is not sent
twice.
"""

import logging
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Final, Protocol

from app.agent.replies.messenger import (
    MessengerRequestError,
    OutgoingMessage,
    QuickReply,
    SendResult,
    SendStatus,
)
from app.agent.replies.templates import ReplyWording, ResultLine, result_reply

logger = logging.getLogger(__name__)

# Joins the names of a list post's places or titles in one line. Like the
# templates' separator, it is the bot's own layout, used in every language.
NAME_SEPARATOR: Final = " / "


class Messenger(Protocol):
    """Sends one DM; `InstagramMessenger` in production."""

    async def send(
        self, recipient_id: str, message: OutgoingMessage, last_user_message_at: datetime
    ) -> SendResult:
        """Send one message, unless the reply window has closed."""
        ...


@dataclass(frozen=True)
class ResultCard:
    """What a reply needs to show one saved item.

    Attributes:
        item_id: The saved item.
        names: The names of the real things it is about, e.g. ("Kronotrop",);
            several for a list post, none when nothing was resolved.
        label: What to call it without a name, e.g. its open category "car review".
        area: Where it is, for places, e.g. "Beşiktaş".
        link: The Maps or TMDB link of the real thing, when the resolver stored one.
        post_url: The link to the original post, when the item has one.
        source_message_id: The message the item was saved with, when it has one.
    """

    item_id: uuid.UUID
    names: tuple[str, ...] = field(repr=False)
    label: str | None = field(repr=False)
    area: str | None = field(repr=False)
    link: str | None
    post_url: str | None
    source_message_id: str | None = field(repr=False)


@dataclass(frozen=True)
class AnswerMessages:
    """An answer ready to send.

    Attributes:
        intro: The intro, in the user's language, written by the response agent.
        results: The items to show, in order, already capped.
        wording: The bot's own words in the user's language.
        intro_quick_replies: Buttons under the intro, e.g. "Yes, delete".
    """

    intro: str = field(repr=False)
    results: tuple[ResultCard, ...]
    wording: ReplyWording | None
    intro_quick_replies: tuple[QuickReply, ...] = ()


@dataclass(frozen=True)
class DeliveryReport:
    """What happened when an answer was sent.

    Attributes:
        sent: How many messages were sent, the intro included.
        fallbacks: How many results needed a fallback step.
        outside_reply_window: Whether sending stopped because the window had closed.
    """

    sent: int
    fallbacks: int
    outside_reply_window: bool

    @property
    def complete(self) -> bool:
        """Return whether every message of the answer was sent."""
        return not self.outside_reply_window


@dataclass(frozen=True)
class _Step:
    reply_to: str | None
    link: str | None


class _WindowClosedError(Exception):
    """The reply window closed; nothing more is sent."""


class AnswerSender:
    """Sends an answer as an intro plus one reply per result, with the fallbacks of record 0004."""

    def __init__(self, messenger: Messenger) -> None:
        """Create the sender.

        Args:
            messenger: Sends one DM.
        """
        self._messenger = messenger

    async def send(
        self, recipient_id: str, answer: AnswerMessages, last_user_message_at: datetime
    ) -> DeliveryReport:
        """Send the intro, then each result.

        Args:
            recipient_id: The user's Instagram-scoped id.
            answer: What to send.
            last_user_message_at: When the user last wrote, as Meta reported it.

        Returns:
            How many messages were sent, and whether the window closed.

        Raises:
            ValueError: If there are results but no wording to show them with.
            MessengerRequestError: If Meta refused every step of a result, or the intro.
            MessengerError: Any other failure to send; it is not retried here.
        """
        if answer.results and answer.wording is None:
            raise ValueError("results need the bot's wording in the user's language")
        sent = 0
        fallbacks = 0
        try:
            intro = OutgoingMessage(answer.intro, quick_replies=answer.intro_quick_replies)
            await self._send_one(recipient_id, intro, last_user_message_at)
            sent += 1
            for position, card in enumerate(answer.results, start=1):
                line = ResultLine(
                    position=position, total=len(answer.results), name=_name(card), area=card.area
                )
                steps_taken = await self._send_result(
                    recipient_id, card, line, answer.wording, last_user_message_at
                )
                sent += 1
                fallbacks += steps_taken > 1
        except _WindowClosedError:
            return DeliveryReport(sent=sent, fallbacks=fallbacks, outside_reply_window=True)
        return DeliveryReport(sent=sent, fallbacks=fallbacks, outside_reply_window=False)

    async def _send_result(
        self,
        recipient_id: str,
        card: ResultCard,
        line: ResultLine,
        wording: ReplyWording | None,
        last_user_message_at: datetime,
    ) -> int:
        """Send one result through its steps; return how many steps it took."""
        if wording is None:
            raise ValueError("results need the bot's wording in the user's language")
        steps = _steps(card)
        for number, step in enumerate(steps, start=1):
            message = result_reply(replace(line, link=step.link), step.reply_to, wording)
            try:
                await self._send_one(recipient_id, message, last_user_message_at)
            except MessengerRequestError as error:
                if number == len(steps):
                    raise
                logger.warning(
                    "answer_result_fallback",
                    extra={
                        "saved_item_id": str(card.item_id),
                        "step": number,
                        "error_code": error.error_code,
                    },
                )
                continue
            return number
        raise AssertionError("a result always has at least one step")

    async def _send_one(
        self, recipient_id: str, message: OutgoingMessage, last_user_message_at: datetime
    ) -> None:
        result = await self._messenger.send(recipient_id, message, last_user_message_at)
        if result.status is SendStatus.OUTSIDE_REPLY_WINDOW:
            raise _WindowClosedError


def _name(card: ResultCard) -> str | None:
    if card.names:
        return NAME_SEPARATOR.join(card.names)
    return card.label


def _steps(card: ResultCard) -> list[_Step]:
    steps: list[_Step] = []
    if card.source_message_id is not None:
        steps.append(_Step(reply_to=card.source_message_id, link=card.link))
    if card.post_url is not None:
        steps.append(_Step(reply_to=None, link=card.post_url))
    if card.link is not None or not steps:
        steps.append(_Step(reply_to=None, link=card.link))
    return steps
