# stock-watcher

Polls one or more shop category/tag pages every 5 minutes via GitHub Actions and
sends a Telegram message the moment an item whose name matches a wanted pattern
flips to **In Stock**.

All targeting (which shop, which pages, which names) is supplied through the
environment, so the target is not disclosed in this source. Stdlib-only Python.
No server, no dependencies.

## Configuration

Set these as repo secrets in CI, and in a local `.env` (gitignored) for local
runs. See `.env.example`.

| Variable | Meaning |
|---|---|
| `WATCH_URLS` | Pages to check. Newline- or comma-separated. |
| `WATCH_SELF_TEST_URLS` | Page(s) scraped by `--self-test` (something usually in stock). |
| `WATCH_HOMEPAGE` | Hit first to pick up any CDN clearance cookie. Defaults to scheme+host of the first `WATCH_URLS` entry. |
| `WANTED_PATTERN` | Regex (IGNORECASE) matched against product names. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram delivery. |

## Setup

### 1. Make the Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`, pick a name and a
   username. It replies with a token like `8123456789:AAH...`.
2. Send your new bot any message (e.g. `hi`) so it's allowed to message you back.
3. Get your chat ID:

   ```sh
   curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"][-1]["message"]["chat"]["id"])'
   ```

4. Test it locally:

   ```sh
   cp .env.example .env      # then fill in real values
   python3 stock_watcher.py
   ```

   (It only pushes when something wanted is in stock. To force a test message,
   run `python3 stock_watcher.py --self-test`.)

### 2. Push and add secrets

```sh
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
gh secret set WATCH_URLS
gh secret set WATCH_SELF_TEST_URLS
gh secret set WATCH_HOMEPAGE
gh secret set WANTED_PATTERN
gh workflow run stock-watch     # verify a manual run passes
```

**Public repo is deliberate.** GitHub Actions minutes are unlimited on public
repos; a private repo only gets 2,000 minutes/month and a 5-minute cron burns
~8,600. If you want it private, drop the cron to `*/30 * * * *` or slower.
Secrets are not exposed by making the repo public.

## Verifying it works

Don't wait for a real restock to find out. `--self-test` scrapes the
`WATCH_SELF_TEST_URLS` page (which should always have *something* in stock) and
pushes a real alert through the real notification path:

```sh
gh workflow run stock-watch -f self_test=true    # on GitHub, using the secrets
python3 stock_watcher.py --self-test             # locally
```

A green self-test run proves delivery, not just execution: if no notification
channel works, `notify()` raises and the run fails. Re-run it after rotating
the bot token.

What that covers, and what it doesn't:

| Link in the chain | Covered by |
|---|---|
| Fetch + parse from the runner | self-test, scheduled runs |
| Detecting `in-stock` markup | self-test (real in-stock products) |
| Matching wanted names | normal run — wanted variants print `*wanted*` |
| Push delivery to your phone | self-test (fails loudly if undelivered) |
| Broken scraper is loud | exits 1 → workflow fails → GitHub emails you |

The one thing never exercised end-to-end is a genuine restock, since it requires
stock that doesn't exist yet — but it's just the two verified halves (wanted-name
matching AND in-stock detection) meeting.

## Operational notes

- **Cron drift.** Scheduled workflows fire on a best-effort basis and can be
  10–15 minutes late under load. Fine for a restock; not fine for a race.
- **60-day auto-disable.** GitHub disables scheduled workflows in repos with no
  activity for 60 days (it emails you first). Any commit, or a manual
  `gh workflow run`, resets the clock.
- **Alerts repeat.** There's no state between runs, so once a wanted variant is
  in stock you get a ping every 5 minutes until it sells out. That's the
  intended behaviour for a hard-to-get item — it will wake you up.
- **Broken scraper is loud.** The regex parse is brittle by nature. If the
  markup changes, the script exits 1 and the workflow fails, and GitHub emails
  you about failed scheduled runs. Silence means "no stock", not "no idea".
