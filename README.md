# XE3

XE3 is the academic assistant project in this workspace. It focuses on E3 course access, timelines, reminders, Discord/LINE delivery, and lightweight utility features.

This repository now keeps XE3 code clearly separated from HomeVault projects. XE3 lives in `~/xe3`, while HomeVault-related projects live under `~/homevault`.

## Features

- LINE bot webhook adapter
- E3 login, relogin, logout, status, course, timeline, file browser
- Reminder worker for upcoming events
- Secure Level 1 file proxy for E3 downloads
- Cloudflare quick tunnel support for temporary public access

## Project Layout

```text
apps/
  web/
    main.py
  discord/
    main.py
  line/
    main.py
agent/
  core/
    config.py
    system_status.py
  platforms/
    discord/
      ...
    line/
      app.py
      background.py
      messaging.py
  features/
    e3/
      service.py
      handler.py
      common.py        # compatibility wrapper
      data/
        db.py
        course_runtime.py
        file_catalog.py
        file_proxy.py
      reminder/
        api.py
        payloads.py
        worker.py
      services/
        client.py
        events.py
        secrets.py
      utils/
        common.py
      views/
        course_cards.py
        file_views.py
        timeline_views.py
        payloads.py
      scraper/
        ...
      references/
        har/
        js/
    weather/
      service.py
      data/
        city_data.py
      services/
        geolocation.py
        weather_api.py
scripts/
  line_rich_menu.py
  cloudflared_tunnel.py
  tunnel_watchdog.py
deploy/
  systemd/
data/
```

## Canonical Modules

XE3 is currently in a compatibility period. The new structure above is the canonical one we should read and extend first.

- `agent/core/`
  - shared runtime config and system status helpers
- `agent/features/e3/data/`
  - database, runtime cache, file catalog, file proxy
- `agent/features/e3/services/`
  - E3 fetch, event extraction, secret handling
- `agent/features/e3/reminder/`
  - reminder scheduler, payload building, worker entrypoints
- `agent/features/e3/views/`
  - Flex / payload builders and presentation helpers
- `agent/features/e3/utils/`
  - shared E3 formatting and normalization helpers
- `agent/features/weather/data/` and `agent/features/weather/services/`
  - city lookup and API access

Some old top-level modules still exist as thin wrappers so the app can keep running during refactors. New code should prefer the canonical paths above.

## Setup

1. Create a virtual environment:

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Configure [`.env`](/home/eason/xe3/.env):

   ```env
   PORT=5000
   AUTO_RELOAD=1
   LINE_CHANNEL_SECRET=...
   LINE_CHANNEL_ACCESS_TOKEN=...
   LINE_NOTIFY_USER_ID=...
   PUBLIC_BASE_URL=auto
   FILE_PROXY_SECRET=...
   E3_FILE_PROXY_TTL_SECONDS=300
   E3_FILE_PROXY_MAX_BYTES=26214400
   ```

4. Run:

   ```bash
   python3 apps/web/main.py
   ```

## Common Commands

```text
天氣
天氣 台北
e3 幫助
e3 login <帳號> <密碼>
e3 relogin
e3 logout
e3 狀態
e3 課程
e3 近期
e3 行事曆
e3 檔案 <課名>
e3 remind show
e3 remind on
e3 remind off
```

## LINE Rich Menu

Create and bind the persistent rich menu with:

```bash
/home/eason/xe3/venv/bin/python /home/eason/xe3/scripts/line_rich_menu.py
```

## Temporary Public Tunnel

This project can run a Cloudflare quick tunnel as a user service for testing.

Install and start:

```bash
systemctl --user enable --now cloudflared-tunnel.service
systemctl --user enable --now cloudflared-watchdog.service
```

Useful checks:

```bash
systemctl --user status cloudflared-tunnel.service
systemctl --user status cloudflared-watchdog.service
journalctl --user -u cloudflared-tunnel.service -f
journalctl --user -u cloudflared-watchdog.service -f
cat /home/eason/xe3/data/cloudflared/current_url
```

Important:

- `PUBLIC_BASE_URL=auto` lets the app read the current tunnel URL from `data/cloudflared/current_url`
- the watchdog sends LINE alerts when the tunnel goes down or recovers
- quick tunnel URLs can change after restart

If you want these user services to stay up even when you are not actively logged in, enable linger once:

```bash
sudo loginctl enable-linger eason
```

You can verify it with:

```bash
loginctl show-user eason | grep Linger
```

## Notes

- The file proxy only works when users can reach your public URL.
- For small-scale testing, the Cloudflare quick tunnel is enough.
- For a stable long-term setup, move to a fixed public domain or named tunnel.

## Discord Bot

Discord support is scaffolded in the repo with a separate entrypoint:

```bash
/home/eason/xe3/venv/bin/python /home/eason/xe3/apps/discord/main.py
```

Environment variables:

```env
DISCORD_BOT_TOKEN=your_bot_token
DISCORD_COMMAND_PREFIX=!
# optional, only needed if you later add guild-scoped slash command sync
DISCORD_GUILD_ID=
# optional, for !chksys / system report naming
APP_SERVICE_NAME=discord-bot.service
```

Current Discord commands:

```text
!homevault
!help
!weather <city>
!chksys
!e3 help
!e3 login <account> <password>
!e3 relogin
!e3 course
!e3 timeline
!e3 grades
!e3 files <keyword>
```

The Discord bot currently reuses the existing E3 and weather core logic and sends the text fallback for responses.

### Discord Service

Systemd template:

```text
deploy/systemd/discord-bot.service
```

Install and start:

```bash
mkdir -p ~/.config/systemd/user
cp /home/eason/xe3/deploy/systemd/discord-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now discord-bot.service
```

Useful checks:

```bash
systemctl --user status discord-bot.service
journalctl --user -u discord-bot.service -f
```
