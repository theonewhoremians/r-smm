# R-SMM

Run locally with Python 3 using `python app.py`, then open http://127.0.0.1:8000. Keep the server running while using the site. Local mode stores data in SQLite; Vercel mode uses Neon Postgres.

## Vercel database setup

Create a free Neon Postgres project at https://neon.com, then add its pooled connection URL as the private `DATABASE_URL` environment variable in the R-SMM Vercel project. Do not use a `NEXT_PUBLIC_` variable or commit the URL. Add `RSMM_ADMIN_PASSWORD` as a private Vercel environment variable before the first API request, then redeploy. The app creates its tables on the first request. The admin password must be at least 10 characters.

Production starts with an empty database. The local `auth.sqlite3` file is ignored by Git and is not uploaded or copied to Neon.

## Accounts and balances

Each account's sign-in email is its primary key; orders, deposit requests, and wallet transactions are tied to that email. Local balances are in `auth.sqlite3`; production balances are in the linked Neon database. The admin panel lists each account and its current balance.

Three consecutive incorrect passwords lock that email out for six hours. The administrator account is `aryan793gupta@gmail.com`; its password is stored as a salted PBKDF2 hash in the database. For a fresh database or an intentional password reset, set `RSMM_ADMIN_PASSWORD` before starting the server. The value is read only during startup and is not stored in source files.

## Telegram order alerts

Create a bot with Telegram's `@BotFather`, open a private chat with it, and send `/start`. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as private Production environment variables in Vercel, then redeploy. Get the chat ID from the bot's `getUpdates` response after sending `/start`; never share the bot token. The bot sends alerts after a new order is saved and after a deposit is approved. Notification failures do not undo an order or wallet credit, and alerts do not include customer email addresses or transfer references.

## Wallet and orders

The minimum deposit is 2 USDT on TRC20 or 2 USDC on Base. Deposit requests remain pending until an administrator checks the transfer reference and credits the wallet. The app does not monitor blockchains, and balances change only after manual review.

Enabled order items are priced at fixed USDT/USDC rates: views 0.085/1,000, likes 5.21/1,000, comments 0.052 each, saves 0.312/1,000, shares 0.52/1,000, and reposts 2.08/1,000. The server recalculates and deducts the charge atomically when an order is placed. Orders remain pending for an administrator to complete after fulfillment. Admin actions and wallet changes are recorded in `auth.sqlite3`.

This prototype does not connect to a social-service provider.
