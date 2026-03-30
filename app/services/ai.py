import base64
import ipaddress
import mimetypes
import re
from typing import Any
from urllib.parse import urlparse, urljoin

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import FileAsset
from app.services.storage import storage_service

settings = get_settings()

_cache: dict[str, str] = {}  # keyed by image identity; cleared on restart


def _bytes_to_data_url(data: bytes, content_type: str | None, name_hint: str) -> str:
    mime = (content_type or "").split(";")[0].strip() or mimetypes.guess_type(name_hint)[0] or "image/jpeg"
    b64 = base64.standard_b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _host_is_private_or_local(host: str | None) -> bool:
    if not host:
        return True
    h = host.lower()
    if h in ("localhost", "minio"):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


def _url_usable_by_remote_api(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    return not _host_is_private_or_local(p.hostname)


async def _resolve_vision_url(
    client: httpx.AsyncClient,
    db: Session | None,
    image_url: str,
    file_id: str | None,
) -> tuple[str, str]:
    """
    Returns (url_or_data_url_for_vision_api, cache_key).
    """
    if file_id and db is not None:
        asset = db.scalar(select(FileAsset).where(FileAsset.id == file_id))
        if asset is None:
            raise ValueError("file_id not found")
        if asset.media_type != "image":
            raise ValueError("AI vision is only supported for image files")
        raw = storage_service.get_object_bytes(asset.bucket_name, asset.object_name)
        data_url = _bytes_to_data_url(raw, asset.content_type, asset.display_name)
        return data_url, f"file:{file_id}"

    if image_url.startswith("data:image/"):
        return image_url, image_url[:256]

    if _url_usable_by_remote_api(image_url):
        return image_url, image_url

    # Presigned MinIO / localhost URLs: fetch from this backend's network, then send base64.
    async def _get_bytes(u: str) -> tuple[bytes, str]:
        r = await client.get(u, follow_redirects=True)
        r.raise_for_status()
        ct = (r.headers.get("content-type") or "").split(";")[0].strip() or "image/jpeg"
        return r.content, ct

    if image_url.startswith(("http://", "https://")):
        raw, ct = await _get_bytes(image_url)
        return _bytes_to_data_url(raw, ct, image_url), f"fetched:{image_url}"

    base = settings.frontend_url.rstrip("/") + "/"
    full = urljoin(base, image_url.lstrip("/"))
    raw, ct = await _get_bytes(full)
    return _bytes_to_data_url(raw, ct, full), f"fetched:{full}"


async def analyze_image_url(
    image_url: str,
    *,
    file_id: str | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=120) as client:
        vision_url, cache_key = await _resolve_vision_url(client, db, image_url, file_id)

        if cache_key in _cache:
            return {"description": _cache[cache_key], "cached": True}

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if settings.vision_api_key:
            headers["Authorization"] = f"Bearer {settings.vision_api_key}"

        response = await client.post(
            settings.vision_api_url,
            headers=headers,
            json={
                "model": settings.vision_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "You are a construction site inspector. Analyze this image and provide a concise report with three sections:\n\n"
                                    "1. SCENE: What is visible — type of work, materials, stage of construction, approximate number of workers, specific locations.\n"
                                    "2. QUALITY ISSUES: Specific defects or incomplete work you can actually see. Be concrete.\n"
                                    "3. SAFETY ISSUES: Specific hazards visible in the image — missing PPE, unsafe structures, exposed hazards. Be concrete.\n\n"
                                    "Rules: Do not repeat the same point across sections. Only report what is clearly visible. Be specific, not generic. If no issues are visible in a section, write 'None observed.' Do not invent or speculate."
                                ),
                            },
                            {"type": "image_url", "image_url": {"url": vision_url}},
                        ],
                    }
                ],
                "max_tokens": 700,
                "temperature": 0.35,
                "top_p": 0.9,
                "stream": False,
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            snippet = (e.response.text or "")[:800]
            raise RuntimeError(
                f"Vision API HTTP {e.response.status_code}: {snippet or e.response.reason_phrase}"
            ) from e

        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError(f"Unexpected vision API response (no choices): {payload!r}"[:500])

        msg = choices[0].get("message") or {}
        content = msg.get("content")

        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict)]
            raw_text = "".join(parts).strip()
        elif isinstance(content, str):
            raw_text = content.strip()
        else:
            raise RuntimeError(f"Unexpected message content shape: {type(content)}")

        # Qwen3 thinking models may put reasoning in <think>…</think> inline blocks
        # or in a separate `message.thinking` field, leaving `content` empty.
        # Strip inline think blocks first; fall back to the thinking field if needed.
        think_match = re.search(r"<think>(.*?)</think>(.*)", raw_text, re.DOTALL)
        if think_match:
            visible = think_match.group(2).strip()
            description = visible if visible else think_match.group(1).strip()
        else:
            description = raw_text

        if not description:
            description = (msg.get("thinking") or "").strip()

        if not description:
            raise RuntimeError("Vision model returned an empty description")

        _cache[cache_key] = description
        return {"description": description, "cached": False}