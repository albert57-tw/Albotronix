# Albotronix

A Discord bot for a CTF / competitive-programming crew: Taiwan transport, CTF & contest reminders,
security news, weather, polls, reminders and Gemini chat.

## Setup

1. **Python 3.11+**
2. Install dependencies in a virtual environment:
   ```sh
   python -m venv .venv
   .venv/Scripts/activate        # Windows  (Linux/macOS: source .venv/bin/activate)
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill it in. Anything optional left blank just turns that feature off.
4. Run it: `python bot.py`

To run it 24/7 on a Linux server, `deploy/albotronix.service` is a systemd unit for it. Logs are written to
`logs/albotronix.log` (5 MB per file, 5 old files kept).

### Keys to apply for

| Setting | Used by | Where |
|---|---|---|
| `TOKEN`, `GUILD_ID` | everything | [Discord Developer Portal](https://discord.com/developers/applications) |
| `TDX_ID`, `TDX_SECRET` | `/thsr`, `/tra` | [TDX](https://tdx.transportdata.tw) (free) |
| `CWA_KEY` | `/weather`, earthquake alerts | [CWA Open Data](https://opendata.cwa.gov.tw) (free) |
| `GEMINI_API_KEY` | `/ai`, @mention | [Google AI Studio](https://aistudio.google.com/apikey) (free tier) |
| `NVD_API_KEY` (optional) | faster `/info cve` | [NVD](https://nvd.nist.gov/developers/request-an-api-key) |

### Bot permissions

Do **not** give the bot Administrator — if its token leaks, the whole server goes with it. Give its role:
View Channels, Send Messages, Send Messages in Threads, Embed Links, Attach Files, Read Message History,
Add Reactions, Create Public Threads, Manage Threads, **Manage Roles** (for `/notify`),
**Mention @everyone, @here and All Roles** (to ping the notification roles), and Manage Channels if you use
`/ctf start`. Keep the bot's role *above* the notification roles it creates, or it cannot assign them.

`/ai summarize` reads channel history, which needs the privileged **Message Content Intent**
(Developer Portal → Bot → Privileged Gateway Intents), then set `MESSAGE_CONTENT_INTENT=1`.
Replying to @mentions works without it. Note that `/ai summarize` sends those messages to Google Gemini.

### Who gets notified

Pushes are posted in their channel without pinging anyone, except the "starts in 1 hour" reminders: those
ping members who opted in. `/notify settings` lets anyone pick topics privately, and `/notify panel` (needs
Manage Roles) posts a public button panel. Topics: CTF and competitive programming (Codeforces / AtCoder)
start reminders. Each topic's role is
created the first time someone subscribes; the CTF topic reuses `CTF_PING_ROLE_ID` if it is set.

## Commands

Commands are grouped so Discord's command picker shows one entry per category:

| Category | Commands |
|---|---|
| Help | `/help` |
| Fun | `/fun choose\|chance\|inspire\|ping` |
| THSR / TRA | `/thsr fare\|status\|train\|board\|alert`, `/tra timetable\|train\|board\|fare\|alert` |
| Metro | `/metro board\|route\|firstlast\|alert` (Taipei, New Taipei, Kaohsiung, Taoyuan Airport, Taichung) |
| Bus | `/bus stop` |
| Weather | `/weather forecast\|quake` |
| CTF & contests | `/ctf upcoming\|team\|start\|chal\|solved`, `/contest upcoming\|leetcode\|cses` |
| Info | `/info rate\|oil\|cve` |
| Reminders & polls | `/remind set\|list\|cancel\|timer`, `/poll` |
| AI | `/ai ask\|reset\|summarize`, @Albotronix |
| Notifications | `/notify settings\|panel` |

## Scheduled posts (Taiwan time, configurable in `.env`)

| When | What | Channel setting |
|---|---|---|
| 00:00 | CTFs in the next 3 days | `CTF_CHANNEL_ID` |
| 07:00 | Weather forecast for `WEATHER_CITY` | `WEATHER_CHANNEL_ID` |
| 08:05 | LeetCode daily challenge | `CONTEST_CHANNEL_ID` |
| 08:10 | CSES daily problem with study hints | `CSES_CHANNEL_ID` (default: contest channel) |
| 09:00 | New CISA KEV entries + critical CVEs | `SECURITY_CHANNEL_ID` |
| Sunday from 13:00 | Next week's CPC fuel prices (as soon as they're announced) | `OIL_CHANNEL_ID` |
| 1 h before start | CTF / Codeforces / AtCoder reminder | CTF / contest channel |
| every minute | New significant earthquake report | `QUAKE_CHANNEL_ID` |
| every 5 min | THSR / TRA / metro service disruptions | `RAIL_ALERT_CHANNEL_ID` |

## Notes

- Images in `assets/emojis/<name>.png` are uploaded as the bot's application emojis on startup and used from
  Discord's CDN (train / line badges, operator logos, the footer avatar). The folder is not in this repository;
  without it the bot still works, showing text instead of badges and no logo thumbnails. Only new file names
  are uploaded: to change an existing image, delete that emoji in the Developer Portal (Emojis page) first,
  then restart the bot.
- The THSR, TRA, metro operator (Taipei, New Taipei, Taoyuan, Taichung, Kaohsiung), CWA and LeetCode logos
  are trademarks of their respective owners and are used only to label
  information from those services.

- Data (reminders, polls, playlists, contest list) lives in `data/albotronix.db` (SQLite).
- TDX's free tier allows only 5 calls per minute, so THSR/TRA answers are cached (seats 2–5 min,
  timetables 1 h, fares 1 day). When many people query at once the bot asks them to retry in a few seconds.
- `/info rate` uses daily mid-market rates from ExchangeRate-API: the Bank of Taiwan site blocks bots.
- AtCoder has no API, so contest times are read from its website; if they change the page,
  only AtCoder reminders stop (check the log).
- Earthquake alerts come from CWA's *significant earthquake reports*, which are published a few
  minutes after a quake — they are not early warnings.
