import logging
from http import HTTPStatus
from typing import Any

import aiohttp
from discord import Member, Message, Thread

from rocketwatch.utils.config import cfg
from rocketwatch.utils.retry import retry

log = logging.getLogger("rocketwatch.sentinel")


class SentinelClient:
    def __init__(self) -> None:
        self._enabled = bool(cfg.sentinel.api_url and cfg.sentinel.api_key)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=cfg.sentinel.api_url,
                headers={"X-Api-Key": cfg.sentinel.api_key},
                timeout=aiohttp.ClientTimeout(total=5),
            )
        return self._session

    @retry(tries=3, delay=2, backoff=2)
    async def _post(
        self, endpoint: str, payload: dict[str, str | int]
    ) -> dict[str, Any] | None:
        session = await self._get_session()
        async with session.post(endpoint, json=payload) as resp:
            if resp.status == HTTPStatus.OK:
                log.info(f"POST {endpoint} -> {resp.status}")
                return dict(await resp.json())
            body = await resp.text()
            log.warning(f"POST {endpoint} -> {resp.status}: {body}")
            return None

    async def _request(self, endpoint: str, payload: dict[str, str | int]) -> bool:
        if not self._enabled:
            return False
        log.info(f"POST {endpoint} {payload}")
        return await self._post(endpoint, payload) is not None

    async def delete_message(self, message: Message, reason: str) -> bool:
        if message.guild is None:
            return False
        return await self._request(
            "/delete_message",
            {
                "guild_id": message.guild.id,
                "channel_id": message.channel.id,
                "message_id": message.id,
                "reason": reason,
            },
        )

    async def lock_thread(self, thread: Thread, reason: str) -> bool:
        if thread.guild is None:
            return False
        return await self._request(
            "/lock_thread",
            {
                "guild_id": thread.guild.id,
                "thread_id": thread.id,
                "reason": reason,
            },
        )

    async def delete_thread(self, thread: Thread, reason: str) -> bool:
        if thread.guild is None:
            return False
        return await self._request(
            "/delete_thread",
            {
                "guild_id": thread.guild.id,
                "thread_id": thread.id,
                "reason": reason,
            },
        )

    async def timeout_member(
        self, member: Member, duration_seconds: int, reason: str
    ) -> bool:
        return await self._request(
            "/timeout_member",
            {
                "guild_id": member.guild.id,
                "user_id": member.id,
                "duration_seconds": duration_seconds,
                "reason": reason,
            },
        )

    async def kick_member(self, member: Member, reason: str) -> bool:
        return await self._request(
            "/kick_member",
            {
                "guild_id": member.guild.id,
                "user_id": member.id,
                "reason": reason,
            },
        )

    async def ban_member(self, member: Member, reason: str) -> bool:
        return await self._request(
            "/ban_member",
            {
                "guild_id": member.guild.id,
                "user_id": member.id,
                "reason": reason,
            },
        )

    async def is_banned(self, guild_id: int, user_id: int) -> bool | None:
        result = await self._post(
            "/is_banned", {"guild_id": guild_id, "user_id": user_id}
        )
        if result and ("banned" in result):
            return bool(result["banned"])
        return None

    async def is_timed_out(self, guild_id: int, user_id: int) -> bool | None:
        result = await self._post(
            "/is_timed_out", {"guild_id": guild_id, "user_id": user_id}
        )
        if result and ("timed_out" in result):
            return bool(result["timed_out"])
        return None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
