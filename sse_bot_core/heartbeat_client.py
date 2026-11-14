"""Heartbeat client for reporting an SSE bot's liveness to SSE Bots Hub.

Call start_heartbeat(bot) after the Discord client has been created.
Required environment variables: HUB_HEARTBEAT_URL, BOT_ACTIVATION_ID,
HUB_HEARTBEAT_SECRET. The heartbeat secret is never logged.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import aiohttp

log = logging.getLogger("sse-heartbeat")


def _get_heartbeat_interval() -> int:
    raw = os.getenv("HUB_HEARTBEAT_INTERVAL", "45").strip()
    try:
        return max(30, min(3600, int(raw)))
    except (TypeError, ValueError):
        return 45


async def heartbeat_loop(client: Any) -> None:
    base_url = os.getenv("HUB_HEARTBEAT_URL", "").strip().rstrip("/")
    activation_id = os.getenv("BOT_ACTIVATION_ID", "").strip()
    secret = os.getenv("HUB_HEARTBEAT_SECRET", "").strip()

    if not base_url or not activation_id or not secret:
        log.warning(
            "Heartbeat disabled: configure HUB_HEARTBEAT_URL, "
            "BOT_ACTIVATION_ID and HUB_HEARTBEAT_SECRET."
        )
        return

    endpoint = f"{base_url}/heartbeat"
    timeout = aiohttp.ClientTimeout(total=10, connect=5)
    interval = _get_heartbeat_interval()

    await client.wait_until_ready()
    if client.is_closed():
        return

    connector = aiohttp.TCPConnector(limit=2, enable_cleanup_closed=True)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        while not client.is_closed():
            payload = {
                "activation_id": activation_id,
                "secret": secret,
                "guild_ids": [str(guild.id) for guild in getattr(client, "guilds", [])],
            }
            try:
                async with session.post(endpoint, json=payload) as response:
                    if response.status == 200:
                        log.debug("SSE Hub heartbeat accepted")
                    elif response.status in (401, 403, 404):
                        log.error(
                            "SSE Hub rejected heartbeat (HTTP %s). "
                            "Check the activation ID and heartbeat configuration.",
                            response.status,
                        )
                    else:
                        log.warning("SSE Hub heartbeat returned HTTP %s", response.status)
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError):
                log.warning("Could not send heartbeat to SSE Bots Hub", exc_info=True)

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise


def start_heartbeat(client: Any) -> asyncio.Task | None:
    """Start at most one heartbeat task for this Discord client."""
    existing = getattr(client, "_sse_heartbeat_task", None)
    if existing is not None and not existing.done():
        return existing

    task = asyncio.create_task(heartbeat_loop(client), name="sse-hub-heartbeat")
    setattr(client, "_sse_heartbeat_task", task)
    return task
