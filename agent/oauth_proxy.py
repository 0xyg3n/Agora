"""Anthropic OAuth proxy for LiveKit agents.

Accepts Anthropic API requests on localhost:8090 and forwards them to
api.anthropic.com with an OAuth bearer token in the x-api-key header.
Supports streaming (SSE passthrough).
"""

import json
import logging
import os

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger("oauth_proxy")

ANTHROPIC_API = "https://api.anthropic.com"
TOKEN = os.environ.get("ANTHROPIC_OAUTH_TOKEN", "")
PORT = int(os.environ.get("OAUTH_PROXY_PORT", "8090"))

app = FastAPI(docs_url=None, redoc_url=None)

_client = httpx.AsyncClient(
    base_url=ANTHROPIC_API,
    timeout=120.0,
    follow_redirects=True,
    limits=httpx.Limits(max_connections=50, max_keepalive_connections=10, keepalive_expiry=120),
)


def _get_token() -> str:
    return TOKEN or os.environ.get("ANTHROPIC_OAUTH_TOKEN", "")


@app.get("/health")
async def health():
    has_token = bool(_get_token())
    return {"status": "ok", "has_token": has_token}


@app.post("/v1/messages")
async def proxy_messages(request: Request):
    token = _get_token()
    if not token:
        return JSONResponse({"error": "ANTHROPIC_OAUTH_TOKEN not set"}, status_code=500)

    body = await request.body()

    # Forward all original headers except host, add auth
    headers = {
        "x-api-key": token,
        "content-type": request.headers.get("content-type", "application/json"),
        "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
    }
    # Pass through anthropic-beta if present
    if beta := request.headers.get("anthropic-beta"):
        headers["anthropic-beta"] = beta

    # Check if client requested streaming
    try:
        req_json = json.loads(body)
        is_stream = req_json.get("stream", False)
    except Exception:
        is_stream = False

    if is_stream:
        return await _stream_response(body, headers)
    else:
        return await _non_stream_response(body, headers)


async def _stream_response(body: bytes, headers: dict):
    """SSE passthrough — forward each chunk as it arrives from Anthropic.

    Preserves upstream status codes so SDK clients see real errors instead
    of an empty 200 response.
    """
    resp = await _client.send(
        _client.build_request("POST", "/v1/messages", content=body, headers=headers),
        stream=True,
    )

    if resp.status_code != 200:
        error_body = await resp.aread()
        await resp.aclose()
        logger.error(
            "Anthropic upstream error %d: %s",
            resp.status_code,
            error_body[:500].decode(errors="replace"),
        )
        try:
            return JSONResponse(json.loads(error_body), status_code=resp.status_code)
        except Exception:
            return JSONResponse(
                {"error": error_body.decode(errors="replace")},
                status_code=resp.status_code,
            )

    logger.info("Anthropic upstream 200 — streaming SSE response")

    async def generate():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        status_code=200,
        headers={
            "cache-control": "no-cache",
            "connection": "keep-alive",
        },
    )


async def _non_stream_response(body: bytes, headers: dict) -> JSONResponse:
    resp = await _client.post("/v1/messages", content=body, headers=headers)
    return JSONResponse(resp.json(), status_code=resp.status_code)


if __name__ == "__main__":
    if not _get_token():
        print("WARNING: ANTHROPIC_OAUTH_TOKEN not set — proxy will reject requests")
    print(f"Starting Anthropic OAuth proxy on http://127.0.0.1:{PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
