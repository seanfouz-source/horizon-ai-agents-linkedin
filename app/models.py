import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator


Channel = Literal["facebook", "messenger", "instagram", "tiktok", "linkedin", "whatsapp", "telegram", "web", "unknown"]
SocialPlatform = Literal["facebook", "instagram", "tiktok", "linkedin"]
GroupInteractionType = Literal["group_comment", "group_post_comment", "group_dm_to_page", "page_dm", "instagram_dm", "manual"]


class InventoryItem(BaseModel):
    sku: str
    title: str
    description: str | None = None
    condition: str | None = None
    price: float | None = None
    currency: str = "USD"
    quantity: int = 0
    ebay_item_id: str | None = None
    ebay_url: str | None = None
    image_url: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    category: str | None = None
    listing_status: str | None = None
    item_specifics: dict[str, str] = Field(default_factory=dict)
    source: str = "manual"
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class InventorySearchResult(BaseModel):
    total: int
    items: list[InventoryItem]


class EbayStoreImportRequest(BaseModel):
    store_url: str
    max_pages: int = Field(default=1, ge=1, le=10)


class EbayDraftBatchRequest(BaseModel):
    batch_id: str
    offset: int = Field(default=0, ge=0)
    max_items: int = Field(default=25, ge=1, le=25)
    catalog_candidates_per_item: int = Field(default=10, ge=1, le=10)
    confirm: bool = False


WalmartProductIdType = Literal["GTIN", "UPC", "EAN", "ISBN"]


class WalmartItemOverride(BaseModel):
    product_id_type: WalmartProductIdType | None = None
    product_id: str | None = None
    shipping_weight_lbs: float | None = Field(default=None, gt=0)
    condition: str | None = None
    price: float | None = Field(default=None, gt=0)
    quantity: int | None = Field(default=None, ge=0)
    main_image_url: str | None = None


class WalmartImportRequest(BaseModel):
    skus: list[str] = Field(default_factory=list)
    overrides: dict[str, WalmartItemOverride] = Field(default_factory=dict)
    max_items: int = Field(default=200, ge=1, le=200)
    sync_ebay_first: bool = False
    verify_catalog: bool = False
    confirm: bool = False


class WalmartInventorySyncRequest(BaseModel):
    skus: list[str] = Field(default_factory=list)
    max_items: int = Field(default=200, ge=1, le=200)
    sync_ebay_first: bool = True
    include_zero_quantity: bool = True
    confirm: bool = False


class WalmartDraftGenerateRequest(BaseModel):
    skus: list[str] = Field(default_factory=list)
    max_items: int = Field(default=200, ge=1, le=200)
    sync_ebay_first: bool = True
    search_walmart_catalog: bool = True
    catalog_candidates_per_item: int = Field(default=5, ge=1, le=10)


class WalmartAutoPublishRequest(BaseModel):
    max_items: int = Field(default=200, ge=1, le=200)
    sync_ebay_first: bool = True
    confirm: bool = False


class CustomerQuestion(BaseModel):
    message: str
    channel: Channel = "unknown"
    user_id: str | None = None
    first_name: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class CustomerAnswer(BaseModel):
    reply: str
    channel: Channel = "unknown"
    matched_items: list[InventoryItem] = Field(default_factory=list)
    recommended_items: list[InventoryItem] = Field(default_factory=list)
    needs_human: bool = False
    redirect_to_ebay: bool = False
    conversation_allowed: bool = True
    ebay_item_id: str | None = None
    ebay_listing_url: str | None = None
    social_post_id: str | None = None
    messenger_conversation_id: str | None = None


class SocialDraftRequest(BaseModel):
    query: str | None = None
    sku: str | None = None
    campaign_goal: str = "Advertise the Horizon Wireless Summer Sale and drive shoppers to the eBay store."
    tone: str = "friendly, clear, and sales-focused without hype"
    platforms: list[SocialPlatform] = Field(default_factory=lambda: ["facebook", "instagram", "tiktok", "linkedin"])
    posts_per_platform: int = Field(default=1, ge=1, le=5)
    promote_all_inventory: bool = False
    ignore_recent_history: bool = False
    max_products_per_run: int = Field(default=200, ge=1, le=200)
    cross_post_to_all_platforms: bool = True
    brand_name: str | None = None
    sale_name: str = "Horizon Wireless Summer Sale"
    store_url: str | None = None
    sale_media_url: str | None = None
    media_url: str | None = None
    campaign_video: str | None = None
    facebook_groups: list[str] = Field(default_factory=list)
    publish_to_facebook_groups: bool = False
    tiktok_daily_post_cap: int = Field(default=200, ge=0, le=200)
    publish_after: str | None = None
    as_draft: bool = True
    auto_publish: bool = False

    @field_validator("platforms", mode="before")
    @classmethod
    def normalize_platform_list(cls, value: object) -> object:
        if isinstance(value, str):
            return _split_zapier_list(value)
        return value

    @field_validator("facebook_groups", mode="before")
    @classmethod
    def normalize_facebook_groups(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            return _split_zapier_list(value)
        return value


class SocialPost(BaseModel):
    platform: SocialPlatform
    text: str
    product_sku: str | None = None
    product_title: str | None = None
    ebay_url: str | None = None
    media_url: str | None = None
    suggested_schedule: str | None = None
    post_type: str = "post"
    hashtags: list[str] = Field(default_factory=list)


class SocialDraftPlan(BaseModel):
    campaign_name: str
    posts: list[SocialPost]
    notes: str = ""


class SocialDraftBatch(BaseModel):
    campaign_name: str
    posts: list[SocialPost]
    metricool_payloads: list[dict[str, object]] = Field(default_factory=list)
    notes: str = ""


class SlowMoverMetric(BaseModel):
    sku: str
    listing_age_days: int | None = Field(default=None, ge=0)
    days_since_sale: int | None = Field(default=None, ge=0)
    views: int | None = Field(default=None, ge=0)
    watchers: int | None = Field(default=None, ge=0)
    quantity_sold: int | None = Field(default=None, ge=0)
    notes: str | None = None


class SlowMoverOutreachRequest(BaseModel):
    query: str | None = "all phones"
    skus: list[str] = Field(default_factory=list)
    slow_mover_metrics: list[SlowMoverMetric] = Field(default_factory=list)
    max_items: int = Field(default=5, ge=1, le=25)
    angles_per_item: int = Field(default=2, ge=1, le=3)
    brand_name: str = "Horizon Wireless"
    platforms: list[SocialPlatform] = Field(default_factory=lambda: ["facebook", "instagram", "tiktok", "linkedin"])
    cross_post_to_all_platforms: bool = True
    tiktok_daily_post_cap: int = Field(default=3, ge=0, le=15)
    publish_after: str | None = None
    as_draft: bool = True
    auto_publish: bool = False

    @field_validator("platforms", "skus", mode="before")
    @classmethod
    def normalize_zapier_lists(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            return _split_zapier_list(value)
        return value

    @field_validator("slow_mover_metrics", mode="before")
    @classmethod
    def normalize_slow_mover_metrics(cls, value: object) -> object:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return json.loads(value)
        return value


class SlowMoverOutreachDraft(BaseModel):
    sku: str
    title: str
    ebay_url: str
    priority_score: int
    reason: str
    comment_keyword: str
    manychat_reply: str
    outreach_posts: list[SocialPost] = Field(default_factory=list)
    manual_outreach_notes: str


class SlowMoverOutreachPlan(BaseModel):
    campaign_name: str
    drafts: list[SlowMoverOutreachDraft] = Field(default_factory=list)
    posts: list[SocialPost] = Field(default_factory=list)
    metricool_payloads: list[dict[str, object]] = Field(default_factory=list)
    manychat_keywords: list[str] = Field(default_factory=list)
    notes: str = ""


class FacebookGroupLead(BaseModel):
    name: str
    url: str | None = None
    member_count: int | None = None
    audience_notes: str | None = None
    rules_text: str | None = None
    allows_promotions: bool | None = None
    notes: str | None = None


class GroupCandidateReview(BaseModel):
    name: str
    url: str | None = None
    relevance_score: int = Field(ge=0, le=100)
    recommendation: Literal["prioritize", "review", "skip"]
    reason: str
    rule_risks: list[str] = Field(default_factory=list)


class GroupPostDraft(BaseModel):
    group_name: str | None = None
    campaign_video: str | None = None
    post_text: str
    cta: str
    manual_posting_notes: str


class GroupOutreachRequest(BaseModel):
    campaign_goal: str = "Promote Horizon Wireless eBay listings and wholesale availability."
    brand_name: str = "Horizon Wireless"
    audience_keywords: list[str] = Field(default_factory=lambda: ["phone resellers", "electronics wholesale", "eBay sellers"])
    campaign_video: str | None = None
    ebay_store_url: str = "https://www.ebay.com/str/exactspec"
    group_leads: list[FacebookGroupLead] = Field(default_factory=list)
    notes: str | None = None

    @field_validator("audience_keywords", mode="before")
    @classmethod
    def normalize_audience_keywords(cls, value: object) -> object:
        if isinstance(value, str):
            return _split_zapier_list(value)
        return value


class GroupOutreachPlan(BaseModel):
    summary: str
    candidate_groups: list[GroupCandidateReview] = Field(default_factory=list)
    join_request_draft: str
    post_drafts: list[GroupPostDraft] = Field(default_factory=list)
    compliance_checklist: list[str] = Field(default_factory=list)
    notes: str = ""


class GroupReplyRequest(BaseModel):
    message: str
    group_name: str | None = None
    group_url: str | None = None
    author_name: str | None = None
    post_context: str | None = None
    interaction_type: GroupInteractionType = "group_comment"
    user_opted_in: bool = False
    channel: Channel = "facebook"
    rules_text: str | None = None


class GroupReplyDraft(BaseModel):
    reply: str
    channel: Channel = "facebook"
    matched_items: list[InventoryItem] = Field(default_factory=list)
    needs_human: bool = False
    manual_review_required: bool = True
    can_auto_send: bool = False
    compliance_notes: str


def _split_zapier_list(value: str) -> list[str]:
    return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
