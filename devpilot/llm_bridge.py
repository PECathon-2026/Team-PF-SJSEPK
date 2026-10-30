from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

import requests

from .config import load_gemini_config

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"


def _extract_gemini_text(response_json: Any) -> str:
    try:
        candidates = response_json["candidates"]
        parts = candidates[0]["content"]["parts"]
    except (TypeError, KeyError, IndexError) as exc:
        raise ValueError("Gemini response did not include candidate text") from exc
    text = "".join(
        part["text"]
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )
    if not text:
        raise ValueError("Gemini response did not include text content")
    return text


def _response_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except requests.exceptions.JSONDecodeError as exc:
        raise ValueError("Gemini returned invalid JSON") from exc


def _post_json(url: str, *, headers: dict[str, str], payload: dict[str, Any], timeout: int, api_key: str) -> Any:
    response = None
    for attempt in range(3):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
            response.raise_for_status()
            return _response_json(response)
        except requests.HTTPError as exc:
            if response is not None and response.status_code in {429, 502, 503, 504} and attempt < 2:
                time.sleep(2**attempt)
                continue
            detail = response.text[:500].replace(api_key, "[redacted]") if response is not None else ""
            status = response.status_code if response is not None else "unknown"
            raise RuntimeError(f"Gemini request failed ({status}): {detail}") from exc
        except requests.RequestException as exc:
            message = str(exc).replace(api_key, "[redacted]")
            raise RuntimeError(f"Gemini request failed: {message}") from exc
    raise RuntimeError("Gemini request did not return a response")


def ask_gemini(prompt: str, *, timeout: int = 60) -> str:
    config = load_gemini_config()
    api_key = config.require_api_key()
    url = f"{GEMINI_ENDPOINT}/{quote(config.model, safe='')}:generateContent"
    response_json = _post_json(
        url,
        headers={"X-goog-api-key": api_key, "Content-Type": "application/json"},
        payload={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2},
        },
        timeout=timeout,
        api_key=api_key,
    )
    return _extract_gemini_text(response_json).strip()
