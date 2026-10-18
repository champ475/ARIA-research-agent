# Setting up price alerts

Alerts are delivered through Telegram. One bot serves every user, and each person links their own chat inside the app. As the person running the instance, you set the bot up once. This is optional: without it everything else works.

## 1. Create the bot (once)

1. Message [@BotFather](https://t.me/BotFather) on Telegram and send `/newbot`.
2. Follow the prompts. Copy the token into `TELEGRAM_BOT_TOKEN` and the username, without the `@`, into `TELEGRAM_BOT_USERNAME` in your `.env`.
3. Restart the app and worker.

## 2. Run the checks

Alerts are evaluated every 5 minutes during NSE market hours (9:15 AM to 3:30 PM IST, weekdays). Pick one:

- **The worker (recommended).** `python worker.py`, or the `worker` service that `docker compose up` already starts. Keep it running somewhere that stays on.
- **GitHub Actions.** Useful when the app is on a host that sleeps, or you don't run a worker. It runs whether or not your machine or the app is awake.
  1. Push the repo to a public GitHub repo (public keeps Actions free at this frequency).
  2. In *Settings → Secrets and variables → Actions*, add `DATABASE_URL` and `TELEGRAM_BOT_TOKEN`, using the same values as your `.env`.
  3. The `alert-check` workflow then runs on its own. Use *Run workflow* to test it by hand.

## 3. Each user links their chat

No work for you here. In the app, a user opens **Settings → Telegram Alerts** and clicks **Connect Telegram**. That opens your bot and links the chat to their account. Their alerts go to that chat, and only that chat.

If Telegram delivery fails repeatedly, the alert lands in the user's in-app notification inbox instead.

## Operator alerts

You can also get ops alerts (job failures, database size, and so on) on your own Telegram. Set `OPERATOR_TELEGRAM_CHAT_ID` in `.env`; `.env.example` explains how to find your chat id. What to do for each alert is in [RUNBOOK.md](RUNBOOK.md).
