"""Slack-backed approval gate. Same "return True for approved, False
for rejected" contract agents/approval.py already documents as the
CLI's swap point -- this file implements that contract using Slack
Block Kit + a pending-approval registry that slack_webhook.py resolves
when a button is clicked.
"""

import queue

from slack_sdk import WebClient

import config
from models import PriceRecommendation

_client = WebClient(token=config.SLACK_BOT_TOKEN)

# sku -> queue.Queue() that slack_webhook.py puts True/False into when
# the corresponding button is clicked. A dict keyed by SKU is enough
# for this project's single-run-at-a-time pipeline; a busier system
# would key by a run-scoped approval id instead.
PENDING: dict[str, "queue.Queue"] = {}


def ask_slack(rec: PriceRecommendation) -> bool:
    q: "queue.Queue" = queue.Queue()
    PENDING[rec.sku] = q

    _client.chat_postMessage(
        channel=config.SLACK_APPROVAL_CHANNEL,
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{rec.sku}*: ${rec.current_price:.2f} -> ${rec.recommended_price:.2f} "
                        f"({(rec.recommended_price - rec.current_price) / rec.current_price:+.1%})\n"
                        f"Confidence: {rec.confidence:.0%} | Margin: {rec.projected_margin_pct:.1%}\n"
                        f"Reasoning: {rec.reasoning}"
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "action_id": "approve",
                        "value": rec.sku,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Reject"},
                        "style": "danger",
                        "action_id": "reject",
                        "value": rec.sku,
                    },
                ],
            },
        ],
        text=f"Approval needed: {rec.sku}",
    )

    try:
        return q.get(timeout=config.APPROVAL_TIMEOUT_SECONDS)
    except queue.Empty:
        return False
    finally:
        PENDING.pop(rec.sku, None)
