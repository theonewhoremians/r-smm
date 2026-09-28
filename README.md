# R-SMM

Run locally with Python 3 using `python app.py`, then open http://127.0.0.1:8000. Keep the server running while using the site.

## Accounts and balances

Customer accounts and wallet balances are stored in the local SQLite database, `auth.sqlite3`. Each account's sign-in email is its primary key; orders, deposit requests, and wallet transactions are tied to that email. Balances persist across server restarts on this machine. The admin panel lists each account and its current balance.

Three consecutive incorrect passwords lock that email out for six hours. The administrator account is `aryan793gupta@gmail.com`; its password is stored as a salted PBKDF2 hash in the database. For a fresh database or an intentional password reset, set `RSMM_ADMIN_PASSWORD` before starting the server. The value is read only during startup and is not stored in source files.

## Wallet and orders

The minimum deposit is 2 USDT on TRC20 or 2 USDC on Base. Deposit requests remain pending until an administrator checks the transfer reference and credits the wallet. The app does not monitor blockchains, and balances change only after manual review.

Enabled order items are priced at fixed USDT/USDC rates: views 0.085/1,000, likes 5.21/1,000, comments 0.052 each, saves 0.312/1,000, shares 0.52/1,000, and reposts 2.08/1,000. The server recalculates and deducts the charge atomically when an order is placed. Orders remain pending for an administrator to complete after fulfillment. Admin actions and wallet changes are recorded in `auth.sqlite3`.

This prototype does not connect to a social-service provider.
