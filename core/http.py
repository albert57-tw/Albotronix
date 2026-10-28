"""One shared aiohttp session for the whole bot (opened in setup_hook, closed on shutdown)."""
import ssl

import aiohttp

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/141.0 Safari/537.36')

# Several Taiwanese government sites (TDX, CWA, CPC) use certificates without a Subject Key Identifier,
# which Python 3.13+ rejects under its default VERIFY_X509_STRICT flag. Everything else stays verified
# as usual (trusted chain, hostname, expiry); only that extra strictness is turned off.
SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.verify_flags &= ~ssl.VERIFY_X509_STRICT

session: aiohttp.ClientSession | None = None


async def start() -> None:
    global session
    session = aiohttp.ClientSession(headers={'User-Agent': USER_AGENT},
                                    timeout=aiohttp.ClientTimeout(total=30),
                                    connector=aiohttp.TCPConnector(ssl=SSL_CONTEXT))


async def close() -> None:
    if session:
        await session.close()


async def get_json(url: str, **kwargs):
    async with session.get(url, **kwargs) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


async def get_text(url: str, **kwargs) -> str:
    async with session.get(url, **kwargs) as resp:
        resp.raise_for_status()
        return await resp.text()


async def post_json(url: str, **kwargs):
    async with session.post(url, **kwargs) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)
