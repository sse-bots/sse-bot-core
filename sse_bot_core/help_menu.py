"""Reusable interactive slash-command help center for discord.py 2.x.

Call setup_help(bot) after registering commands and before syncing the command tree.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord import app_commands

log = logging.getLogger("sse-help")
HIDDEN_COMMANDS = {"help", "activation", "deactivate", "reactivate", "remove_server"}


def _children(command: Any) -> list[Any]:
    return list(command.commands) if isinstance(command, app_commands.Group) else []


def _description(command: Any) -> str:
    value = getattr(command, "description", None)
    return (str(value).strip() if value else "No description provided.")[:100]


def _qualified_name(command: Any) -> str:
    return str(getattr(command, "qualified_name", getattr(command, "name", "command")))


def _bot_name(bot: Any) -> str:
    user = getattr(bot, "user", None)
    return getattr(user, "name", None) or getattr(bot, "name", None) or "Discord Bot"


def _bot_avatar(bot: Any) -> str | None:
    avatar = getattr(getattr(bot, "user", None), "display_avatar", None)
    return str(avatar.url) if avatar else None


def _command_keys(command: Any) -> list[str]:
    name = str(getattr(command, "name", "")).lower().replace("-", "_")
    keys = [name] if name else []
    root = name.split("_", 1)[0] if name else ""
    if root and root not in keys:
        keys.append(root)
    return keys


async def _ensure_app_emojis(bot: Any) -> dict[str, str]:
    if getattr(bot, "_sse_help_emojis_loaded", False):
        return getattr(bot, "_sse_help_app_emojis", {}) or {}

    task = getattr(bot, "_sse_help_emojis_task", None)
    if task is None:
        async def fetch() -> dict[str, str]:
            fetcher = getattr(bot, "fetch_application_emojis", None)
            if not callable(fetcher):
                return {}
            try:
                items = await fetcher()
                return {
                    str(item.name).lower(): str(item)
                    for item in (items or [])
                    if getattr(item, "name", None)
                }
            except (discord.HTTPException, discord.Forbidden, AttributeError):
                log.debug("Could not fetch application emojis", exc_info=True)
                return {}

        task = asyncio.create_task(fetch())
        setattr(bot, "_sse_help_emojis_task", task)

    try:
        result = await task
    except Exception:
        log.debug("Application emoji loading failed", exc_info=True)
        result = {}

    setattr(bot, "_sse_help_app_emojis", result)
    setattr(bot, "_sse_help_emojis_loaded", True)
    return result


def _emoji_for(bot: Any, command: Any) -> str:
    keys = _command_keys(command)
    explicit = getattr(bot, "_sse_help_explicit_emojis", {}) or {}
    app_emojis = getattr(bot, "_sse_help_app_emojis", {}) or {}
    guild_emojis = {
        str(item.name).lower(): str(item)
        for item in (getattr(bot, "emojis", []) or [])
        if getattr(item, "name", None)
    }
    for mapping in (explicit, app_emojis, guild_emojis):
        normalized = {str(k).lower().replace("-", "_"): v for k, v in mapping.items()}
        for key in keys:
            if normalized.get(key):
                return str(normalized[key])
    if app_emojis.get("default"):
        return str(app_emojis["default"])
    return "📂" if _children(command) else "⚙️"


def _parse_emoji(value: str) -> Any:
    if value.startswith(("<:", "<a:")):
        try:
            return discord.PartialEmoji.from_str(value)
        except (TypeError, ValueError, AttributeError):
            return None
    return value or None


def _registered_commands(bot: Any) -> list[Any]:
    commands = list(bot.tree.get_commands())
    seen = {(c.name, getattr(c, "type", None)) for c in commands}
    # Include guild-only commands, without duplicating global commands.
    for guild in getattr(bot, "guilds", []):
        try:
            guild_commands = bot.tree.get_commands(guild=discord.Object(id=guild.id))
        except (TypeError, AttributeError):
            continue
        for command in guild_commands:
            key = (command.name, getattr(command, "type", None))
            if key not in seen:
                commands.append(command)
                seen.add(key)
    return sorted(
        (c for c in commands if getattr(c, "name", None) not in HIDDEN_COMMANDS),
        key=lambda c: getattr(c, "name", "").casefold(),
    )


def _parameter_lines(command: Any) -> list[str]:
    lines: list[str] = []
    for parameter in getattr(command, "parameters", []) or []:
        name = getattr(parameter, "display_name", None) or getattr(parameter, "name", "option")
        description = getattr(parameter, "description", None) or "No description provided."
        option_type = getattr(parameter, "type", None)
        type_name = getattr(option_type, "name", str(option_type or "option")).replace("_", " ").title()
        line = f"**{name}** (`{type_name}`, {'Required' if parameter.required else 'Optional'})\n{description}"
        choices = getattr(parameter, "choices", None) or []
        if choices:
            names = [str(getattr(choice, "name", choice)) for choice in choices[:10]]
            if len(choices) > 10:
                names.append("…")
            line += "\nChoices: " + ", ".join(names)
        minimum = getattr(parameter, "min_value", None)
        maximum = getattr(parameter, "max_value", None)
        if minimum is not None or maximum is not None:
            line += f"\nRange: {minimum if minimum is not None else 'No minimum'} to {maximum if maximum is not None else 'No maximum'}"
        channel_types = getattr(parameter, "channel_types", None) or []
        if channel_types:
            line += "\nChannel types: " + ", ".join(
                str(getattr(x, "name", x)).replace("_", " ").title() for x in channel_types
            )
        if getattr(parameter, "autocomplete", False):
            line += "\nAutocomplete: enabled"
        lines.append(line[:950])
    return lines


def _split_fields(lines: list[str], limit: int = 1000) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        addition = ("\n\n" if current else "") + line
        if current and len(current) + len(addition) > limit:
            chunks.append(current)
            current = line
        else:
            current += addition
    if current:
        chunks.append(current)
    return chunks


def _base_embed(bot: Any, commands: list[Any], page: int, page_size: int = 21) -> discord.Embed:
    name = _bot_name(bot)
    embed = discord.Embed(
        title=f"📚 {name} — Help Center"[:256],
        description="Browse the registered slash commands. Select a command or category to see its details.",
        color=discord.Color.blurple(),
    )
    if not commands:
        embed.add_field(name="No commands found", value="No visible slash commands are registered.", inline=False)
    else:
        visible = commands[page * page_size:(page + 1) * page_size]
        for command in visible:
            count = len(_children(command))
            label = str(command.name).replace("-", " ").replace("_", " ").title()
            detail = f"`/{command.name}`" + (f" • {count} subcommands" if count else " • Command")
            embed.add_field(
                name=f"{_emoji_for(bot, command)} {label}"[:256],
                value=f"{_description(command)}\n{detail}"[:1024],
                inline=True,
            )
        remainder = len(visible) % 3
        for _ in range((3 - remainder) % 3):
            embed.add_field(name="\u200b", value="\u200b", inline=True)
        pages = max(1, (len(commands) + page_size - 1) // page_size)
        embed.set_footer(text=f"{name} • Interactive Help • Page {page + 1}/{pages}")
    avatar = _bot_avatar(bot)
    if avatar:
        embed.set_thumbnail(url=avatar)
    if not commands:
        embed.set_footer(text=f"{name} • Interactive Help")
    return embed


class HelpCenterView(discord.ui.View):
    def __init__(self, bot: Any, owner_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.owner_id = owner_id
        self.commands = _registered_commands(bot)
        self.path: list[Any] = []
        self.page = 0
        self.message: discord.Message | None = None
        self._build()

    @property
    def current_nodes(self) -> list[Any]:
        return _children(self.path[-1]) if self.path else self.commands

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This help menu belongs to another user. Run `/help` to open your own.",
                ephemeral=True,
            )
            return False
        return True

    def _build(self) -> None:
        self.clear_items()
        nodes = self.current_nodes
        page_size = 21 if not self.path else 25
        max_page = max(0, (len(nodes) - 1) // page_size)
        self.page = min(self.page, max_page)
        start = self.page * page_size
        visible = nodes[start:start + page_size]

        if visible:
            options = []
            for offset, node in enumerate(visible):
                description = _description(node)
                if _children(node):
                    description = f"{len(_children(node))} subcommands • {description}"
                options.append(discord.SelectOption(
                    label=f"/{_qualified_name(node)}"[:100],
                    value=str(start + offset),
                    description=description[:100],
                    emoji=_parse_emoji(_emoji_for(self.bot, node)),
                ))
            select = discord.ui.Select(
                placeholder="Choose a command or subcommand…",
                min_values=1,
                max_values=1,
                options=options,
                row=0,
            )
            select.callback = self._on_select
            self.add_item(select)
        else:
            self.add_item(discord.ui.Button(
                label="No commands in this section",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                row=0,
            ))

        if self.path:
            back = discord.ui.Button(label="Back", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
            back.callback = self._on_back
            self.add_item(back)

        previous = discord.ui.Button(
            label="Previous", emoji="◀️", style=discord.ButtonStyle.secondary,
            disabled=self.page <= 0, row=1,
        )
        previous.callback = self._on_previous
        self.add_item(previous)

        next_button = discord.ui.Button(
            label="Next", emoji="▶️", style=discord.ButtonStyle.secondary,
            disabled=self.page >= max_page, row=1,
        )
        next_button.callback = self._on_next
        self.add_item(next_button)

        home = discord.ui.Button(
            label="Main menu", emoji="🏠", style=discord.ButtonStyle.primary,
            disabled=not self.path, row=1,
        )
        home.callback = self._on_home
        self.add_item(home)

    def _current_embed(self) -> discord.Embed:
        if not self.path:
            return _base_embed(self.bot, self.commands, self.page)
        command = self.path[-1]
        children = _children(command)
        embed = discord.Embed(
            title=f"{_emoji_for(self.bot, command)} /{_qualified_name(command)}"[:256],
            description=_description(command),
            color=discord.Color.blurple(),
        )
        avatar = _bot_avatar(self.bot)
        if avatar:
            embed.set_thumbnail(url=avatar)
        if children:
            start = self.page * 25
            visible = children[start:start + 25]
            lines = [f"• **/{_qualified_name(child)}** — {_description(child)}" for child in visible]
            embed.add_field(name="Subcommands", value="\n".join(lines)[:1024] or "No subcommands.", inline=False)
            embed.set_footer(text="Select a subcommand above to view its details.")
        elif isinstance(command, app_commands.Command):
            parameters = _parameter_lines(command)
            chunks = _split_fields(parameters)
            if not chunks:
                embed.add_field(name="Options", value="This command has no additional parameters.", inline=False)
            else:
                for index, chunk in enumerate(chunks[:5], 1):
                    embed.add_field(
                        name="Available options" if index == 1 else f"Available options • {index}",
                        value=chunk,
                        inline=False,
                    )
                if len(chunks) > 5:
                    embed.add_field(
                        name="More options",
                        value="Additional options are available in Discord's command interface.",
                        inline=False,
                    )
            embed.set_footer(text=f"{_bot_name(self.bot)} • Use Back to return.")
        else:
            embed.add_field(name="Details", value="No additional details are available.", inline=False)
        return embed

    async def _on_select(self, interaction: discord.Interaction) -> None:
        try:
            index = int(interaction.data["values"][0])
            node = self.current_nodes[index]
        except (KeyError, IndexError, ValueError, TypeError):
            await interaction.response.send_message("That selection is no longer valid. Please reopen `/help`.", ephemeral=True)
            return
        self.path.append(node)
        self.page = 0
        self._build()
        await interaction.response.edit_message(embed=self._current_embed(), view=self)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        if self.path:
            self.path.pop()
        self.page = 0
        self._build()
        await interaction.response.edit_message(embed=self._current_embed(), view=self)

    async def _on_home(self, interaction: discord.Interaction) -> None:
        self.path.clear()
        self.page = 0
        self._build()
        await interaction.response.edit_message(embed=self._current_embed(), view=self)

    async def _on_previous(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._build()
        await interaction.response.edit_message(embed=self._current_embed(), view=self)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        page_size = 21 if not self.path else 25
        max_page = max(0, (len(self.current_nodes) - 1) // page_size)
        self.page = min(max_page, self.page + 1)
        self._build()
        await interaction.response.edit_message(embed=self._current_embed(), view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def setup_help(
    bot: Any,
    *,
    command_name: str = "help",
    ephemeral: bool = True,
    timeout: float = 300,
    replace_existing: bool = True,
    emojis: dict[str, str] | None = None,
) -> app_commands.Command:
    """Register the interactive /help command."""
    if not hasattr(bot, "tree"):
        raise TypeError("setup_help(bot) requires a discord.py bot with an application-command tree.")
    if not command_name or len(command_name) > 32:
        raise ValueError("command_name must contain between 1 and 32 characters.")

    setattr(bot, "_sse_help_explicit_emojis", emojis or {})
    existing = bot.tree.get_command(command_name)
    if existing is not None:
        if not replace_existing:
            raise ValueError(f"The /{command_name} command is already registered.")
        bot.tree.remove_command(command_name)

    async def help_callback(interaction: discord.Interaction) -> None:
        await _ensure_app_emojis(bot)
        view = HelpCenterView(bot, interaction.user.id, timeout=timeout)
        await interaction.response.send_message(
            embed=_base_embed(bot, view.commands, view.page),
            view=view,
            ephemeral=ephemeral,
        )
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            log.debug("Could not retain help-menu message for timeout cleanup.")

    command = app_commands.Command(
        name=command_name,
        description=f"Open the {_bot_name(bot)[:70]} interactive help menu"[:100],
        callback=help_callback,
    )
    bot.tree.add_command(command)
    return command
