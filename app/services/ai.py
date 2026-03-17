from typing import Any

import httpx

from app.config import get_settings

settings = get_settings()

_cache: dict[str, str] = {}


async def analyze_image_url(image_url: str) -> dict[str, Any]:
    if image_url in _cache:
        return {"description": _cache[image_url], "cached": True}

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            settings.hyperbolic_api_url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {settings.hyperbolic_api_key}",
            },
            json={
                "model": settings.hyperbolic_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Describe what is in this construction image. "
                                    "Also identify quality issues and safety issues."
                                ),
                            },
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    }
                ],
                "max_tokens": 2048,
                "temperature": 0.7,
                "top_p": 0.9,
                "stream": False,
            },
        )
        response.raise_for_status()
        payload = response.json()
        description = payload["choices"][0]["message"]["content"]
        _cache[image_url] = description
        return {"description": description, "cached": False}
