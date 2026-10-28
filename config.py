"""All settings come from .env (see .env.example). Optional values left blank disable that feature."""
import datetime
import os

from dotenv import load_dotenv

load_dotenv()

TZ = datetime.timezone(datetime.timedelta(hours=8))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _str(name: str, default: str = '') -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int | None = None) -> int | None:
    value = _str(name)
    return int(value) if value else default


def _bool(name: str, default: bool = False) -> bool:
    value = _str(name).lower()
    return value in ('1', 'true', 'yes', 'on') if value else default


def _path(name: str, default: str = '') -> str:
    """Relative paths are relative to the project folder, not wherever the bot was started from."""
    value = _str(name, default)
    return os.path.join(BASE_DIR, value) if value and not os.path.isabs(value) else value


def _time(name: str, default: str) -> datetime.time:
    hour, minute = _str(name, default).split(':')
    return datetime.time(hour=int(hour), minute=int(minute), tzinfo=TZ)


# Discord
TOKEN = _str('TOKEN')
GUILD_ID = _int('GUILD_ID')
# Needed only by /summarize; must also be enabled in the Developer Portal or login fails.
MESSAGE_CONTENT_INTENT = _bool('MESSAGE_CONTENT_INTENT')

# Branding used by every embed
BOT_NAME = 'Albotronix'
FOOTER_TEXT = 'M3t30r'
EMBED_COLOR = 0x00FFAA

# Data
DB_PATH = _path('DB_PATH', 'data/albotronix.db')
LOG_DIR = _path('LOG_DIR', 'logs')

# TDX (THSR + TRA)
TDX_ID = _str('TDX_ID')
TDX_SECRET = _str('TDX_SECRET')
RAIL_ALERT_CHANNEL_ID = _int('RAIL_ALERT_CHANNEL_ID')
RAIL_ALERT_MINUTES = _int('RAIL_ALERT_MINUTES', 5)

# CTF / contests
CTF_CHANNEL_ID = _int('CTF_CHANNEL_ID')
CTF_PING_ROLE_ID = _int('CTF_PING_ROLE_ID')
CTF_NOTIFY_TIME = _time('CTF_NOTIFY_TIME', '00:00')
# Only CTFtime events with at least this weight are listed, posted and reminded (0 = all of them).
CTF_MIN_WEIGHT = float(_str('CTF_MIN_WEIGHT') or _str('CTF_REMIND_MIN_WEIGHT', '25'))
CONTEST_CHANNEL_ID = _int('CONTEST_CHANNEL_ID')
CONTEST_REFRESH_TIME = _time('CONTEST_REFRESH_TIME', '08:00')
LEETCODE_TIME = _time('LEETCODE_TIME', '08:05')
# CSES daily problem (in CSES order, one a day); defaults to the contest channel
CSES_CHANNEL_ID = _int('CSES_CHANNEL_ID') or CONTEST_CHANNEL_ID
CSES_TIME = _time('CSES_TIME', '08:10')
EVENT_REMIND_MINUTES = _int('EVENT_REMIND_MINUTES', 60)

# Fuel prices: CPC announces next week's prices on Sunday around noon
OIL_CHANNEL_ID = _int('OIL_CHANNEL_ID')
OIL_TIME = _time('OIL_TIME', '13:00')  # Sunday, from this time on

# Security
SECURITY_CHANNEL_ID = _int('SECURITY_CHANNEL_ID')
SECURITY_TIME = _time('SECURITY_TIME', '09:00')
NVD_API_KEY = _str('NVD_API_KEY')

# Weather (Central Weather Administration)
CWA_KEY = _str('CWA_KEY')
WEATHER_CHANNEL_ID = _int('WEATHER_CHANNEL_ID')
# CWA only knows the official spelling (臺北市, not 台北市), so accept either.
WEATHER_CITY = _str('WEATHER_CITY', '臺南市').replace('台', '臺')
WEATHER_TIME = _time('WEATHER_TIME', '07:00')
QUAKE_CHANNEL_ID = _int('QUAKE_CHANNEL_ID') or WEATHER_CHANNEL_ID

# Gemini
GEMINI_API_KEY = _str('GEMINI_API_KEY')
GEMINI_MODEL = _str('GEMINI_MODEL', 'gemini-3.8-flash')
AI_HISTORY_TURNS = _int('AI_HISTORY_TURNS', 10)
AI_RATE_LIMIT = _int('AI_RATE_LIMIT', 5)  # requests per user per minute

# Fun: auto-react to one user's messages (the old "electric god" feature)
REACT_USER_ID = _int('REACT_USER_ID')
REACT_EMOJI = _str('REACT_EMOJI', '⚡')
