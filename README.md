# Panel Key Manager Bot

A Telegram bot that manages and distributes 24-hour panel keys to authorised users.

## Features

| Role | Commands |
|------|----------|
| Anyone | `/start`, `/request` |
| Authorised users | `/getkey`, `/mykey` |
| Admin only | `/addkey`, `/keys`, `/used`, `/members`, `/removeuser`, `/broadcast`, `/stats`, `/revoke` |

## Setup

### Local development

```bash
pip install -r requirements.txt
mkdir -p data
python bot.py
```

### Deploy to Render (background worker)

1. Push this repo to GitHub.
2. In the [Render dashboard](https://render.com), create a new **Background Worker**.
3. Connect your GitHub repo.
4. Add a **Disk** (under the service's "Disks" section):
   - Mount path: `/opt/render/project/src/data`
   - Size: 1 GB (free tier supports this)
5. Set the build command: `pip install -r requirements.txt`
6. Set the start command: `python bot.py`
7. Deploy — the bot will start polling immediately.

The `data/bot.db` SQLite file lives on the mounted disk and persists across redeploys.

---

## Command reference

### User commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message; shows authorisation status |
| `/request` | Send an access request to the admin |
| `/getkey` | Get your current active key (or request a new one) |
| `/mykey` | View your active key + remaining time |

### Admin commands

| Command | Description |
|---------|-------------|
| `/addkey k1 k2 …` | Add one or more keys to stock |
| `/keys` | List all unassigned keys + remaining lifetime |
| `/used` | Show keys given out today |
| `/members` | List all authorised users |
| `/removeuser <id>` | Remove user and revoke their key |
| `/broadcast <msg>` | Send a message to all authorised users |
| `/stats` | Overview: total added / remaining / used today |
| `/revoke <id>` | Revoke a user's active key without removing them |

---

## Key lifecycle

```
Admin /addkey "MYKEY123"
        │
        ▼
  keys table (added_time = now)
        │
  User /getkey
        │
        ▼  oldest unexpired key assigned
  used_keys table  ──  key deleted from keys table
        │
  Key expires 24 h after added_time (not assigned_time)
```

- A key is valid for **24 hours from when the admin added it**.
- Once assigned to a user it is **removed from stock** (one key → one user).
- If the user already has an active key, `/getkey` returns the same key + remaining time.
