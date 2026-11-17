"""Delete my data: remove one user's rows and lower the vocabulary's item counts.

The foreign keys delete a user's rows when the `user` row goes. The vocabulary
has no reference to a user, so its `item_count` has to be lowered here, before
the user's items are gone.

The user row is locked first. A second request for the same user then waits for
the first one to finish and finds no user, so the counts are lowered only once.
"""

import uuid

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    CATEGORY_VOCABULARY_FIELD,
    SUBTYPE_VOCABULARY_FIELD,
    TAG_VOCABULARY_FIELD,
    User,
)

# One row per (item, field, value) the user's items use: the category, the
# subtype, each tag, and each open value in `fields` (a string, or the strings
# in a list). Wording that never entered the vocabulary matches no row there,
# so it changes nothing. A count never goes below zero.
_LOWER_ITEM_COUNTS = text(
    """
    WITH item_values AS (
        SELECT id AS item_id, CAST(:category_field AS text) AS field, category AS value
        FROM saved_item
        WHERE user_id = :user_id AND category IS NOT NULL
        UNION
        SELECT id, CAST(:subtype_field AS text), subtype
        FROM saved_item
        WHERE user_id = :user_id AND subtype IS NOT NULL
        UNION
        SELECT id, CAST(:tag_field AS text), unnest(tags)
        FROM saved_item
        WHERE user_id = :user_id
        UNION
        SELECT saved_item.id, open_field.key, open_value.value
        FROM saved_item
        CROSS JOIN LATERAL jsonb_each(saved_item.fields) AS open_field
        CROSS JOIN LATERAL (
            SELECT open_field.value #>> '{}' AS value
            WHERE jsonb_typeof(open_field.value) = 'string'
            UNION ALL
            SELECT jsonb_array_elements_text(open_field.value)
            WHERE jsonb_typeof(open_field.value) = 'array'
        ) AS open_value
        WHERE saved_item.user_id = :user_id
    ),
    uses AS (
        SELECT field, value, count(*) AS item_count
        FROM item_values
        GROUP BY field, value
    )
    UPDATE vocabulary
    SET item_count = greatest(vocabulary.item_count - uses.item_count, 0)
    FROM uses
    WHERE vocabulary.field = uses.field AND vocabulary.value = uses.value
    """
)


async def delete_user_data(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Delete a user and every database row that belongs to them.

    The user's saved items go with their raw wording, entity links and
    enrichment runs, as do platform accounts, entity statuses and conversation
    messages. Each vocabulary value loses one from its `item_count` for every
    deleted item that used it. The shared tables keep their rows: entities,
    streaming availability, vocabulary values and aliases.

    The caller commits the session. The user row stays locked until then, so a
    second request for the same user waits and then changes nothing. Nothing
    happens for an unknown user id either. Files and session state outside
    Postgres are not touched.

    Args:
        session: The database session to run the statements on.
        user_id: The id of the user to delete.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: If the database rejects a statement.
    """
    # Without this lock, a second request that starts before the first one
    # commits still sees the user's items and lowers the counts a second time.
    locked_user = await session.execute(select(User.id).where(User.id == user_id).with_for_update())
    if locked_user.scalar_one_or_none() is None:
        return
    await session.execute(
        _LOWER_ITEM_COUNTS,
        {
            "user_id": user_id,
            "category_field": CATEGORY_VOCABULARY_FIELD,
            "subtype_field": SUBTYPE_VOCABULARY_FIELD,
            "tag_field": TAG_VOCABULARY_FIELD,
        },
    )
    await session.execute(delete(User).where(User.id == user_id))
