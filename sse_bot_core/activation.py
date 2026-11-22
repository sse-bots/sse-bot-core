"""Plug-and-play activation logic for SSE Discord bots.

This module contains activation validation and enforcement only. It does NOT
create keys, process support requests, or approve requests; those belong to
SSE Bots Hub / SSE Nexus and the SSE Web Dashboard.

Integration (after creating your commands and bot instance):
    from sse_bot_core.activation import setup_activation
    await setup_activation(bot)

Required .env values:
    MONGODB_URI=...
    BOT_ACTIVATION_ID=SSE-YOURBOT-XXXXXXXX
    ACTIVATION_DATABASE=sse_bots_user   # optional; this is the default

The Discord bot ID is read from bot.user.id at runtime and checked against its
registered Hub record, so it does not need a separate .env variable.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import discord
from bson import ObjectId
from discord import app_commands
from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

log = logging.getLogger("sse-activation")

__all__ = [
    "ActivationService",
    "setup_activation",
    "hash_activation_key",
    "_configure_srv_dns",
    "utcnow",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalize naive or aware datetimes returned by PyMongo to UTC-aware."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def hash_activation_key(key: str) -> str:
    normalized = "".join((key or "").strip().upper().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _configure_srv_dns(uri: str) -> None:
    if not uri.lower().startswith("mongodb+srv://"):
        return
    try:
        import dns.resolver
    except ImportError as exc:
        raise RuntimeError("Install dnspython: pip install dnspython") from exc
    configured = os.getenv("MONGODB_DNS_NAMESERVERS", "").strip()
    if configured:
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = [x.strip() for x in configured.split(",") if x.strip()]
        resolver.timeout = 5
        resolver.lifetime = 10
        dns.resolver.default_resolver = resolver


class ActivationService:
    """Database layer for checking and consuming single, bundle, and global keys."""

    def __init__(
        self,
        mongo_client: MongoClient,
        activation_id: str,
        discord_bot_id: Optional[int] = None,
    ):
        self.client = mongo_client
        self.db = mongo_client[os.getenv("ACTIVATION_DATABASE", "sse_bots_user")]
        self.activation_id = activation_id.strip()
        self.discord_bot_id = (
            str(discord_bot_id) if discord_bot_id is not None else None
        )
        self.bots = self.db.bots
        self.keys = self.db.activation_keys
        self.servers = self.db.activated_servers
        self.logs = self.db.activation_logs
        self.requests = self.db.activation_requests
        self.web_sync_events = self.db.web_sync_events

    def initialize_indexes(self) -> None:
        self.bots.create_index([("activation_id", ASCENDING)], unique=True)
        self.bots.create_index([("discord_bot_id", ASCENDING)], unique=True)
        self.keys.create_index([("key_hash", ASCENDING)], unique=True)
        self.servers.create_index(
            [("bot_activation_id", ASCENDING), ("guild_id", ASCENDING)], unique=True
        )
        self.logs.create_index(
            [("bot_activation_id", ASCENDING), ("created_at", ASCENDING)]
        )
        self.web_sync_events.create_index(
            [
                ("type", ASCENDING),
                ("bot_activation_id", ASCENDING),
                ("status", ASCENDING),
            ]
        )

    def _registered_bot(self, discord_bot_id: int | str) -> Optional[dict]:
        return self.bots.find_one(
            {
                "activation_id": self.activation_id,
                "discord_bot_id": str(discord_bot_id),
                "status": "active",
            }
        )

    def is_server_activated(
        self, guild_id: int, discord_bot_id: Optional[int] = None
    ) -> bool:
        discord_bot_id = (
            discord_bot_id if discord_bot_id is not None else self.discord_bot_id
        )
        if (
            not self.activation_id
            or not discord_bot_id
            or not self._registered_bot(discord_bot_id)
        ):
            return False
        record = self.servers.find_one(
            {
                "bot_activation_id": self.activation_id,
                "discord_bot_id": str(discord_bot_id),
                "guild_id": str(guild_id),
                "status": "active",
            }
        )
        if not record:
            return False
        expiry = _as_utc(record.get("expires_at"))
        if expiry is not None and expiry <= utcnow():
            return False
        return True

    def server_status(
        self, guild_id: int, discord_bot_id: Optional[int] = None
    ) -> str:
        """Return active, inactive, or removed for this bot in a Discord server."""
        discord_bot_id = (
            discord_bot_id if discord_bot_id is not None else self.discord_bot_id
        )
        if (
            not self.activation_id
            or not discord_bot_id
            or not self._registered_bot(discord_bot_id)
        ):
            return "removed"
        record = self.servers.find_one(
            {
                "bot_activation_id": self.activation_id,
                "discord_bot_id": str(discord_bot_id),
                "guild_id": str(guild_id),
            }
        )
        if not record:
            return "removed"
        status = str(record.get("status", "")).lower()
        if status == "active":
            expiry = _as_utc(record.get("expires_at"))
            if expiry is not None and expiry <= utcnow():
                return "removed"
            return "active"
        if status == "inactive":
            return "inactive"
        # Revoked or unknown records require a fresh activation key.
        return "removed"

    def deactivate_server(
        self, guild_id: int, user_id: int, discord_bot_id: Optional[int] = None
    ) -> tuple[bool, str]:
        discord_bot_id = (
            discord_bot_id if discord_bot_id is not None else self.discord_bot_id
        )
        query = {
            "bot_activation_id": self.activation_id,
            "discord_bot_id": str(discord_bot_id),
            "guild_id": str(guild_id),
            "status": "active",
        }
        record = self.servers.find_one_and_update(
            query,
            {
                "$set": {
                    "status": "inactive",
                    "deactivated_at": utcnow(),
                    "deactivated_by": str(user_id),
                }
            },
        )
        if not record:
            return (
                False,
                "This server does not have an active activation to deactivate.",
            )
        self.logs.insert_one(
            {
                "bot_activation_id": self.activation_id,
                "discord_bot_id": str(discord_bot_id),
                "guild_id": str(guild_id),
                "user_id": str(user_id),
                "event": "deactivated",
                "created_at": utcnow(),
            }
        )
        bot_name = record.get("bot_name") or "This SSE bot"
        return True, f"**{bot_name}** has been deactivated in this server."

    def reactivate_server(
        self, guild_id: int, user_id: int, discord_bot_id: Optional[int] = None
    ) -> tuple[bool, str]:
        discord_bot_id = (
            discord_bot_id if discord_bot_id is not None else self.discord_bot_id
        )
        bot_doc = self._registered_bot(discord_bot_id) if discord_bot_id else None
        if not bot_doc:
            return False, "This bot is currently disabled in SSE Bots Hub."
        query = {
            "bot_activation_id": self.activation_id,
            "discord_bot_id": str(discord_bot_id),
            "guild_id": str(guild_id),
            "status": "inactive",
        }
        record = self.servers.find_one_and_update(
            query,
            {
                "$set": {
                    "status": "active",
                    "reactivated_at": utcnow(),
                    "reactivated_by": str(user_id),
                },
                "$unset": {"deactivated_at": "", "deactivated_by": ""},
            },
        )
        if not record:
            return (
                False,
                "No deactivated server activation was found. If the server was removed, use `/activation` with a new key.",
            )
        self.logs.insert_one(
            {
                "bot_activation_id": self.activation_id,
                "discord_bot_id": str(discord_bot_id),
                "guild_id": str(guild_id),
                "user_id": str(user_id),
                "event": "reactivated",
                "created_at": utcnow(),
            }
        )
        bot_name = bot_doc.get("name") or record.get("bot_name") or "This SSE bot"
        return True, f"**{bot_name}** has been reactivated in this server."

    def remove_server(
        self, guild_id: int, user_id: int, discord_bot_id: Optional[int] = None
    ) -> tuple[bool, str]:
        discord_bot_id = (
            discord_bot_id if discord_bot_id is not None else self.discord_bot_id
        )
        query = {
            "bot_activation_id": self.activation_id,
            "discord_bot_id": str(discord_bot_id),
            "guild_id": str(guild_id),
        }
        record = self.servers.find_one_and_delete(query)
        if not record:
            return False, "No activation record was found for this server."
        self.logs.insert_one(
            {
                "bot_activation_id": self.activation_id,
                "discord_bot_id": str(discord_bot_id),
                "guild_id": str(guild_id),
                "user_id": str(user_id),
                "event": "server_removed",
                "created_at": utcnow(),
                "key_id": record.get("key_id"),
            }
        )
        return (
            True,
            "This server's activation record has been permanently removed. A new activation key is required to use the bot again.",
        )

    def _resolve_key_scope(self, key_doc: dict) -> str:
        raw_scope = str(key_doc.get("key_scope") or "").strip().lower()
        if raw_scope in {"single", "bundle", "global"}:
            return raw_scope
        primary = str(key_doc.get("bot_activation_id") or "")
        if primary == "__GLOBAL__":
            return "global"
        if primary == "__BUNDLE__":
            return "bundle"
        return "single"

    def activate(
        self,
        key: str,
        guild_id: int,
        user_id: int,
        discord_bot_id: Optional[int] = None,
    ) -> tuple[bool, str]:
        discord_bot_id = (
            discord_bot_id if discord_bot_id is not None else self.discord_bot_id
        )
        if not self.activation_id:
            return False, "BOT_ACTIVATION_ID is missing from this bot's .env file."
        if not discord_bot_id:
            return False, "Could not identify this Discord bot. Restart it and try again."
        bot_doc = self._registered_bot(discord_bot_id)
        if not bot_doc:
            return False, "This bot is not registered or enabled in SSE Bots Hub."
        if len((key or "").strip()) < 16:
            return False, "That activation key is invalid."

        key_hash = hash_activation_key(key)
        guild_id_str = str(guild_id)
        user_id_str = str(user_id)
        activated_names: list[str] = []
        used_scope = "single"

        try:
            with self.client.start_session() as session:
                def transaction(s):
                    nonlocal activated_names, used_scope
                    current_time = utcnow()

                    current_bot = self.bots.find_one(
                        {
                            "activation_id": self.activation_id,
                            "discord_bot_id": str(discord_bot_id),
                            "status": "active",
                        },
                        session=s,
                    )
                    if not current_bot:
                        raise ValueError("This bot has been disabled in SSE Bots Hub.")

                    key_doc = self.keys.find_one(
                        {
                            "key_hash": key_hash,
                            "status": "unused",
                        },
                        session=s,
                    )
                    if not key_doc:
                        raise ValueError(
                            "Key is invalid, already used, or has been revoked."
                        )

                    # Enforce optional key expiration timestamp.
                    key_expiry = _as_utc(key_doc.get("expires_at"))
                    if key_expiry is not None and key_expiry <= current_time:
                        self.keys.update_one(
                            {"_id": key_doc["_id"], "status": "unused"},
                            {
                                "$set": {
                                    "status": "revoked",
                                    "revoked_at": current_time,
                                    "revocation_reason": "Key expired",
                                },
                                "$unset": {"encrypted_key": ""},
                            },
                            session=s,
                        )
                        raise ValueError("This activation key has expired.")

                    # Enforce optional guild binding.
                    bound_guild = str(key_doc.get("guild_id") or "").strip()
                    if bound_guild and bound_guild != guild_id_str:
                        raise ValueError(
                            "This activation key is bound to a different Discord server."
                        )

                    scope = self._resolve_key_scope(key_doc)
                    used_scope = scope

                    # Determine all target bots covered by this key.
                    target_bots: list[dict] = []
                    if scope == "single":
                        key_bot_id = str(key_doc.get("bot_activation_id") or "")
                        listed_ids = [
                            str(x) for x in (key_doc.get("bot_activation_ids") or []) if x
                        ]
                        if (
                            key_bot_id != self.activation_id
                            and self.activation_id not in listed_ids
                        ):
                            raise ValueError(
                                "This activation key belongs to a different SSE bot."
                            )
                        target_bots = [current_bot]
                    elif scope == "bundle":
                        allowed_ids = [
                            str(x) for x in (key_doc.get("bot_activation_ids") or []) if x
                        ]
                        if self.activation_id not in allowed_ids:
                            raise ValueError(
                                "This bundle key does not include this SSE bot."
                            )
                        target_bots = list(
                            self.bots.find(
                                {
                                    "activation_id": {"$in": allowed_ids},
                                    "status": "active",
                                },
                                session=s,
                            )
                        )
                        if not any(
                            b.get("activation_id") == self.activation_id
                            for b in target_bots
                        ):
                            target_bots.append(current_bot)
                    else:  # global
                        target_bots = list(
                            self.bots.find(
                                {
                                    "status": "active",
                                    "activation_required": True,
                                },
                                session=s,
                            )
                        )
                        if not any(
                            b.get("activation_id") == self.activation_id
                            for b in target_bots
                        ):
                            target_bots.append(current_bot)

                    # Filter out bots that are already active in this server.
                    target_ids = [str(b["activation_id"]) for b in target_bots]
                    already_active_rows = list(
                        self.servers.find(
                            {
                                "bot_activation_id": {"$in": target_ids},
                                "guild_id": guild_id_str,
                                "status": "active",
                            },
                            {"bot_activation_id": 1},
                            session=s,
                        )
                    )
                    already_active_ids = {
                        str(r.get("bot_activation_id")) for r in already_active_rows
                    }

                    bots_to_activate = [
                        b
                        for b in target_bots
                        if str(b["activation_id"]) not in already_active_ids
                    ]
                    if not bots_to_activate:
                        if scope == "single":
                            raise ValueError(
                                "This server has already activated this bot."
                            )
                        raise ValueError(
                            "This server has already activated all bots covered by this key."
                        )

                    redeemed_ids = [str(b["activation_id"]) for b in bots_to_activate]

                    # Consume the key atomically and remove any web-cached ciphertext.
                    consumed = self.keys.update_one(
                        {"_id": key_doc["_id"], "status": "unused"},
                        {
                            "$set": {
                                "status": "used",
                                "used_at": current_time,
                                "used_by_guild_id": guild_id_str,
                                "used_by_user_id": user_id_str,
                                "redeemed_bot_ids": redeemed_ids,
                            },
                            "$unset": {"encrypted_key": ""},
                        },
                        session=s,
                    )
                    if consumed.modified_count != 1:
                        raise ValueError("This key has already been used.")

                    # Clear encrypted key on linked web/Discord activation request.
                    request_id_raw = str(key_doc.get("request_id") or "").strip()
                    if request_id_raw and ObjectId.is_valid(request_id_raw):
                        self.requests.update_one(
                            {"_id": ObjectId(request_id_raw)},
                            {
                                "$unset": {"encrypted_key": ""},
                                "$set": {"redeemed_at": current_time},
                            },
                            session=s,
                        )

                    activated_names = []
                    for target_bot in bots_to_activate:
                        t_aid = str(target_bot["activation_id"])
                        t_did = str(target_bot.get("discord_bot_id") or "")
                        t_name = str(target_bot.get("name") or t_aid)

                        # Upsert so previously revoked/inactive licenses reactivate cleanly.
                        self.servers.update_one(
                            {
                                "bot_activation_id": t_aid,
                                "guild_id": guild_id_str,
                            },
                            {
                                "$set": {
                                    "bot_activation_id": t_aid,
                                    "bot_name": t_name,
                                    "discord_bot_id": t_did,
                                    "guild_id": guild_id_str,
                                    "activated_by": user_id_str,
                                    "activated_at": current_time,
                                    "status": "active",
                                    "key_id": str(key_doc["_id"]),
                                    "key_scope": scope,
                                },
                                "$unset": {
                                    "deactivated_at": "",
                                    "deactivated_by": "",
                                    "revoked_at": "",
                                    "revoked_by": "",
                                    "revocation_reason": "",
                                },
                            },
                            upsert=True,
                            session=s,
                        )

                        self.logs.insert_one(
                            {
                                "bot_activation_id": t_aid,
                                "discord_bot_id": t_did,
                                "guild_id": guild_id_str,
                                "user_id": user_id_str,
                                "key_scope": scope,
                                "key_id": str(key_doc["_id"]),
                                "event": "activated",
                                "created_at": current_time,
                            },
                            session=s,
                        )

                        # Ask all other affected SSE bots to refresh slash commands for this guild.
                        if t_aid != self.activation_id:
                            self.web_sync_events.insert_one(
                                {
                                    "type": "sync_guild_commands",
                                    "bot_activation_id": t_aid,
                                    "guild_id": guild_id_str,
                                    "status": "pending",
                                    "created_at": current_time,
                                },
                                session=s,
                            )

                        activated_names.append(t_name)

                session.with_transaction(transaction)

            if used_scope == "single" or len(activated_names) == 1:
                only_name = activated_names[0] if activated_names else bot_doc.get("name", "this bot")
                return (
                    True,
                    f"Successfully activated **{only_name}** in this server.",
                )

            scope_label = "Global" if used_scope == "global" else "Bundle"
            names_preview = ", ".join(activated_names[:15])
            if len(activated_names) > 15:
                names_preview += f" (+{len(activated_names) - 15} more)"
            return (
                True,
                f"Successfully activated **{len(activated_names)}** SSE bot(s) in this server with your **{scope_label}** key:\n{names_preview}",
            )
        except ValueError as exc:
            return False, str(exc)
        except DuplicateKeyError:
            return False, "This server or activation key has already been used."
        except PyMongoError:
            log.exception("Activation database transaction failed")
            return (
                False,
                "Activation service is temporarily unavailable. Please try again later.",
            )

    def claim_pending_sync_events(self, limit: int = 20) -> list[dict]:
        """Atomically claim pending sync_guild_commands events for this bot."""
        claimed: list[dict] = []
        for _ in range(limit):
            doc = self.web_sync_events.find_one_and_update(
                {
                    "type": "sync_guild_commands",
                    "bot_activation_id": {"$in": [self.activation_id, "__ALL__"]},
                    "status": "pending",
                },
                {
                    "$set": {
                        "status": "processing",
                        "claimed_at": utcnow(),
                        "claimed_by": self.activation_id,
                    }
                },
                sort=[("created_at", ASCENDING)],
                return_document=ReturnDocument.AFTER,
            )
            if not doc:
                break
            claimed.append(doc)
        return claimed

    def finish_sync_event(
        self, event_id, *, ok: bool, error: Optional[str] = None
    ) -> None:
        update: dict = {
            "status": "done" if ok else "failed",
            "processed_at": utcnow(),
        }
        if error:
            update["error"] = error[:300]
        self.web_sync_events.update_one({"_id": event_id}, {"$set": update})


async def setup_activation(bot: discord.Client) -> ActivationService:
    """Install three-state activation, multi-bot key support, and web-synced commands.

    Safe to call more than once on the same bot instance.
    """
    existing = getattr(bot, "_sse_activation_service", None)
    if existing is not None:
        return existing
    if not hasattr(bot, "tree"):
        raise TypeError("setup_activation(bot) requires a discord.py client with a command tree.")

    uri = os.getenv("MONGODB_URI", "").strip()
    activation_id = os.getenv("BOT_ACTIVATION_ID", "").strip()
    if not uri:
        raise RuntimeError("MONGODB_URI is required for activation.")
    if not activation_id:
        raise RuntimeError("BOT_ACTIVATION_ID is required for activation.")

    _configure_srv_dns(uri)
    client = MongoClient(
        uri,
        serverSelectionTimeoutMS=15000,
        connectTimeoutMS=10000,
        appname="SSE-Bot-Activation",
        tz_aware=True,
        tzinfo=timezone.utc,
    )
    service = ActivationService(client, activation_id)
    try:
        await asyncio.to_thread(client.admin.command, "ping")
        await asyncio.to_thread(service.initialize_indexes)
    except Exception:
        client.close()
        log.exception("Could not initialize the SSE activation database")
        raise

    # --------------------------------------------------
    # COMMAND SYNCHRONIZATION
    # --------------------------------------------------
    tree = bot.tree

    async def sync_guild_commands(guild: discord.Guild):
        """Synchronize commands according to this server's activation state."""
        bot_user_id = bot.user.id if bot.user else None
        if not bot_user_id:
            return
        status = await asyncio.to_thread(service.server_status, guild.id, bot_user_id)

        # Rebuild this guild's command list from the bot's
        # complete locally registered command list.
        tree.clear_commands(guild=guild)
        tree.copy_global_to(guild=guild)
        commands = list(tree.get_commands(guild=guild))

        if status == "active":
            allowed = None  # All normal commands and management commands.
        elif status == "inactive":
            allowed = {"reactivate"}
        else:
            allowed = {"activation"}

        for command in commands:
            if allowed is None:
                # Active: hide activation and reactivation.
                if command.name in {"activation", "reactivate"}:
                    tree.remove_command(command.name, guild=guild)
            elif command.name not in allowed:
                tree.remove_command(command.name, guild=guild)

        await tree.sync(guild=guild)
        log.info(
            "Synced commands for guild %s (activation status: %s)",
            guild.id,
            status,
        )

    async def sync_all_guilds():
        """Synchronize every server the bot currently belongs to."""
        for guild in list(bot.guilds):
            try:
                await sync_guild_commands(guild)
            except Exception:
                log.exception(
                    "Could not synchronize commands for guild %s",
                    guild.id,
                )

    async def web_sync_poll_loop():
        """Poll web_sync_events so web or bundle/global changes sync slash commands quickly."""
        await bot.wait_until_ready()
        while not bot.is_closed():
            try:
                events = await asyncio.to_thread(service.claim_pending_sync_events, 20)
                for ev in events:
                    guild_id_raw = str(ev.get("guild_id") or "").strip()
                    if not guild_id_raw.isdigit():
                        await asyncio.to_thread(
                            service.finish_sync_event,
                            ev["_id"],
                            ok=False,
                            error="invalid_guild_id",
                        )
                        continue
                    guild = bot.get_guild(int(guild_id_raw))
                    if guild is None:
                        await asyncio.to_thread(
                            service.finish_sync_event,
                            ev["_id"],
                            ok=True,
                        )
                        continue
                    try:
                        await sync_guild_commands(guild)
                        await asyncio.to_thread(
                            service.finish_sync_event,
                            ev["_id"],
                            ok=True,
                        )
                    except Exception as exc:
                        log.exception(
                            "Failed web-triggered command sync for guild %s",
                            guild_id_raw,
                        )
                        await asyncio.to_thread(
                            service.finish_sync_event,
                            ev["_id"],
                            ok=False,
                            error=f"{type(exc).__name__}: {exc}",
                        )
            except Exception:
                log.exception("Web sync event poll failed for %s", activation_id)
            await asyncio.sleep(15)

    # --------------------------------------------------
    # ACTIVATION COMMAND
    # --------------------------------------------------
    @app_commands.describe(key="Enter the single, bundle, or global activation key from SSE Nexus.")
    async def activation_command(
        interaction: discord.Interaction,
        key: str,
    ):
        if interaction.guild is None:
            return await interaction.response.send_message(
                "Use this command inside the Discord server you want to activate.",
                ephemeral=True,
            )
        if interaction.guild.owner_id != interaction.user.id:
            return await interaction.response.send_message(
                "Only the actual Discord server owner can activate this bot.",
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot_user_id = bot.user.id if bot.user else None
        if not bot_user_id:
            return await interaction.followup.send(
                "Bot identity is not ready. Try again shortly.",
                ephemeral=True,
            )
        success, message = await asyncio.to_thread(
            service.activate,
            key,
            interaction.guild.id,
            interaction.user.id,
            bot_user_id,
        )
        await interaction.followup.send(message, ephemeral=True)
        if success:
            await sync_guild_commands(interaction.guild)
            log.info(
                "Activated bot %s in guild %s",
                activation_id,
                interaction.guild.id,
            )

    # --------------------------------------------------
    # DEACTIVATE COMMAND
    # --------------------------------------------------
    async def deactivate_command(interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
        if interaction.guild.owner_id != interaction.user.id:
            return await interaction.response.send_message(
                "Only the actual server owner can deactivate this bot.",
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot_user_id = bot.user.id if bot.user else None
        success, message = await asyncio.to_thread(
            service.deactivate_server,
            interaction.guild.id,
            interaction.user.id,
            bot_user_id,
        )
        await interaction.followup.send(message, ephemeral=True)
        if success:
            await sync_guild_commands(interaction.guild)
            log.info("Deactivated guild %s", interaction.guild.id)

    # --------------------------------------------------
    # REACTIVATE COMMAND
    # --------------------------------------------------
    async def reactivate_command(interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
        if interaction.guild.owner_id != interaction.user.id:
            return await interaction.response.send_message(
                "Only the actual server owner can reactivate this bot.",
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot_user_id = bot.user.id if bot.user else None
        success, message = await asyncio.to_thread(
            service.reactivate_server,
            interaction.guild.id,
            interaction.user.id,
            bot_user_id,
        )
        await interaction.followup.send(message, ephemeral=True)
        if success:
            await sync_guild_commands(interaction.guild)
            log.info("Reactivated guild %s", interaction.guild.id)

    # --------------------------------------------------
    # PERMANENT SERVER REMOVAL COMMAND
    # --------------------------------------------------
    async def remove_server_command(interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
        if interaction.guild.owner_id != interaction.user.id:
            return await interaction.response.send_message(
                "Only the actual server owner can remove this server's activation.",
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot_user_id = bot.user.id if bot.user else None
        success, message = await asyncio.to_thread(
            service.remove_server,
            interaction.guild.id,
            interaction.user.id,
            bot_user_id,
        )
        await interaction.followup.send(message, ephemeral=True)
        if success:
            await sync_guild_commands(interaction.guild)
            log.info("Permanently removed guild %s", interaction.guild.id)

    # --------------------------------------------------
    # REGISTER COMMANDS
    # --------------------------------------------------
    command_definitions = [
        app_commands.Command(
            name="activation",
            description="Activate this SSE bot using an activation key.",
            callback=activation_command,
        ),
        app_commands.Command(
            name="deactivate",
            description="Temporarily deactivate this bot in this server.",
            callback=deactivate_command,
        ),
        app_commands.Command(
            name="reactivate",
            description="Reactivate this bot in this server.",
            callback=reactivate_command,
        ),
        app_commands.Command(
            name="remove_server",
            description="Permanently remove this server's activation.",
            callback=remove_server_command,
        ),
    ]

    for command in command_definitions:
        try:
            tree.add_command(command)
        except app_commands.CommandAlreadyRegistered:
            log.warning(
                "Command /%s is already registered.",
                command.name,
            )

    # --------------------------------------------------
    # GLOBAL COMMAND REMOVAL
    # --------------------------------------------------
    original_commands = list(tree.get_commands())
    tree.clear_commands(guild=None)
    await tree.sync()
    for command in original_commands:
        try:
            tree.add_command(command)
        except app_commands.CommandAlreadyRegistered:
            log.warning(
                "Could not restore local command /%s",
                command.name,
            )

    # --------------------------------------------------
    # GLOBAL INTERACTION ENFORCEMENT
    # --------------------------------------------------
    previous_check = getattr(tree, "interaction_check", None)

    async def activation_interaction_check(
        interaction: discord.Interaction,
    ) -> bool:
        if previous_check is not None:
            allowed = await previous_check(interaction)
            if not allowed:
                return False

        if interaction.guild is None:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Commands can only be used inside a Discord server.",
                    ephemeral=True,
                )
            return False

        command_name = (
            interaction.command.name if interaction.command is not None else None
        )
        bot_user_id = bot.user.id if bot.user else None
        if not bot_user_id:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Activation verification is not ready. Try again shortly.",
                    ephemeral=True,
                )
            return False

        try:
            status = await asyncio.to_thread(
                service.server_status,
                interaction.guild.id,
                bot_user_id,
            )
        except Exception:
            log.exception("Activation verification failed")
            status = "removed"

        # Never activated or permanently removed.
        if status == "removed":
            if command_name == "activation":
                return True
            await interaction.response.send_message(
                "This server has not activated this SSE bot. "
                "Use `/activation` to activate it.",
                ephemeral=True,
            )
            return False

        # Temporarily deactivated.
        if status == "inactive":
            if command_name == "reactivate":
                return True
            await interaction.response.send_message(
                "This bot is currently deactivated in this server. "
                "Use `/reactivate` to restore access.",
                ephemeral=True,
            )
            return False

        # Active.
        if command_name in {"activation", "reactivate"}:
            await interaction.response.send_message(
                "This command is not available in the current activation state.",
                ephemeral=True,
            )
            return False

        return True

    tree.interaction_check = activation_interaction_check

    # --------------------------------------------------
    # AUTOMATIC GUILD SYNCHRONIZATION
    # --------------------------------------------------
    async def activation_on_ready():
        # Re-sync on reconnect as Hub/Web status may have changed while offline.
        await sync_all_guilds()
        existing_task = getattr(bot, "_sse_activation_web_sync_task", None)
        if existing_task is None or existing_task.done():
            setattr(
                bot,
                "_sse_activation_web_sync_task",
                asyncio.create_task(web_sync_poll_loop()),
            )

    async def activation_on_guild_join(guild: discord.Guild):
        await sync_guild_commands(guild)

    async def activation_on_guild_remove(guild: discord.Guild):
        try:
            tree.clear_commands(guild=guild)
        except Exception:
            log.exception(
                "Could not clean up local commands for guild %s",
                guild.id,
            )

    bot.add_listener(activation_on_ready, "on_ready")
    bot.add_listener(activation_on_guild_join, "on_guild_join")
    bot.add_listener(activation_on_guild_remove, "on_guild_remove")

    setattr(bot, "_sse_activation_client", client)
    setattr(bot, "_sse_activation_service", service)
    setattr(bot, "_sse_activation_sync_guild", sync_guild_commands)
    setattr(bot, "_sse_activation_sync_all", sync_all_guilds)

    log.info(
        "Three-state activation system (single/bundle/global + web sync) initialized for %s",
        activation_id,
    )
    return service
