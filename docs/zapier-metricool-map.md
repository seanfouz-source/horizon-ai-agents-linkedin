# Zapier And Metricool Field Map

Use this guide for the two main Zaps.

## Zap 1: Customer Questions To AI Reply

Trigger options:

- Manychat new message
- Metricool new inbox conversation
- Webhooks by Zapier Catch Hook

Action:

```text
Webhooks by Zapier -> Custom Request
```

Method:

```text
POST
```

URL:

```text
https://YOUR-PUBLIC-URL/webhooks/zapier/customer-question
```

Headers:

```text
Content-Type: application/json
x-horizon-secret: YOUR_SHARED_SECRET
```

Body:

```json
{
  "message": "MESSAGE_FROM_TRIGGER",
  "channel": "instagram",
  "user_id": "USER_OR_SUBSCRIBER_ID",
  "first_name": "FIRST_NAME"
}
```

Use the response field named `reply` as the message text in the next Manychat or Metricool action.

## Zap 2: Generate Social Posts For Metricool

Trigger options:

- Manual Zapier trigger
- Schedule by Zapier, set to every day so Saturday and Sunday are included
- New row in a spreadsheet
- New item imported to inventory

Action:

```text
Webhooks by Zapier -> Custom Request
```

Method:

```text
POST
```

URL:

```text
https://YOUR-PUBLIC-URL/webhooks/zapier/social-drafts
```

Body:

```json
{
  "promote_all_inventory": true,
  "query": "all inventory",
  "max_products_per_run": 50,
  "platforms": ["facebook", "instagram", "tiktok", "linkedin"],
  "brand_name": "Horizon Wireless",
  "sale_name": "Horizon Wireless Summer Sale",
  "store_url": "https://www.ebay.com/str/exactspec",
  "sale_media_url": "https://raw.githubusercontent.com/seanfouz-source/horizon-ai-agents-live/main/assets/horizon-summer-sale-square.jpg",
  "as_draft": false,
  "auto_publish": true
}
```

Video campaign body:

```json
{
  "campaign_video": "wholesale",
  "campaign_goal": "Promote Horizon Wireless wholesale device availability to resellers and bulk buyers.",
  "platforms": ["facebook", "instagram", "tiktok", "linkedin"],
  "posts_per_platform": 1,
  "brand_name": "Horizon Wireless",
  "as_draft": false,
  "auto_publish": true
}
```

Use `campaign_video: "ebay-retail-store"` for the eBay retail store video.
The public media URLs are:

```text
https://YOUR-PUBLIC-URL/media/campaigns/wholesale.mp4
https://YOUR-PUBLIC-URL/media/campaigns/ebay-retail-store.mp4
```

The production inventory rotation does not require this Zap. Render refreshes
the active listings through the eBay API, hosts a compatible copy of each eBay
image when needed, and creates the Metricool planner post directly.

When `promote_all_inventory` is true, the agent schedules one listing every
hour from 09:00 through 21:00 Central time, including weekends. The start time
uses `METRICOOL_MORNING_POST_TIME`; the end and interval use
`METRICOOL_INVENTORY_POST_END_TIME` and
`METRICOOL_INVENTORY_POST_INTERVAL_HOURS`. If more than thirteen posts are
generated, the schedule continues at 09:00 the next day.

Other social-draft requests still use at most two Metricool posts per calendar
day. Their default slots are 09:00 and 18:00 and can be changed with
`METRICOOL_MORNING_POST_TIME` and `METRICOOL_EVENING_POST_TIME`.

When `publish_after` is provided, treat it as the earliest start time, not as a
single publication time for every eBay listing. The app still spreads the
returned posts across the daily slots and continues through weekends. In
Zapier, do not map the trigger timestamp directly into every Metricool loop
item; map each loop item's `publicationDate_items` value instead.

Response fields:

```text
metricool_payloads
metricool_payload_count
metricool_brand_name_items
metricool_facebook_items
metricool_instagram_items
metricool_tiktok_items
metricool_linkedin_items
metricool_publication_date_time_items
publicationDate_items
metricool_post_content_items
metricool_facebook_post_items
metricool_instagram_post_items
metricool_tiktok_post_items
metricool_linkedin_post_items
metricool_media_01_items
metricool_facebook_media_01_items
metricool_instagram_media_01_items
metricool_tiktok_media_01_items
metricool_linkedin_media_01_items
metricool_as_draft_items
draft_items
metricool_auto_publish_items
metricool_product_sku_items
metricool_product_title_items
metricool_ebay_url_items
metricool_buy_url_items
metricool_link_url_items
metricool_facebook_link_url_items
metricool_comment_keyword_items
metricool_manychat_reply_items
metricool_publication_date_time
metricool_post_content
metricool_facebook
metricool_instagram
metricool_tiktok
metricool_linkedin
metricool_media_01
metricool_facebook_post
metricool_instagram_post
metricool_tiktok_post
metricool_linkedin_post
metricool_facebook_media_01
metricool_instagram_media_01
metricool_tiktok_media_01
metricool_linkedin_media_01
metricool_publish_to_facebook_groups
metricool_facebook_groups
metricool_product_sku
metricool_product_title
metricool_ebay_url
metricool_buy_url
metricool_link_url
metricool_facebook_link_url
metricool_as_draft
publicationDate
draft
metricool_auto_publish
metricool_post_type
metricool_comment_keyword
metricool_manychat_reply
```

Each item in `metricool_payloads` maps to one Metricool `Schedule Post` action,
but the most reliable Zapier setup is to add `Looping by Zapier` after this
webhook and loop over the `metricool_*_items` arrays, plus
`publicationDate_items` and `draft_items` for Metricool's required internal
fields. The app records emitted payloads in local post history before returning
them, so a Zapier retry or manual rerun does not produce duplicate posts for the
same item inside the default 14-day cooldown. The flat `metricool_*` fields
intentionally contain only the first post for quick single-product tests; using
only those flat fields will schedule only one item.

## Metricool Schedule Post Mapping

For the first generated post, map these flat Zapier fields:

```text
Brand Name -> choose the connected Metricool brand in the dropdown
Facebook -> metricool_facebook
Instagram -> metricool_instagram
Tiktok -> metricool_tiktok
LinkedIn -> metricool_linkedin
Publication Date/Time -> metricool_publication_date_time
Post content / Text -> metricool_post_content
Facebook -> Post, if Metricool shows platform-specific fields -> metricool_facebook_post
Media 01 -> metricool_media_01
Facebook -> Media 01, if Metricool shows platform-specific fields -> metricool_facebook_media_01
Link URL / URL, if available -> metricool_link_url
As draft -> metricool_as_draft
Auto publish -> metricool_auto_publish
Post type -> POST
```

If Zapier shows Metricool's internal required field names instead of the
friendly labels, use these aliases:

```text
Publication Date/Time (publicationDate) -> publicationDate
As draft (draft) -> draft
```

For daily eBay inventory coverage, add `Looping by Zapier` before the Metricool
action and map the Metricool action from the current loop values:

```text
Facebook -> metricool_facebook_items
Instagram -> metricool_instagram_items
Tiktok -> metricool_tiktok_items
LinkedIn -> metricool_linkedin_items
Publication Date/Time (publicationDate) -> publicationDate_items
Post content / Text -> metricool_post_content_items
Facebook -> Post, if Metricool shows platform-specific fields -> metricool_facebook_post_items
Instagram -> Post, if Metricool shows platform-specific fields -> metricool_instagram_post_items
Tiktok -> Post, if Metricool shows platform-specific fields -> metricool_tiktok_post_items
LinkedIn -> Post, if Metricool shows platform-specific fields -> metricool_linkedin_post_items
Media 01 -> metricool_media_01_items
Facebook -> Media 01, if Metricool shows platform-specific fields -> metricool_facebook_media_01_items
Instagram -> Media 01, if Metricool shows platform-specific fields -> metricool_instagram_media_01_items
Tiktok -> Media 01, if Metricool shows platform-specific fields -> metricool_tiktok_media_01_items
LinkedIn -> Media 01, if Metricool shows platform-specific fields -> metricool_linkedin_media_01_items
Link URL / URL, if available -> metricool_link_url_items
As draft (draft) -> draft_items
Auto publish -> metricool_auto_publish_items
Post type -> metricool_post_type_items
```

Metricool should remain the public scheduler. Do not add direct Facebook,
Instagram, TikTok, or LinkedIn posting actions after this webhook.

For Facebook eBay product posts, keep the eBay URL in the caption and map any
Metricool link/URL field that Zapier exposes to `metricool_link_url` or the
current loop item's `link_url`. The generated Summer Sale caption includes the
store page and ends with its own `View this listing:
https://www.ebay.com/itm/...` line so Facebook has a visible purchase link even
when a separate link field is unavailable.

If the Metricool Zap action exposes Facebook Group destination fields, map:

```text
Publish to Facebook Groups -> metricool_publish_to_facebook_groups
Facebook Groups / Destinations -> metricool_facebook_groups
```

If those fields are not available, schedule the post to the connected Facebook
Page and share it manually to approved groups. Manychat's Facebook comment
automation works on Page posts, not Facebook Group posts.

Optional tracking fields:

```text
Product SKU -> metricool_product_sku or metricool_product_sku_items
Product Title -> metricool_product_title or metricool_product_title_items
eBay URL -> metricool_ebay_url or metricool_ebay_url_items
```

Review-first mode:

```text
As draft: true
Auto publish: false
```

Auto-publish mode:

```text
As draft: false
Auto publish: true
```

Review-first mode sends generated posts to Metricool for approval. Auto-publish
mode lets Metricool publish at the scheduled time.

For auto-publish, the flat Instagram field is only true when a media URL exists,
and the flat TikTok field is only true when a TikTok-safe media URL exists. The
app uses generated `.jpg` product cards by default because TikTok Business photo
posts reject `.png` media.
