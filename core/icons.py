"""The bot's application emojis (Developer Portal → Emojis).

Images live in assets/emojis/<name>.png. On startup sync() uploads any that the application doesn't
have yet, then get(name) returns the emoji markup ('<:name:id>') — or a text fallback if it's missing,
so nothing breaks before the upload or if it fails.
"""
import logging
import os

import discord

import config

log = logging.getLogger(__name__)

EMOJI_DIR = os.path.join(config.BASE_DIR, 'assets', 'emojis')
_emojis: dict[str, str] = {}
_urls: dict[str, str] = {}


async def sync(client: discord.Client) -> None:
    global _emojis, _urls
    existing = {e.name: e for e in await client.fetch_application_emojis()}
    if os.path.isdir(EMOJI_DIR):
        for file in sorted(os.listdir(EMOJI_DIR)):
            name, ext = os.path.splitext(file)
            if ext.lower() != '.png' or name in existing:
                continue
            with open(os.path.join(EMOJI_DIR, file), 'rb') as f:
                existing[name] = await client.create_application_emoji(name=name, image=f.read())
            log.info('Uploaded application emoji :%s:', name)
    _emojis = {name: str(emoji) for name, emoji in existing.items()}
    _urls = {name: str(emoji.url) for name, emoji in existing.items()}


def get(name: str, fallback: str = '') -> str:
    return _emojis.get(name, fallback)


def url(name: str) -> str | None:
    """Discord CDN URL of an asset image, for embed thumbnails and footer icons (None if not uploaded)."""
    return _urls.get(name)
