"""FastAPI app receiving Slack's button-click interaction callback.
Run alongside main.py/scheduler.py (separate process) whenever a run
might need Slack approval:

    uvicorn slack_webhook:app --port 3000

Slack's Interactivity settings must point to this server's public URL
(e.g. via ngrok in development) + /slack/interact.
"""

import json

from fastapi import FastAPI, Request

from agents.slack_approval import PENDING

app = FastAPI()


@app.post("/slack/interact")
async def slack_interact(request: Request):
    form = await request.form()
    payload = json.loads(form["payload"])
    action = payload["actions"][0]
    sku = action["value"]
    approved = action["action_id"] == "approve"

    if sku in PENDING:
        PENDING[sku].put(approved)

    return {"ok": True}
