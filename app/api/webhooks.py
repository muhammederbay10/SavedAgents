"""Webhook endpoints: Meta's verification handshake and the Instagram `messages` webhook.

`POST /webhooks/instagram` does only quick work: it checks the signature, stores
the share or routes the text, queues a job and answers. Meta delivers a webhook
again when the answer is slow or not a 200, so the heavy work runs in the jobs.

| Answer | When |
|---|---|
| 200 | The webhook was handled, also when it held nothing for us |
| 400 | The signature is valid but the body is not a webhook object |
| 401 | The signature is missing or wrong. Nothing is read from the body |
| 500 | The database or the queue failed. Meta delivers the webhook again |
"""

import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse

from app.adapters import instagram
from app.config.base import Settings
from app.ingestion.service import IngestionService

logger = logging.getLogger(__name__)

_SUBSCRIBE_MODE = "subscribe"


def create_instagram_webhook_router(settings: Settings, ingestion: IngestionService) -> APIRouter:
    """Build the routes under `/webhooks/instagram`.

    Args:
        settings: Holds the Meta app secret and the webhook verify token.
        ingestion: Stores captures and queues jobs.

    Returns:
        A router with the `GET` handshake and the `POST` webhook.
    """
    router = APIRouter(prefix="/webhooks/instagram")
    verify_token = settings.meta_webhook_verify_token.get_secret_value()
    app_secret = settings.meta_app_secret.get_secret_value()

    @router.get("", response_class=PlainTextResponse)
    async def verify_subscription(
        mode: Annotated[str | None, Query(alias="hub.mode")] = None,
        token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
        challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
    ) -> str:
        """Answer Meta's verification handshake by returning its challenge.

        Args:
            mode: `hub.mode`, which Meta sets to `subscribe`.
            token: `hub.verify_token`, which must equal our verify token.
            challenge: `hub.challenge`, the text to send back.

        Returns:
            The challenge, as plain text.

        Raises:
            HTTPException: 403 if the mode, the token or the challenge is
                missing or wrong.
        """
        token_matches = token is not None and hmac.compare_digest(
            token.encode(), verify_token.encode()
        )
        if mode != _SUBSCRIBE_MODE or not token_matches or challenge is None:
            logger.warning("instagram_webhook_verification_rejected")
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Verification failed")
        logger.info("instagram_webhook_verified")
        return challenge

    @router.post("")
    async def receive_webhook(
        request: Request,
        signature: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
    ) -> Response:
        """Check a `messages` webhook, ingest its messages and acknowledge it.

        Args:
            request: The request. Its body is read as raw bytes for the signature check.
            signature: The `X-Hub-Signature-256` header.

        Returns:
            An empty 200 response.

        Raises:
            HTTPException: 401 if the signature is missing or wrong, 400 if
                the body is not a webhook object.
        """
        raw_body = await request.body()
        if not instagram.is_valid_signature(raw_body, signature, app_secret):
            logger.warning("instagram_webhook_signature_rejected")
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid signature")
        try:
            messages = instagram.parse_webhook(raw_body)
        except instagram.InvalidWebhookPayloadError as error:
            logger.warning("instagram_webhook_payload_rejected", extra={"reason": str(error)})
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid payload") from error
        for message in messages:
            await _ingest(ingestion, message)
        return Response(status_code=status.HTTP_200_OK)

    return router


async def _ingest(ingestion: IngestionService, message: instagram.InstagramMessage) -> None:
    user_id = await ingestion.resolve_user(instagram.PLATFORM, message.sender_id)
    if isinstance(message, instagram.InstagramShare):
        await ingestion.ingest_capture(instagram.to_capture_event(message, user_id))
    else:
        await ingestion.ingest_chat_message(instagram.to_chat_message(message, user_id))
