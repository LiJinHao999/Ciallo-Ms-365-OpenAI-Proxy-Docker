"""Local disk cache for generated images.

Why this exists:
  OpenAI clients that request response_format=url need a durable, immediately
  loadable URL. Designer media proxy URLs often 504 when designer auth is stale
  or Chromium re-capture is slow — WebUIs then show a blank image even though
  /v1/images/generations returned 200.

  After a successful generation we materialize the image bytes once (using the
  account that produced them) and serve later GETs from disk without Designer.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class CachedImage:
    id: str
    account_id: str = ""
    account_email: str = ""
    content_type: str = "image/png"
    prompt: str = ""
    model: str = ""
    source_url: str = ""
    created_at: float = 0.0
    bytes_len: int = 0
    kind: str = "generation"  # generation | edit


class ImageCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._index_path = self.root / "index.json"
        self._index: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._index = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._index = {}

    def _save(self) -> None:
        try:
            tmp = self._index_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._index, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._index_path)
        except OSError:
            pass

    def _ext(self, content_type: str) -> str:
        ct = (content_type or "").lower()
        if "jpeg" in ct or "jpg" in ct:
            return "jpg"
        if "webp" in ct:
            return "webp"
        if "gif" in ct:
            return "gif"
        return "png"

    def put(
        self,
        content: bytes,
        *,
        content_type: str = "image/png",
        account_id: str = "",
        account_email: str = "",
        prompt: str = "",
        model: str = "",
        source_url: str = "",
        kind: str = "generation",
    ) -> CachedImage:
        if not content:
            raise ValueError("empty image content")
        img_id = "img_" + uuid.uuid4().hex
        ext = self._ext(content_type)
        path = self.root / f"{img_id}.{ext}"
        meta = CachedImage(
            id=img_id,
            account_id=account_id or "",
            account_email=account_email or "",
            content_type=content_type or "image/png",
            prompt=(prompt or "")[:500],
            model=model or "",
            source_url=(source_url or "")[:1000],
            created_at=time.time(),
            bytes_len=len(content),
            kind=kind or "generation",
        )
        with self._lock:
            path.write_bytes(content)
            self._index[img_id] = {**asdict(meta), "file": path.name}
            # Keep index bounded so the admin panel stays usable.
            if len(self._index) > 300:
                ordered = sorted(self._index.items(), key=lambda kv: float(kv[1].get("created_at") or 0))
                for old_id, old in ordered[: max(0, len(self._index) - 250)]:
                    try:
                        (self.root / str(old.get("file") or "")).unlink(missing_ok=True)
                    except OSError:
                        pass
                    self._index.pop(old_id, None)
            self._save()
        return meta

    def get_meta(self, img_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._index.get(img_id)
            return dict(item) if item else None

    def get_bytes(self, img_id: str) -> tuple[bytes, str] | None:
        with self._lock:
            item = self._index.get(img_id)
            if not item:
                return None
            path = self.root / str(item.get("file") or "")
            ct = str(item.get("content_type") or "image/png")
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if not data:
            return None
        return data, ct

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._index.values())
        items.sort(key=lambda x: float(x.get("created_at") or 0), reverse=True)
        out = []
        for item in items[: max(1, int(limit or 50))]:
            out.append({
                "id": item.get("id"),
                "account_id": item.get("account_id"),
                "account_email": item.get("account_email"),
                "content_type": item.get("content_type"),
                "prompt": item.get("prompt"),
                "model": item.get("model"),
                "created_at": item.get("created_at"),
                "bytes_len": item.get("bytes_len"),
                "kind": item.get("kind"),
            })
        return out
