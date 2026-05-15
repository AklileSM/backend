import httpx
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.config import get_settings

settings = get_settings()
router = APIRouter()

_FORWARD_HEADERS = frozenset(
    {"content-type", "content-length", "etag", "cache-control", "last-modified", "accept-ranges"}
)


@router.get("/{path:path}")
async def proxy_minio(path: str, request: Request) -> StreamingResponse:
    """
    Transparent proxy for MinIO presigned URLs.
    The browser hits this endpoint; the backend fetches from the internal MinIO instance.
    The MinIO HMAC signature embedded in the query string is the only auth required —
    MinIO rejects expired or tampered signatures with 403, so no additional JWT check is needed.
    """
    protocol = "https" if settings.minio_use_ssl else "http"
    minio_url = f"{protocol}://{settings.minio_server}/{path}"
    qs = str(request.query_params)
    if qs:
        minio_url += f"?{qs}"

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=10.0)
    )
    minio_resp = await client.send(httpx.Request("GET", minio_url), stream=True)

    headers = {k: v for k, v in minio_resp.headers.items() if k.lower() in _FORWARD_HEADERS}

    async def _stream_and_cleanup():
        try:
            async for chunk in minio_resp.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await minio_resp.aclose()
            await client.aclose()

    return StreamingResponse(
        _stream_and_cleanup(),
        status_code=minio_resp.status_code,
        headers=headers,
    )
