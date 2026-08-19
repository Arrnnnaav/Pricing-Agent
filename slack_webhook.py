"""FastAPI app receiving Slack's button-click interaction callback.
Run alongside main.py/scheduler.py (separate process) whenever a run
might need Slack approval:

    uvicorn slack_webhook:app --port 3000

Slack's Interactivity settings must point to this server's public URL
(e.g. via ngrok in development) + /slack/interact.
"""

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slack_sdk.signature import SignatureVerifier

import config
from agents.slack_approval import PENDING

app = FastAPI()

# None (verification skipped, with a console warning) if no signing
# secret is configured -- matches config.py's existing "Slack approval
# gate is optional" fallback behavior for SLACK_BOT_TOKEN.
_verifier = (
    SignatureVerifier(config.SLACK_SIGNING_SECRET)
    if config.SLACK_SIGNING_SECRET
    else None
)
if _verifier is None:
    print(
        "[slack_webhook] WARNING: SLACK_SIGNING_SECRET is not set -- "
        "/slack/interact will accept unverified requests."
    )


@app.post("/slack/interact")
async def slack_interact(request: Request):
    body = await request.body()

    if _verifier is not None and not _verifier.is_valid_request(body, request.headers):
        return JSONResponse(
            status_code=401, content={"ok": False, "error": "invalid_signature"}
        )

    form = await request.form()
    payload = json.loads(form["payload"])
    action = payload["actions"][0]
    sku = action["value"]
    approved = action["action_id"] == "approve"

    if sku in PENDING:
        PENDING[sku].put(approved)

    return {"ok": True}
