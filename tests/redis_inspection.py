"""Reads everything a Redis server holds, so tests can look for personal data in it."""

from redis.asyncio import Redis


async def all_stored_bytes(redis_url: str) -> bytes:
    """Return every key and every value in the server's database, joined together.

    Strings, hashes, sets, sorted sets and lists are read. Values that arq
    pickled keep their texts as UTF-8 bytes, so a plain search finds them.
    """
    client: Redis = Redis.from_url(redis_url)
    chunks: list[bytes] = []
    try:
        async for key in client.scan_iter():
            chunks.append(key)
            chunks.extend(await _values_of(client, key))
    finally:
        await client.aclose()
    return b"\n".join(chunks)


async def _values_of(client: Redis, key: bytes) -> list[bytes]:
    key_type = (await client.type(key)).decode()
    if key_type == "string":
        value = await client.get(key)
        return [] if value is None else [value]
    if key_type == "hash":
        pairs = await client.hgetall(key)
        return [part for pair in pairs.items() for part in pair]
    if key_type == "set":
        return list(await client.smembers(key))
    if key_type == "zset":
        return list(await client.zrange(key, 0, -1))
    if key_type == "list":
        return list(await client.lrange(key, 0, -1))
    return []
