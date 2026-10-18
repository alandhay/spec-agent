# Card payments (prototype, `payments` branch)

Top-ups on the hosted workbench can take real card payments through Stripe Checkout. Without Stripe settings nothing changes: a top-up is simply granted, as before.

## How it works

1. **Top up** on the page asks the server for a payment. The server creates a Stripe Checkout Session for that amount, tagged with the person's id. The page then goes to Stripe's own payment page, so card details never touch this server.
2. The person pays (or cancels), and Stripe sends them back to the workbench (`/?paid=...`). That return only shows a message. It grants nothing, because anyone could type that address.
3. Stripe also tells the server directly with a **signed webhook** (`POST /api/stripe/webhook`). The server checks the signature with the webhook secret and rejects signatures more than 5 minutes old. Only a `checkout.session.completed` event for a paid session adds credit, and it adds the amount that was sold, not the total including any tax. Each session is credited once (`payments.json` in the person's folder), however often Stripe repeats the event.
4. The budget on the page goes up as soon as that happens. The admin page shows each payment in the person's log, with its Stripe session id.

Code: `specdev/payments.py` (Checkout, signature checks, which events count) and `credit()` / `_stripe_webhook` in `specdev/server.py`. Tests: `tests/test_specdev.py` (payments section).

## Trying it with Stripe's test mode

You need a Stripe account. Only you can create it, at https://dashboard.stripe.com/register. Test mode needs no bank details.

1. In the Stripe dashboard, switch to **Test mode** and copy the **secret key** (`sk_test_...`) from Developers → API keys.
2. Developers → Webhooks → **Add endpoint**:
   - URL: `https://d15a3imp4047e7.cloudfront.net/api/stripe/webhook`
   - events: `checkout.session.completed` and `checkout.session.async_payment_succeeded`.

   Then copy its **signing secret** (`whsec_...`).
3. Create `deploy/stripe.env` (it is not checked in) with two lines:
   ```
   SPECDEV_STRIPE_SECRET_KEY=sk_test_...
   SPECDEV_STRIPE_WEBHOOK_SECRET=whsec_...
   ```
4. Deploy (`python deploy/deploy.py`). The keys go into `/etc/specdev.env`, which only root can read. Their names start with `SPECDEV_`, so the sandbox keeps them away from the agent's processes.
5. On the workbench, open the budget, choose an amount, and pay with Stripe's test card `4242 4242 4242 4242` (any future expiry date, any CVC). The page comes back, and the budget goes up a few seconds later.

**On your own machine:** run the server with `SPECDEV_BUDGETS=1` and the two keys set. Then forward Stripe's webhooks to it with the Stripe CLI: `stripe listen --forward-to localhost:8766/api/stripe/webhook`. That prints the `whsec_...` to use.

## Before taking real money

This is a prototype. Switching to a live key (`sk_live_...`) makes real charges, and `deploy.py` prints a warning when it sees one. Before doing that:

- **Business details in Stripe:** a verified account with a business name, a statement descriptor and a payout bank account.
- **Tax:** whether you must charge VAT or sales tax, and where. Stripe Tax can calculate it. Credit is the amount sold, so adding tax at checkout doesn't change what people get.
- **Terms and refunds:** a page saying what credit buys and whether unused credit is refundable, linked from the top-up menu. Refunds and disputes are handled in Stripe's dashboard; the workbench doesn't yet take credit back when one happens (the `charge.refunded` and `charge.dispute.created` events).
- **Receipts:** turn on Stripe's emailed receipts.
- **A sleeping instance:** if a webhook arrives while the instance is off, CloudFront answers with an error and Stripe retries for up to three days. In practice the instance is awake, because the person has just been using it.
- **Fixed amounts:** only the fixed amounts `TOP_UPS` in `server.py` can be bought; there is no free-form amount.
