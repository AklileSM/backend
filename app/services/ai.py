import json
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

_cache: dict[str, str] = {}

def _extract_final_sections(text: str) -> str:
    """
    Attempt to remove any leading 'thinking' / scratchpad content by slicing
    from the first known section heading.
    """
    t = (text or "").strip()
    if not t:
        return t

    headings = [
        r"Description:",
        r"Safety Concerns:",
        r"Quality Concerns:",
        r"Safety Issues:",
        r"Quality Issues:",
    ]

    # Find earliest match (case-insensitive)
    earliest: int | None = None
    for h in headings:
        m = re.search(h, t, flags=re.IGNORECASE)
        if m:
            idx = m.start()
            earliest = idx if earliest is None else min(earliest, idx)

    return t[earliest:].strip() if earliest is not None else t


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
    Returns (url_or_data_url_for_hyperbolic, cache_key).
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
    # Some OpenAI-compatible providers (e.g. local Ollama) do not require auth.
    # Keep the code tolerant of an empty key.
    api_key = (settings.hyperbolic_api_key or "").strip()

    async with httpx.AsyncClient(timeout=120) as client:
        vision_url, cache_key = await _resolve_vision_url(client, db, image_url, file_id)

        if cache_key in _cache:
            return {"description": _cache[cache_key], "cached": True}

        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        response = await client.post(
            settings.hyperbolic_api_url,
            headers=headers,
            json={
                "model": settings.hyperbolic_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Return ONLY the final report text. Do NOT include any reasoning, step-by-step thinking, or planning.\n\n"
                                    "Use exactly this format:\n"
                                    "**Description:**\n"
                                    "(Write 5-8 sentences in paragraph form. No bullet points for Description.)\n"
                                    "This is a panoramic/360 image: account for wide-angle distortion and avoid assuming elements are misaligned unless misalignment is clearly visible across the scene.\n\n"
                                    "**Quality Concerns:**\n"
                                    "(Provide 0-5 numbered items.)\n"
                                    "Use numbered items like:\n"
                                    "1. ...\n"
                                    "2. ...\n\n"
                                    "**Safety Concerns:**\n"
                                    "(Provide 0-5 numbered items. )\n"
                                    "Use numbered items like:\n"
                                    "1. ...\n"
                                    "2. ...\n"
                                ),
                            },
                            {"type": "image_url", "image_url": {"url": vision_url}},
                        ],
                    }
                ],
                "max_tokens": 2048,
                "temperature": 0.1,
                "top_p": 0.001,
                "stream": False,
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            snippet = (e.response.text or "")[:800]
            raise RuntimeError(
                f"AI provider HTTP {e.response.status_code}: {snippet or e.response.reason_phrase}"
            ) from e

        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError(f"Unexpected AI provider response (no choices): {payload!r}"[:500])

        msg = choices[0].get("message") or {}
        content = msg.get("content")
        description: str

        # Handle a few common OpenAI-compat shapes:
        # - content is a string
        # - content is a list of parts (dicts and/or strings)
        # - some providers put the final text directly under choices[0].text
        if isinstance(content, str):
            description = content.strip()
        elif isinstance(content, list):
            parts: list[str] = []
            for p in content:
                if isinstance(p, str):
                    parts.append(p)
                elif isinstance(p, dict):
                    # OpenAI-style: {"type":"text","text":"..."}
                    if isinstance(p.get("text"), str):
                        parts.append(p["text"])
                    # Occasionally: {"type":"text","content":"..."}
                    elif isinstance(p.get("content"), str):
                        parts.append(p["content"])
                    else:
                        # Fall back to stringification for unknown dict shapes
                        parts.append(str(p))
                else:
                    parts.append(str(p))
            description = "".join(parts).strip()
        elif isinstance(content, dict):
            # Rare, but keep it tolerant
            if isinstance(content.get("text"), str):
                description = content["text"].strip()
            elif isinstance(content.get("content"), str):
                description = content["content"].strip()
            else:
                raise RuntimeError(f"Unexpected message content dict keys: {list(content.keys())!r}"[:500])
        else:
            # Non-chat completion adapters sometimes use choices[0].text
            alt_text = choices[0].get("text")
            if isinstance(alt_text, str):
                description = alt_text.strip()
            else:
                raise RuntimeError(f"Unexpected message content shape: {type(content)}")

        if not description:
            # Some Ollama/OpenAI-compat adapters return the actual text under `reasoning`
            # while leaving `content` empty. Fall back to that.
            reasoning = msg.get("reasoning")
            reasoning_is_str = isinstance(reasoning, str)
            reasoning_nonempty = reasoning_is_str and bool(reasoning.strip())
            if reasoning_nonempty:
                description = reasoning.strip()

        description = _extract_final_sections(description)

        if not description:
            # Include a small provider payload snippet for faster debugging.
            payload_snippet = json.dumps(payload, ensure_ascii=False)[:1200]
            # Also include whether `reasoning` was present, since some adapters put the text there.
            reasoning_preview = ""
            if isinstance(msg.get("reasoning"), str):
                reasoning_preview = msg.get("reasoning", "")[:120].replace("\n", "\\n")
            reasoning_present = isinstance(msg.get("reasoning"), str) and bool(msg.get("reasoning").strip())
            raise RuntimeError(
                "AI provider returned an empty description. "
                f"reasoning_present={reasoning_present} reasoning_preview={reasoning_preview!r} "
                f"payload_snippet={payload_snippet}"
            )

        _cache[cache_key] = description
        return {"description": description, "cached": False}
