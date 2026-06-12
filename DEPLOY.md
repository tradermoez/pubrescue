# PubRescue deployment (10-minute checklist)

Everything is built and tested locally. To go live, the owner needs to do these
one-time account steps; after that the agent can manage deploys.

## 1. GitHub (5 min)
- Create a repo (e.g. `pubrescue`, can be private).
- Give the agent a fine-grained personal access token with `contents:write`
  on that repo (type `! export GH_TOKEN=...` in the session, or paste it when asked).
- Agent will push `products/pubrescue/` to it.

## 2. Render (5 min) — hosts the converter (free tier)
- Sign up at render.com (free, no card) with GitHub login.
- "New → Web Service" → pick the pubrescue repo → it auto-detects `render.yaml`/Dockerfile.
- Set env vars `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` (from step 3) and `BASE_URL` (the onrender.com URL).
- Free tier sleeps after 15 min idle — first visitor waits ~50s; acceptable for v1.

## 3. PayPal API credentials (10 min, uses your existing PayPal account)
- Go to https://developer.paypal.com/dashboard/ and log in with your normal PayPal account.
- Toggle from "Sandbox" to **"Live"** (top right), then Apps & Credentials → **Create App**
  (name it e.g. `pubrescue`).
- Copy the **Client ID** and **Secret** it shows you.
- These go into the env vars `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`
  (on Render, or in `~/moneymaker/.secrets/credentials.env` for the agent).
- No webhook needed — the app captures/verifies the order when the buyer returns.
- Buyers do NOT need a PayPal account: PayPal Checkout includes card guest checkout.

## 4. Optional polish (later, ~$10 from first profits)
- Domain `pubrescue.app` or similar → point at Render; until then the
  free `pubrescue.onrender.com` URL works.
- Cloudflare Pages for the marketing/SEO site (free, cardless).

## Local dev
```bash
cd products/pubrescue
docker build -t pubrescue-app .
docker run -p 8088:8000 -e DEV_SKIP_PAYMENT=1 pubrescue-app
# open http://localhost:8088
```

## Env vars
| var | meaning | default |
|---|---|---|
| PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET | Live app credentials | (unset → checkout returns 503) |
| PAYPAL_ENV | `live` or `sandbox` | live |
| PRICE_CENTS | batch price | 1900 |
| BASE_URL | public URL for Stripe redirects | http://localhost:8000 |
| DEV_SKIP_PAYMENT | 1 = skip Stripe (testing only) | unset |
| MAX_FILES / MAX_FILE_MB | upload limits | 50 / 50 |
| JOB_TTL_HOURS | file retention | 24 |
