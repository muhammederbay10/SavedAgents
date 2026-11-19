"""Where the pipeline keeps a saved item's media: object storage, a local folder in development.

Media links in webhooks expire, so the pipeline downloads the file at once and
keeps its own copies: a smaller video, a still picture, or a smaller image
(`docs/ARCHITECTURE.md` §3.3 and §9). Deep look re-watches them later.

Files are kept per user and item, `<user id>/<saved item id>/<name>`, so "delete
my data" removes one user's folder. `LocalMediaStore.erase_user` has the shape
of `app.db.deletion.UserDataOutsidePostgres`, so it can run while the user's
row is locked, like ingestion's eraser.
"""

import asyncio
import shutil
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol


class MediaNotFoundError(LookupError):
    """No stored file has the requested name."""


class MediaStore(Protocol):
    """Keeps the media files of saved items."""

    async def put(
        self, user_id: uuid.UUID, saved_item_id: uuid.UUID, name: str, data: bytes
    ) -> str:
        """Store one file of an item, replacing a file of the same name.

        Args:
            user_id: The item's owner.
            saved_item_id: The item.
            name: The file's name within the item, e.g. `video.mp4`.
            data: The file's bytes.

        Returns:
            The file's key, to read it back with `get`.
        """
        ...

    async def get(self, key: str) -> bytes:
        """Read a stored file.

        Args:
            key: A key that `put` returned.

        Returns:
            The file's bytes.

        Raises:
            MediaNotFoundError: If no file has that key.
        """
        ...

    async def erase_item(self, user_id: uuid.UUID, saved_item_id: uuid.UUID) -> None:
        """Delete every file of one item. Nothing happens if it has none.

        Args:
            user_id: The item's owner.
            saved_item_id: The item.
        """
        ...

    async def erase_user(self, user_id: uuid.UUID, saved_item_ids: Sequence[uuid.UUID]) -> None:
        """Delete every file of one user. Nothing happens if they have none.

        Args:
            user_id: The user.
            saved_item_ids: The user's items. Every file is under the user's own
                folder, so they are not needed; the argument matches
                `UserDataOutsidePostgres`.
        """
        ...


class LocalMediaStore(MediaStore):
    """A `MediaStore` in a folder on this machine, for development."""

    def __init__(self, root: Path) -> None:
        """Create the store.

        Args:
            root: The folder that holds every user's files. It is created on
                the first write.
        """
        self._root = root.resolve()

    async def put(
        self, user_id: uuid.UUID, saved_item_id: uuid.UUID, name: str, data: bytes
    ) -> str:
        """Store one file of an item, replacing a file of the same name.

        Args:
            user_id: The item's owner.
            saved_item_id: The item.
            name: The file's name within the item. It must be a plain file name.
            data: The file's bytes.

        Returns:
            The key `<user id>/<saved item id>/<name>`.

        Raises:
            ValueError: If `name` is not a plain file name.
            OSError: If the file cannot be written.
        """
        if Path(name).name != name or name in {"", ".", ".."}:
            raise ValueError("a stored media name must be a plain file name")
        key = f"{user_id}/{saved_item_id}/{name}"
        path = self._path(key)
        await asyncio.to_thread(_write_file, path, data)
        return key

    async def get(self, key: str) -> bytes:
        """Read a stored file.

        Args:
            key: A key that `put` returned.

        Returns:
            The file's bytes.

        Raises:
            MediaNotFoundError: If no file has that key.
            ValueError: If the key points outside the store.
        """
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as error:
            raise MediaNotFoundError(f"no stored media under key {key!r}") from error

    async def erase_item(self, user_id: uuid.UUID, saved_item_id: uuid.UUID) -> None:
        """Delete every file of one item.

        Args:
            user_id: The item's owner.
            saved_item_id: The item.

        Raises:
            OSError: If a file cannot be deleted.
        """
        await asyncio.to_thread(_remove_folder, self._root / str(user_id) / str(saved_item_id))

    async def erase_user(self, user_id: uuid.UUID, saved_item_ids: Sequence[uuid.UUID]) -> None:
        """Delete every file of one user.

        Args:
            user_id: The user.
            saved_item_ids: Not needed: every file is under the user's folder.

        Raises:
            OSError: If a file cannot be deleted.
        """
        await asyncio.to_thread(_remove_folder, self._root / str(user_id))

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root):
            raise ValueError("a media key must stay inside the store")
        return path


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _remove_folder(folder: Path) -> None:
    if folder.exists():
        shutil.rmtree(folder)
