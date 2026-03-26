import logging

log = logging.getLogger("sentinel.audit")


def log_action(
    action: str,
    guild_id: int,
    target_id: int,
    reason: str,
    status: str,
) -> None:
    log.info(
        f"{action} guild={guild_id} target={target_id} status={status} reason={reason!r}"
    )
