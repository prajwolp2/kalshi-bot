# Running the bot on a server with real orders

This runs the v0.4.2 rules on a small Linux server, 24/7, placing real orders on your Kalshi
account. Every step that touches your account or money is one **you** do. The code never asks
for your password, and nobody else needs your key.

How it trades:

- It uses the paper bot's rules, with the size based on your real cash: 5 contracts
  per $100, at least 10 (v0.4.2). With $450 that's 22 contracts; with $1,000, 50.
- One difference from the paper bot: a trigger must stay on, same side on every look, for
  **4 seconds** before it becomes an order (`--hold 4`, the default). The log shows
  `trigger: ... ordering if it holds 4s`, then either `ACT ... held 4s` or `trigger gone ...: no order`.
  Add `--hold 0` to `KALSHI_RUN_FLAGS` to order on the first look, as the paper bot does.
- Each decision becomes one **immediate-or-cancel** buy: the shown ask plus up to 1¢. Anything that
  doesn't fill at once is cancelled. Nothing is left resting, and it never sells.
- Contracts are held to settlement. Kalshi pays winners automatically.

Hard limits (change them in `secrets/kalshi.env`):

The order cap and the daily stop follow the account: they're a share of it, so they grow and shrink
with your balance. You can add a fixed dollar cap on top; then the tighter of the two applies.

| Limit | Default | What happens |
|---|---|---|
| `--max-order-pct` | 25 | No single order costs more than this % of equity (fees included). 0 = off |
| `--daily-loss-pct` | 30 | Once **this bot's own** trades today have lost this % of the day's starting equity, no more orders until the next UTC day (7 pm ET). 0 = off |
| `--max-order` | none | Optional: also cap one order at this many dollars |
| `--daily-loss` | none | Optional: also stop for the day after losing this many dollars |
| `--stop-below` | $100 | **HALT** when the account's equity falls below this: no orders until you run `deploy/kb resume` |
| `--max-orders-per-day` | 60 | The busiest backtest month averaged ~27 a day |

**Sharing the account with the gold bot** (`~/gold-bot`, `deploy/gk`):
- The daily loss limit counts only this bot's own orders, from Kalshi's published results. A gold
  loss never stops BTC for the day.
- Equity, for the floor, counts every open position on the account, so money the gold bot has tied
  up in a trade doesn't look like a BTC loss.
- The gold bot works the same way the other way round.

It also writes HALT by itself in these cases:

- Kalshi records an order differently than intended (market, side, price or cost).
- Kalshi rejects the key.
- Anything unexpected happens while placing an order.

## 1. Kalshi: a new API key

1. **Revoke the key you pasted into the chat earlier**: kalshi.com → Account & security → API keys
   → delete it. Treat that key as exposed.
2. Create a new key there and download the private key file. Kalshi shows it **once**; keep the
   file and don't paste it anywhere. Note the key ID next to it.
3. For the practice round, make a demo account at https://demo.kalshi.co (mock money) and create a
   demo API key the same way. Demo and real keys are separate.
4. Deposit your $200 on kalshi.com when you're ready for step 8. It isn't needed before then.
5. **Move that money to the Crypto exchange shard.** Kalshi holds cash per shard, and crypto markets
   trade on shard 2. The website moves money between shards for its own orders, but API orders
   only use the cash already on the market's shard.
   - kalshi.com/account/exchange-indexes shows the split. If it has no transfer button, run this on
     the server (it asks you to type yes first):
     `deploy/kb fund-shard --amount 200`
   - `deploy/kb status` shows your cash by shard.
   - The smoke test fails if shard 2 can't pay for one trade.

## 2. A server

Any small Ubuntu 24.04 server will do: 1 vCPU and 1 GB RAM is plenty, about $5–6 a month at
DigitalOcean, Hetzner, Vultr or AWS Lightsail.

- Pick a **US region** (e.g. New York or Virginia).
- Sign in with an SSH key rather than a password.
- When it's created, note its IP address.

## 3. Copy the code (from your Mac)

This copies the code only. Market data (`data/`) stays on the Mac.

```bash
rsync -av --exclude data --exclude .venv --exclude __pycache__ --exclude secrets ~/"Desktop/Claude Projects/kalshi-bot/" root@SERVER_IP:~/kalshi-bot/
```

Replace `SERVER_IP`, and `root` if your provider gives you a different user. Run the same command
again later to update the code, then restart the service (step 9).

## 4. Set up the server

```bash
ssh root@SERVER_IP
```

```bash
cd ~/kalshi-bot && bash deploy/setup.sh
```

This installs Python and the signing library, then runs the tests; the signing test runs here.
It also creates `secrets/kalshi.env` and installs the `kalshi-live` service without starting it.

## 5. Put the demo key on the server

From your Mac (change the file name to the one you downloaded):

```bash
scp ~/Downloads/your-demo-key.key root@SERVER_IP:~/kalshi-bot/secrets/kalshi.key
```

On the server:

```bash
chmod 600 ~/kalshi-bot/secrets/kalshi.key && nano ~/kalshi-bot/secrets/kalshi.env
```

Set `KALSHI_KEY_ID` to the demo key's ID and leave `KALSHI_ENV=demo`.

## 6. Practice on the demo exchange

```bash
~/kalshi-bot/deploy/kb smoke
```

The smoke test:

- Checks the key by reading your demo balance.
- Places a 1-contract BUY YES and a 1-contract BUY NO at **1¢**, checks that Kalshi recorded the
  right side and price, then cancels both.

It must say PASSED. If it says there's no open KXBTC15M market, the demo exchange may not list this
series: skip the rest of this step and go to step 7, whose smoke test does the same 1¢ check on
your real account.

Then start the service on demo:

1. Clear `KALSHI_RUN_FLAGS=` in `secrets/kalshi.env` so it sends demo orders.
2. Start it:

```bash
sudo systemctl enable --now kalshi-live && ~/kalshi-bot/deploy/kb logs
```

You'll see one status line a minute. Trading starts after ~30 minutes of warm-up. On demo:

- Market data comes from the real exchange, but demo order books differ, so orders often won't
  fill. That's fine: this round tests the plumbing.
- Let it run a day.
- Check `deploy/kb status` for orders `sent` without `error` or `HALT`.

## 7. Switch to your real account, dry run first

Stop the demo service:

```bash
sudo systemctl stop kalshi-live
```

Copy the real key to the server (from the Mac):

```bash
scp ~/Downloads/your-real-key.key root@SERVER_IP:~/kalshi-bot/secrets/kalshi.key
```

Then on the server:

1. Run `chmod 600 ~/kalshi-bot/secrets/kalshi.key`.
2. In `secrets/kalshi.env`, set `KALSHI_ENV=live`, `KALSHI_KEY_ID=<real key ID>` and
   `KALSHI_RUN_FLAGS=--dry-run`.
3. Run the smoke test against your real account:

```bash
~/kalshi-bot/deploy/kb smoke live
```

It places two 1-contract orders at 1¢ and cancels them. If one fills by chance, it costs about a
cent. It must say PASSED; otherwise real orders won't start. Then start the dry run:

```bash
sudo systemctl start kalshi-live && ~/kalshi-bot/deploy/kb logs
```

It logs `DRY RUN, not sent: BUY ...` for every order it would place. Let it run a few hours, then
compare those lines with the paper bot on your Mac.

## 8. Real orders

Once you've deposited the $200 on kalshi.com, edit `secrets/kalshi.env`:

- Delete `--dry-run` from `KALSHI_RUN_FLAGS`.
- Set `KALSHI_REAL_MONEY=yes`.

Then restart:

```bash
sudo systemctl restart kalshi-live && ~/kalshi-bot/deploy/kb logs
```

The first lines say `REAL MONEY`. Each order logs `ORDER FILLED` / `ORDER NOT_FILLED`.

## Live dashboard

The page shows your real account only:

- cash, settled P&L and win rate
- the current window: the model vs Kalshi's prices
- every order with its fill price, fees and result
- what the limits blocked

It runs on the server as the `kalshi-dash` service. It only listens on the server itself, so it
isn't on the internet; you open it from your Mac through an SSH tunnel.

One-time setup on the server. Start it now and at every boot:

```bash
systemctl enable --now kalshi-dash
```

To view it, run this on your **Mac** and leave that Terminal window open:

```bash
ssh -N -L 8766:127.0.0.1:8766 root@SERVER_IP
```

The window shows nothing while the tunnel is open. That's normal. Then browse to
http://localhost:8766. Close the tunnel with Ctrl+C; the dashboard and the bot keep running.

## 9. Day to day (on the server)

| What | Command |
|---|---|
| Cash, recent orders, HALT state | `~/kalshi-bot/deploy/kb status` |
| Follow the log | `~/kalshi-bot/deploy/kb logs` |
| Stop placing orders now (keeps watching) | `~/kalshi-bot/deploy/kb halt` |
| Allow orders again | `~/kalshi-bot/deploy/kb resume` |
| Stop the bot completely | `sudo systemctl stop kalshi-live` |
| After updating the code (step 3) | `sudo systemctl restart kalshi-live` |

To stop everything from anywhere, revoke the API key on kalshi.com. The bot can't place another
order after that, and it writes HALT when Kalshi rejects the key.

Files on the server, all in `data/trading-<env>/`:

- `orders-<env>.jsonl`: every order attempt and its result.
- `decisions-v0.4.0.jsonl`: every ACT, every look while a trigger is being held, plus one sampled
  decision a minute.
- `HALT`: present while halted, with the reason.

## What this has and hasn't been tested against

- **Tested:** 27 tests against a fake exchange (order mapping, every limit, read-back checks,
  restarts, no double orders), plus the 87 paper tests.
- **Built from Kalshi's docs and not yet run against Kalshi:** request signing and the
  Create Order V2 request. Steps 6–7 are the first time they meet the real API. The smoke test
  exists so a mistake there shows up on 1¢ orders, not real ones.
- **The backtest:** it assumes fills at the shown price. One cent worse on every fill took the
  past year's result from +$7,550 to about break-even (from $200 + $50/month). Real fills decide
  whether this makes money.
