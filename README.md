# Horizon AI Agents

This is a starter agent hub for promoting an eBay store and answering product questions from Facebook, Instagram, TikTok, Manychat, Zapier, and Metricool workflows.

## What It Does

- Imports current active eBay listings into a local inventory database through the eBay API.
- Answers customer questions with an OpenAI-powered inventory agent.
- Returns Manychat Dynamic Block responses for Facebook and Instagram automations.
- Gives Zapier webhook endpoints for customer Q&A and social post generation.
- Produces Metricool-ready payloads that Zapier can map into Metricool's "Schedule Post" action.

## Launch Guides

- [Launch checklist](docs/launch-checklist.md)
- [Manychat payloads](docs/manychat-payloads.md)
- [Zapier and Metricool field map](docs/zapier-metricool-map.md)
- [Slow-mover social outreach](docs/slow-mover-outreach.md)

## Local Setup

1. Create a Python 3.11+ virtual environment and install dependencies:

   ```bash
   python3.12 -m venv .venv
   .venv/bin/python -m pip install -e '.[dev]'
   ```

2. Import sample inventory:

   ```bash
   .venv/bin/python scripts/import_inventory_csv.py data/inventory_sample.csv
   ```

3. Start the service:

   ```bash
   ./scripts/start_service.sh
   ```

4. Open:

   ```text
   http://127.0.0.1:8010/docs
   ```

## Main Endpoints

- `GET /health` checks that the service is running.
- `GET /inventory/search?q=keyboard` searches stock.
- `POST /inventory/import` imports JSON inventory items.
- `POST /inventory/import/ebay-store-page` imports public listing cards from an eBay store URL.
- `POST /inventory/sync/ebay` syncs active seller listings from the eBay API when `EBAY_ACCESS_TOKEN` is set.
- `POST /inventory/sync/store-page` refreshes from the configured default eBay store URL only as a fallback.
- `POST /walmart/import/preview` validates eBay listings for Walmart without publishing them.
- `POST /walmart/import/submit` submits confirmed, catalog-matched listings to Walmart Marketplace.
- `GET /walmart/feeds/{feed_id}` returns Walmart feed and item-level processing results.
- `POST /walmart/inventory/sync` copies eBay quantities to already-created Walmart SKUs.
- `POST /webhooks/manychat` answers Manychat Dynamic Block or External Request calls.
- `POST /webhooks/zapier/customer-question` returns a JSON answer for Zapier.
- `POST /webhooks/zapier/social-drafts` creates Facebook, Instagram, and TikTok post drafts.
- `POST /webhooks/zapier/slow-mover-outreach` creates engagement-first posts for stale eBay items.
- `POST /webhooks/metricool/inbox` answers Metricool inbox/comment events routed through Zapier.
- `POST /webhooks/metricool/schedule-inventory` refreshes active eBay inventory and directly fills Metricool's planner with the every-two-hours rotation. The existing Render daily job calls it automatically.
- `GET /reports/daily` returns a daily Metricool effectiveness report.
- `GET /reports/daily.md` returns the same report as email-ready Markdown.
- `GET /reports/daily.pdf` returns a polished PDF attachment.
- `GET` or `POST /webhooks/zapier/daily-report` returns Zapier-friendly report fields.

## Manychat Flow

Create a Dynamic Block or External Request in Manychat and send a JSON body like:

```json
{
  "message": "{{last_text_input}}",
  "channel": "instagram",
  "subscriber_id": "{{subscriber.id}}",
  "first_name": "{{first_name}}"
}
```

Point it to:

```text
https://your-public-service-url/webhooks/manychat
```

If you set `WEBHOOK_SHARED_SECRET`, send it as the `x-horizon-secret` header.

## Zapier And Metricool Flow

For product Q&A:

1. Trigger: Manychat, Metricool, or Webhooks by Zapier catches a new customer message.
2. Action: POST the message to `/webhooks/zapier/customer-question`.
3. Action: Send the returned `reply` back through Manychat or Metricool.

For advertising posts:

1. Trigger: Schedule by Zapier every day, new eBay listing, or manual webhook.
2. Action: POST a request to `/webhooks/zapier/social-drafts`.
3. Action: Use Looping by Zapier over the returned `metricool_*_items` fields, plus `publicationDate_items` and `draft_items`, then map each loop item into Metricool's `Schedule Post` action.

For stale eBay listings:

1. Trigger: Schedule by Zapier, a slow-mover report, or a manual webhook.
2. Action: POST slow item metrics to `/webhooks/zapier/slow-mover-outreach`.
3. Action: Loop over the returned Metricool fields for outreach posts, and connect `comment_keyword_items` to ManyChat keyword replies.

For daily reporting:

1. Trigger: Schedule by Zapier once per morning.
2. Action: Webhooks by Zapier `GET https://your-public-service-url/webhooks/zapier/daily-report`.
3. Optional query: `date=YYYY-MM-DD`. If omitted, the report uses yesterday in Central time.
4. Action: Email by Zapier, Gmail, Slack, or Google Sheets using `email_body` for the message and `attachment_url` for the report PDF.

GitHub Actions can send the same report without Zapier:

1. Workflow: `.github/workflows/daily-report-email.yml`, scheduled for `15:10 UTC` daily so Metricool's overnight sync has time to complete.
2. Script: `python scripts/send_daily_report_email.py`.
3. Required GitHub Secrets: `SMTP_HOST`, `SMTP_USERNAME`, and `SMTP_PASSWORD`.
4. Optional GitHub Secrets or Variables: `SMTP_PORT`, `SMTP_SECURITY`, `REPORT_BASE_URL`, `REPORT_EMAIL_TO`, `REPORT_EMAIL_FROM`, `REPORT_EMAIL_FROM_NAME`, and `WEBHOOK_SHARED_SECRET`.

Render can send the same report without Zapier or GitHub:

1. The `render.yaml` blueprint includes a `horizon-daily-report-email` Cron Job scheduled at `0 15 * * *` (9:00 AM CST / 10:00 AM CDT), after Metricool's overnight sync window.
2. To send from Gmail, set `REPORT_EMAIL_PROVIDER=gmail`, `REPORT_EMAIL_FROM=sean.fouz@gmail.com`, and `GMAIL_SENDER=sean.fouz@gmail.com`.
3. Add Google OAuth variables to the web service in Render: `GMAIL_CLIENT_ID` plus either `GMAIL_CLIENT_SECRET`, `GMAIL_CLIENT_SECRET_FILE`, or `GMAIL_CLIENT_CREDENTIALS_FILE`. A Google OAuth JSON secret file can also be uploaded to Render as a secret file; Render mounts it at `/etc/secrets/<filename>`.
4. Add `https://horizon-ai-agents.onrender.com` as the Google OAuth JavaScript origin and `https://horizon-ai-agents.onrender.com/oauth2callback` as the authorized redirect URI.
5. Open `/gmail/oauth/start?secret=YOUR_WEBHOOK_SHARED_SECRET` on the Render web service, approve Gmail access, then copy the returned `GMAIL_REFRESH_TOKEN_CURRENT` value into the Render web service environment.
6. Copy the same `WEBHOOK_SHARED_SECRET` value from the web service into the `horizon-daily-report-email` Cron Job environment so the cron can call the protected email endpoint.
7. In Google Cloud, keep the OAuth consent screen published/in production for long-lived Gmail refresh tokens. Google expires refresh tokens after 7 days when an external OAuth app is left in Testing and requests scopes beyond basic profile/email/openid.
8. To check the live Gmail configuration without exposing secrets, open `/gmail/oauth/status?secret=YOUR_WEBHOOK_SHARED_SECRET&test_refresh=true`. It reports token/client fingerprints and whether Google accepts the refresh token.
9. The Cron Job runs `python scripts/post_render_daily_report_email.py`, which calls `POST https://horizon-ai-agents.onrender.com/reports/daily/email` and prints any error response body while Gmail and Metricool credentials stay on the web service.
10. For a manual test from the live web service, `POST /reports/daily/email?dry_run=true` prepares the email, and `POST /reports/daily/email` sends it.

Example social draft request:

```json
{
  "promote_all_inventory": true,
  "query": "all inventory",
  "max_products_per_run": 50,
  "platforms": ["facebook", "instagram", "tiktok", "linkedin"],
  "tiktok_daily_post_cap": 3,
  "brand_name": "Horizon Wireless",
  "sale_name": "Horizon Wireless Summer Sale",
  "store_url": "https://www.ebay.com/str/exactspec",
  "sale_media_url": "https://raw.githubusercontent.com/seanfouz-source/horizon-ai-agents-live/main/assets/horizon-summer-sale-square.jpg",
  "as_draft": false,
  "auto_publish": true
}
```

When `promote_all_inventory` is true, the app creates one Metricool payload per
eligible active eBay listing, using an Instagram-safe square version of the eBay
store's July Summer Sale banner as the default campaign media. If `sale_media_url` and `media_url` are omitted, the
app falls back to the selected eBay product image when available. Metricool remains the public social scheduler; the app only prepares
Metricool-ready payloads and records local scheduling history to prevent reruns
from duplicating posts. All-inventory campaigns rotate one listing every two
hours from 09:00 through 21:00 Central time, including weekends. The start time
uses `METRICOOL_MORNING_POST_TIME`; the end and interval use
`METRICOOL_INVENTORY_POST_END_TIME` and
`METRICOOL_INVENTORY_POST_INTERVAL_HOURS`. Other social campaigns keep the
two-per-day morning/evening defaults. Items are not reposted within
`METRICOOL_REPOST_COOLDOWN_DAYS` by default. Summer Sale product captions include
the eBay store page plus a visible `View this listing:` eBay item link, and the
Zapier response also includes
`metricool_link_url` fields for Metricool link/URL mappings when that field is
available. For Metricool's required `Publication Date/Time` and `As draft`
fields, the response includes both the readable
`metricool_publication_date_time` / `metricool_as_draft` fields and Zapier's
internal `publicationDate` / `draft` aliases.

## eBay Inventory

Start with CSV import from Seller Hub or your own listing export. Required columns are:

```text
sku,title,description,condition,price,currency,quantity,ebay_item_id,ebay_url,image_url,category,item_specifics
```

`item_specifics` should be JSON, such as:

```json
{"Brand":"Sony","Color":"Black","Size":"Large"}
```

Live sync can use `EBAY_ACCESS_TOKEN`, long-lived OAuth refresh credentials, or
client credentials. eBay access tokens are short-lived, so production should set
`EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET`; the app will mint a fresh OAuth
Application access token before inventory syncs that use the public Browse API
fallback. If you also set `EBAY_REFRESH_TOKEN`, the app will prefer that user
refresh-token flow before each inventory sync. `EBAY_OAUTH_SCOPES` defaults to
`https://api.ebay.com/oauth/api_scope`. eBay's traditional Trading API does not
use OAuth scopes, but it does require the user access token created from the
refresh token. Add eBay Sell Inventory scopes only if that refresh token was
authorized for them. `EBAY_TRADING_COMPATIBILITY_LEVEL` defaults to the current
Trading API schema version used by this project.

The service tries the eBay Sell Inventory API first, then falls back to the eBay
Buy Browse API seller search/details endpoint when Seller Hub listings are not
represented as Sell Inventory records. Browse item groups are expanded into
separate, stable SKU rows and catalog GTINs are requested for each purchasable
variation. When an eBay refresh token is configured, the importer additionally
calls Trading API `GetItem` as the listing owner. That owner-only step recovers
the original seller SKUs, variation-level UPC/EAN/ISBN values, exact available
quantity, item specifics, variation images, and packaged weight when those values
are saved on eBay. A manually generated OAuth User Token in `EBAY_ACCESS_TOKEN`
can run the same Trading API enrichment while that short-lived token remains
valid; a refresh token is still preferred for unattended syncs. It stores active,
available SKU rows only.

By default, social draft endpoints refresh inventory before generating Metricool
payloads (`SYNC_INVENTORY_BEFORE_SOCIAL_POSTS=true`). This lets newly changed
eBay listing photos flow into future `media_01` values automatically. If the
eBay API token is invalid or eBay does not return data, the app tries the public
store-page fallback and includes `inventory_refresh_*`, `ebay_sync_*`, and
`store_sync_*` fields in Zapier responses so the Zap can show whether fresh
listing photos were used or cached inventory was used.

## eBay To Walmart Marketplace

The Walmart bridge uses Walmart's OAuth client-credentials flow and the
`MP_ITEM_MATCH` 4.2 offer feed. This is the safest first path for phones and
tablets that already exist in Walmart's catalog: Walmart reuses its catalog
content while this service supplies the seller SKU, product identifier, price,
condition, shipping weight, and condition image when required.

Walmart's public Marketplace API does not provide the **Save draft** action
available in Seller Center. To prevent accidental publication, this service
first stores API-enriched drafts in its persistent Render database. On startup,
the live eBay API snapshot is refreshed, Walmart's catalog is searched by the
listing's brand/model/variation data, and the resulting candidates are attached
to each draft. No Walmart feed is submitted by this staging workflow.

Safe draft endpoints:

```text
GET  /walmart/drafts/summary
GET  /walmart/drafts?secret=YOUR_WEBHOOK_SHARED_SECRET
POST /walmart/drafts/generate?secret=YOUR_WEBHOOK_SHARED_SECRET
```

The generate request accepts `sync_ebay_first`, `search_walmart_catalog`,
`max_items`, optional `skus`, and `catalog_candidates_per_item`. Publishing
remains a separate operation behind the existing `confirm=true` guard.

For an explicitly authorized API-only batch that must be visible in Walmart
without becoming buyable, set a unique `WALMART_UNPUBLISHED_BATCH_ID`. The
startup worker submits only a single exact catalog match for each SKU and then
submits inventory quantity `0`. The batch ID and persistent job record make the
operation idempotent; ambiguous matches are skipped. Monitor the non-sensitive
job state at `GET /walmart/unpublished/summary` and Walmart's live processing
responses at `GET /walmart/unpublished/feeds`.

Add these secret environment variables to the `horizon-ai-agents` Render web
service. Do not commit their values:

```text
WALMART_CLIENT_ID
WALMART_CLIENT_SECRET
```

The blueprint already defines the production API URL, US market, service name,
and optional `WALMART_CHANNEL_TYPE`. The channel type can remain blank unless
Walmart assigned one during onboarding. Confirm authentication without
publishing anything:

```text
GET /walmart/status?test_auth=true&secret=YOUR_WEBHOOK_SHARED_SECRET
```

Walmart requires a UPC/GTIN/EAN/ISBN and packaged shipping weight for every
offer-by-match listing. The live eBay API importer preserves those fields when
eBay returns them. You can provide missing values as per-SKU overrides during a
preview; overrides are never written back to eBay:

```json
{
  "sync_ebay_first": true,
  "verify_catalog": true,
  "skus": ["EBAY-366419891578"],
  "overrides": {
    "EBAY-366419891578": {
      "product_id_type": "UPC",
      "product_id": "123456789012",
      "shipping_weight_lbs": 1.25,
      "condition": "Open Box"
    }
  }
}
```

Send that body to `POST /walmart/import/preview` first. The response separates
`ready` and `blocked` items and reports each missing field. Start with one canary
SKU. After reviewing the preview, send the same body to
`POST /walmart/import/submit` with `"confirm": true`. The submit endpoint runs a
live Walmart catalog check again and only sends items that resolve to an
existing published Walmart catalog item.

The submission returns a `feed_id`. Check
`GET /walmart/feeds/{feed_id}?include_details=true` until Walmart reports
`PROCESSED`, then review `itemsSucceeded`, `itemsFailed`, and the item ingestion
errors. Once the offers exist, copy the current eBay quantities with:

```json
{
  "skus": ["EBAY-366419891578"],
  "sync_ebay_first": true,
  "include_zero_quantity": true,
  "confirm": true
}
```

Send that body to `POST /walmart/inventory/sync`. Quantity updates overwrite
Walmart's current quantity, including zero for ended eBay listings, so this
endpoint also requires explicit confirmation. Full `MP_ITEM` creation for
products absent from Walmart's catalog is intentionally blocked until all
category-specific compliance attributes are supplied; the preview identifies
those non-matches instead of inventing regulatory or product data.

## eBay Store URL Workaround

The app now uses the eBay API as the default inventory source. The ExactSpec
public store page remains configured only as a fallback:

```text
https://www.ebay.com/str/exactspec
```

The app tries to refresh from that store page when the service starts. You can also refresh it manually:

```bash
.venv/bin/python scripts/import_ebay_store_url.py
```

Or through the API:

```text
POST /inventory/sync/store-page
```

To override the store URL for a one-off import:

```bash
.venv/bin/python scripts/import_ebay_store_url.py "https://www.ebay.com/str/YOUR-STORE" 3
```

If eBay blocks a refresh, the app keeps the last successful inventory in the database. This workaround can usually capture public title, price, image, item ID, and listing URL. It cannot reliably capture exact quantity, private SKU, private item specifics, shipping rules, or unpublished inventory, so imported items are marked with `source = ebay-store-page` and `quantity = 1`.
