"""Async TDX (Taiwan transport data) client shared by THSR and TRA."""
import asyncio
import time

import aiohttp

import config
from core import UserFacingError, http

TOKEN_URL = 'https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token'
API_BASE = 'https://tdx.transportdata.tw/api/basic'


class TDXError(UserFacingError):
    pass




_token: str | None = None
_token_expiry = 0.0
_lock = asyncio.Lock()


async def _get_token() -> str:
    global _token, _token_expiry
    async with _lock:
        if _token and time.time() < _token_expiry:
            return _token
        data = {'grant_type': 'client_credentials',
                'client_id': config.TDX_ID, 'client_secret': config.TDX_SECRET}
        result = await http.post_json(TOKEN_URL, data=data)
        _token = result['access_token']
        # Refresh a few minutes before the token (valid for 1 day) actually expires.
        _token_expiry = time.time() + result.get('expires_in', 86400) - 300
        return _token


# The free TDX tier allows only 5 API calls per minute, so responses are cached.
_cache: dict[str, tuple[float, object]] = {}
MAX_WAIT = 20  # seconds we are willing to wait out a rate limit before giving up


async def get(path: str, ttl: float = 0, **params):
    """GET an API path such as '/v2/Rail/THSR/ODFare/1000/to/1070'.

    `ttl` caches the response for that many seconds. Waits out short rate limits.
    """
    global _token
    params.setdefault('$format', 'JSON')
    key = path + '?' + '&'.join(f'{k}={v}' for k, v in sorted(params.items()))
    cached = _cache.get(key)
    if cached and time.time() < cached[0]:
        return cached[1]
    for _ in range(3):
        headers = {'authorization': f'Bearer {await _get_token()}'}
        async with http.session.get(API_BASE + path, headers=headers, params=params) as resp:
            if resp.status == 429:
                wait = int(resp.headers.get('Retry-After', 10))
                if wait > MAX_WAIT:
                    raise TDXError(f'交通部 TDX 每分鐘只能查詢幾次，請 {wait} 秒後再試。')
                await asyncio.sleep(wait + 0.5)
                continue
            if resp.status == 401:
                _token = None
                continue
            try:
                resp.raise_for_status()
            except aiohttp.ClientResponseError as e:
                raise TDXError(f'交通部 TDX 回應錯誤（HTTP {e.status}），請稍後再試。') from e
            data = await resp.json(content_type=None)
        if ttl:
            _cache[key] = (time.time() + ttl, data)
            if len(_cache) > 500:
                now = time.time()
                for k in [k for k, (expiry, _) in _cache.items() if expiry < now]:
                    del _cache[k]
        return data
    raise TDXError('交通部 TDX 查詢太頻繁，請一分鐘後再試。')
