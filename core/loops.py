import asyncio
import functools
import logging

log = logging.getLogger('albotronix.tasks')


def guarded(func):
    """Put under @tasks.loop: log any error and keep the loop alive.

    discord.ext.tasks only retries network errors; anything else (a changed API, a channel the
    bot can no longer post in…) would stop the loop for good until the bot restarts.
    """
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Background task %s failed; will run again next time', func.__qualname__)
    return wrapper
