import logging
from datetime import UTC, datetime, timedelta

import discord
from aiohttp import web
from discord.ext.commands import Bot

from audit import log_action
from config import KeyConfig, cfg
from guardrails import (
    check_guild,
    check_member_age,
    check_message_age,
    check_moderator,
    check_thread_age,
    check_timeout_duration,
    rate_limiter,
)

log = logging.getLogger("sentinel.server")


def create_app(bot: Bot) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app["bot"] = bot
    app.router.add_post("/delete_message", handle_delete_message)
    app.router.add_post("/lock_thread", handle_lock_thread)
    app.router.add_post("/delete_thread", handle_delete_thread)
    app.router.add_post("/timeout_member", handle_timeout_member)
    app.router.add_post("/kick_member", handle_kick_member)
    app.router.add_post("/ban_member", handle_ban_member)
    return app


@web.middleware
async def auth_middleware(request: web.Request, handler) -> web.StreamResponse:
    api_key = request.headers.get("X-Api-Key")
    for key in cfg.api.keys:
        if key.secret == api_key:
            request["key"] = key
            return await handler(request)
    return web.json_response({"error": "unauthorized"}, status=401)


async def handle_delete_message(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    key: KeyConfig = request["key"]
    data = await request.json()

    guild_id = data["guild_id"]
    channel_id = data["channel_id"]
    message_id = data["message_id"]
    reason = data.get("reason", "")

    if error := check_guild(key, guild_id):
        return web.json_response({"error": error}, status=422)

    if (retry_after := rate_limiter.check(key)) is not None:
        log_action("delete_message", guild_id, message_id, reason, "rate_limited")
        return web.json_response(
            {"error": "rate_limited", "retry_after_seconds": retry_after}, status=429
        )

    guild = bot.get_guild(guild_id)
    if guild is None:
        return web.json_response({"error": "guild_not_found"}, status=404)

    channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except discord.NotFound:
            return web.json_response({"error": "channel_not_found"}, status=404)

    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        log_action("delete_message", guild_id, message_id, reason, "not_found")
        return web.json_response({"error": "message_not_found"}, status=404)

    if age_check := check_message_age(key.delete_message_max_age, message):
        error, age, status = age_check
        log_action("delete_message", guild_id, message_id, reason, error)
        return web.json_response({"error": error, "age_seconds": age}, status=status)

    try:
        await message.delete()
    except discord.Forbidden:
        log_action("delete_message", guild_id, message_id, reason, "forbidden")
        return web.json_response({"error": "missing_permissions"}, status=403)

    log_action("delete_message", guild_id, message_id, reason, "success")
    return web.json_response(
        {"status": "ok", "action": "delete_message", "message_id": message_id}
    )


async def handle_lock_thread(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    key: KeyConfig = request["key"]
    data = await request.json()

    guild_id = data["guild_id"]
    thread_id = data["thread_id"]
    reason = data.get("reason", "")

    if error := check_guild(key, guild_id):
        return web.json_response({"error": error}, status=422)

    if (retry_after := rate_limiter.check(key)) is not None:
        log_action("lock_thread", guild_id, thread_id, reason, "rate_limited")
        return web.json_response(
            {"error": "rate_limited", "retry_after_seconds": retry_after}, status=429
        )

    guild = bot.get_guild(guild_id)
    if guild is None:
        return web.json_response({"error": "guild_not_found"}, status=404)

    thread = guild.get_thread(thread_id)
    if thread is None:
        try:
            thread = await guild.fetch_channel(thread_id)
        except discord.NotFound:
            log_action("lock_thread", guild_id, thread_id, reason, "not_found")
            return web.json_response({"error": "thread_not_found"}, status=404)

    if age_check := check_thread_age(key.lock_thread_max_age, thread):
        error, age, status = age_check
        log_action("lock_thread", guild_id, thread_id, reason, error)
        return web.json_response({"error": error, "age_seconds": age}, status=status)

    try:
        await thread.edit(locked=True, archived=True, reason=reason)
    except discord.Forbidden:
        log_action("lock_thread", guild_id, thread_id, reason, "forbidden")
        return web.json_response({"error": "missing_permissions"}, status=403)

    log_action("lock_thread", guild_id, thread_id, reason, "success")
    return web.json_response(
        {"status": "ok", "action": "lock_thread", "thread_id": thread_id}
    )


async def handle_delete_thread(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    key: KeyConfig = request["key"]
    data = await request.json()

    guild_id = data["guild_id"]
    thread_id = data["thread_id"]
    reason = data.get("reason", "")

    if error := check_guild(key, guild_id):
        return web.json_response({"error": error}, status=422)

    if (retry_after := rate_limiter.check(key)) is not None:
        log_action("delete_thread", guild_id, thread_id, reason, "rate_limited")
        return web.json_response(
            {"error": "rate_limited", "retry_after_seconds": retry_after}, status=429
        )

    guild = bot.get_guild(guild_id)
    if guild is None:
        return web.json_response({"error": "guild_not_found"}, status=404)

    thread = guild.get_thread(thread_id)
    if thread is None:
        try:
            thread = await guild.fetch_channel(thread_id)
        except discord.NotFound:
            log_action("delete_thread", guild_id, thread_id, reason, "not_found")
            return web.json_response({"error": "thread_not_found"}, status=404)

    if age_check := check_thread_age(key.delete_thread_max_age, thread):
        error, age, status = age_check
        log_action("delete_thread", guild_id, thread_id, reason, error)
        return web.json_response({"error": error, "age_seconds": age}, status=status)

    try:
        await thread.delete()
    except discord.Forbidden:
        log_action("delete_thread", guild_id, thread_id, reason, "forbidden")
        return web.json_response({"error": "missing_permissions"}, status=403)

    log_action("delete_thread", guild_id, thread_id, reason, "success")
    return web.json_response(
        {"status": "ok", "action": "delete_thread", "thread_id": thread_id}
    )


async def handle_timeout_member(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    key: KeyConfig = request["key"]
    data = await request.json()

    guild_id = data["guild_id"]
    user_id = data["user_id"]
    duration_seconds = data["duration_seconds"]
    reason = data.get("reason", "")

    if error := check_guild(key, guild_id):
        return web.json_response({"error": error}, status=422)

    if result := check_timeout_duration(key, duration_seconds):
        error, status = result
        return web.json_response(
            {"error": error, "max_seconds": key.timeout_member_max_duration},
            status=status,
        )

    if (retry_after := rate_limiter.check(key)) is not None:
        log_action("timeout_member", guild_id, user_id, reason, "rate_limited")
        return web.json_response(
            {"error": "rate_limited", "retry_after_seconds": retry_after}, status=429
        )

    guild = bot.get_guild(guild_id)
    if guild is None:
        return web.json_response({"error": "guild_not_found"}, status=404)

    try:
        member = await guild.fetch_member(user_id)
    except discord.NotFound:
        log_action("timeout_member", guild_id, user_id, reason, "not_found")
        return web.json_response({"error": "member_not_found"}, status=404)

    if check_moderator(member):
        log_action("timeout_member", guild_id, user_id, reason, "target_is_moderator")
        return web.json_response({"error": "target_is_moderator"}, status=422)

    if member.is_timed_out():
        until = member.timed_out_until.isoformat() if member.timed_out_until else None
        log_action("timeout_member", guild_id, user_id, reason, "already_timed_out")
        return web.json_response(
            {"error": "already_timed_out", "until": until}, status=409
        )

    until = datetime.now(UTC) + timedelta(seconds=duration_seconds)
    try:
        await member.timeout(until, reason=reason)
    except discord.Forbidden:
        log_action("timeout_member", guild_id, user_id, reason, "forbidden")
        return web.json_response({"error": "missing_permissions"}, status=403)

    log_action("timeout_member", guild_id, user_id, reason, "success")
    return web.json_response(
        {
            "status": "ok",
            "action": "timeout_member",
            "user_id": user_id,
            "until": until.isoformat(),
        }
    )


async def handle_kick_member(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    key: KeyConfig = request["key"]
    data = await request.json()

    guild_id = data["guild_id"]
    user_id = data["user_id"]
    reason = data.get("reason", "")

    if error := check_guild(key, guild_id):
        return web.json_response({"error": error}, status=422)

    if (retry_after := rate_limiter.check(key)) is not None:
        log_action("kick_member", guild_id, user_id, reason, "rate_limited")
        return web.json_response(
            {"error": "rate_limited", "retry_after_seconds": retry_after}, status=429
        )

    guild = bot.get_guild(guild_id)
    if guild is None:
        return web.json_response({"error": "guild_not_found"}, status=404)

    try:
        member = await guild.fetch_member(user_id)
    except discord.NotFound:
        log_action("kick_member", guild_id, user_id, reason, "not_found")
        return web.json_response({"error": "member_not_found"}, status=404)

    if check_moderator(member):
        log_action("kick_member", guild_id, user_id, reason, "target_is_moderator")
        return web.json_response({"error": "target_is_moderator"}, status=422)

    if age_check := check_member_age(key.kick_member_max_age, member):
        error, age, status = age_check
        log_action("kick_member", guild_id, user_id, reason, error)
        return web.json_response({"error": error, "age_seconds": age}, status=status)

    try:
        await member.kick(reason=reason)
    except discord.Forbidden:
        log_action("kick_member", guild_id, user_id, reason, "forbidden")
        return web.json_response({"error": "missing_permissions"}, status=403)

    log_action("kick_member", guild_id, user_id, reason, "success")
    return web.json_response(
        {"status": "ok", "action": "kick_member", "user_id": user_id}
    )


async def handle_ban_member(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    key: KeyConfig = request["key"]
    data = await request.json()

    guild_id = data["guild_id"]
    user_id = data["user_id"]
    reason = data.get("reason", "")

    if error := check_guild(key, guild_id):
        return web.json_response({"error": error}, status=422)

    if (retry_after := rate_limiter.check(key)) is not None:
        log_action("ban_member", guild_id, user_id, reason, "rate_limited")
        return web.json_response(
            {"error": "rate_limited", "retry_after_seconds": retry_after}, status=429
        )

    guild = bot.get_guild(guild_id)
    if guild is None:
        return web.json_response({"error": "guild_not_found"}, status=404)

    try:
        member = await guild.fetch_member(user_id)
    except discord.NotFound:
        log_action("ban_member", guild_id, user_id, reason, "not_found")
        return web.json_response({"error": "member_not_found"}, status=404)

    if check_moderator(member):
        log_action("ban_member", guild_id, user_id, reason, "target_is_moderator")
        return web.json_response({"error": "target_is_moderator"}, status=422)

    if age_check := check_member_age(key.ban_member_max_age, member):
        error, age, status = age_check
        log_action("ban_member", guild_id, user_id, reason, error)
        return web.json_response({"error": error, "age_seconds": age}, status=status)

    try:
        await member.ban(reason=reason)
    except discord.Forbidden:
        log_action("ban_member", guild_id, user_id, reason, "forbidden")
        return web.json_response({"error": "missing_permissions"}, status=403)

    log_action("ban_member", guild_id, user_id, reason, "success")
    return web.json_response(
        {"status": "ok", "action": "ban_member", "user_id": user_id}
    )
