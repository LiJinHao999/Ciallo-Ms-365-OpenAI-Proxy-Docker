from __future__ import annotations

import json
import re

from .media_proxy import normalize_m365_media_text

# M365 injects private-use citation markers in streamed and final text.
#
# Observed real-world forms (from production call logs):
#   1. Markdown:   [label](citeturn1search6)
#   2. Bare PUA:   citeturn1search6
#   3. Split residual after a partial delta ate the opener:
#                  turn1search6   /   [^1]turn1search3
#   4. Sydney footnote: [^3^]
#   5. CN bracket id:  【1-1fd57a】 / 【2-ba0dbb】
#
# Strategy: keep human labels, emit stable plain ``[n]`` markers (not Markdown
# ``[^n]`` footnotes — most chat UIs leave the caret form unrendered), map n →
# sourceAttributions[n-1] when the final type=2 payload arrives. Hold
# incomplete openers across deltas so we never emit raw turn/search tokens.
_PUA = "-"
_MARKDOWN_CITE_RE = re.compile(
    rf"\[([^\]]*)\]\(\s*([{_PUA}]*cite[{_PUA}][^)]*)\)",
    re.IGNORECASE,
)
# Require a non-empty ascii id so "cite" alone is NOT consumed (held instead).
_BARE_PUA_CITE_RE = re.compile(
    rf"[{_PUA}]cite[{_PUA}]([A-Za-z0-9_]+)[{_PUA}]?",
    re.IGNORECASE,
)
# Leftover after a split ate the cite opener: "turn1search6" or plain "turn1search6".
_LOOSE_TURN_CITE_RE = re.compile(
    rf"(?:[{_PUA}])?(turn\d+(?:search|file|news|image|video|base|knowledge|gpt|cid|doc)\d+)(?:[{_PUA}])?",
    re.IGNORECASE,
)
# Chinese-bracket web cites used by some M365 answers: 【1-1fd57a】 or 【3】.
_BRACKET_CITE_RE = re.compile(
    r"【\s*(\d{1,3})\s*(?:[-–—]\s*([0-9a-fA-F]{3,16}))?\s*】"
)
_SYDNEY_FOOTNOTE_RE = re.compile(r"\[\^(\d+)\^\]")
_CITE_KEY_NOISE_RE = re.compile(rf"[{_PUA}]+")

# Incomplete tails we must HOLD across stream deltas (not emit raw).
_INCOMPLETE_CITE_TAIL_RE = re.compile(
    rf"(?:"
    rf"\[[^\]]*\]\(\s*[{_PUA}]*cite[{_PUA}][^)]*$"  # open markdown cite
    rf"|[{_PUA}]cite[{_PUA}][A-Za-z0-9_]*$"          # open bare PUA cite
    rf"|[{_PUA}]cite$"
    rf"|[{_PUA}]$"                                    # lone PUA starter
    rf"|\[$"                                          # lone "["
    rf"|\[[^\]]*$"                                    # open "[" label
    rf")",
    re.IGNORECASE,
)


def _normalize_cite_key(raw: str) -> str:
    """Collapse a PUA cite target to a stable ascii key (e.g. ``turn1search0``)."""
    key = _CITE_KEY_NOISE_RE.sub("", raw or "")
    key = re.sub(r"^cite", "", key, flags=re.IGNORECASE)
    key = re.sub(r"[^A-Za-z0-9_]+", "", key).lower()
    return key


def _has_cite_noise(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    if "cite" in lower or "turn" in lower and "search" in lower:
        return True
    if "[^" in text or "【" in text:
        return True
    return any("" <= c <= "" for c in text)


class CitationTracker:
    """Track streamed cite markers and renumber them as ``[n]``.

    Holds incomplete openers across ``clean()`` calls so split deltas cannot
    leak raw ``turn1search6`` / lone PUA bytes / bare ``cite`` to the client.
    """

    def __init__(self) -> None:
        self._key_to_num: dict[str, int] = {}
        self._order: list[str] = []
        self._hold = ""

    def _num_for(self, key: str, preferred: int | None = None) -> int:
        normalized = key or f"anon{len(self._order)}"
        if normalized in self._key_to_num:
            return self._key_to_num[normalized]
        if preferred is not None and preferred > 0 and preferred not in self._key_to_num.values():
            while len(self._order) < preferred - 1:
                placeholder = f"pad{len(self._order)}"
                self._order.append(placeholder)
                self._key_to_num[placeholder] = len(self._order)
            if len(self._order) == preferred - 1:
                self._order.append(normalized)
                self._key_to_num[normalized] = preferred
                return preferred
        self._key_to_num[normalized] = len(self._order) + 1
        self._order.append(normalized)
        return self._key_to_num[normalized]

    @property
    def count(self) -> int:
        return len([k for k in self._order if not k.startswith("pad")])

    @property
    def keys_in_order(self) -> list[str]:
        return [k for k in self._order if not k.startswith("pad")]

    def _split_incomplete(self, text: str) -> tuple[str, str]:
        """Return (safe_prefix, hold_suffix) for cite openers still in progress."""
        if not text:
            return "", ""
        # Longest incomplete tails first. MUST hold partial turn/search tokens
        # like "turn1s" / "turn1search" mid-id, otherwise chunked streams leak them.
        patterns = [
            r"\[[^\]]*\]\(\s*[-]*cite[-][^)]*$",
            r"\[[^\]]*\]\(\s*$",
            r"[-]cite[-][A-Za-z0-9_]*$",
            r"[-]+cite$",
            r"(?:^|[^A-Za-z])cite$",
            # full or partial sydney turn ids, optionally after a footnote
            r"(?:\[\^?\d+\^?\])?turn\d*(?:search|file|news|image|video|base|knowledge|gpt|cid|doc)?[A-Za-z0-9_]*$",
            r"turn\d*[A-Za-z0-9_]*$",
            r"【[^】]*$",
            r"\[\^[^\]]*$",
            r"\[[^\]]*$",
            r"[-]+$",
        ]
        for pat in patterns:
            match = re.search(pat, text, re.IGNORECASE)
            if match and match.end() == len(text):
                start = match.start()
                if pat.startswith("(?:^|[^A-Za-z])"):
                    if start < len(text) and text[start].lower() != "c":
                        start += 1
                # Keep a completed footnote in the safe prefix when the hold is
                # only the trailing turn residual: "[1]turn1s" → ("[1]", "turn1s").
                held = text[start:]
                if held.startswith("[") and "turn" in held.lower():
                    foot = re.match(r"\[\^?\d+\^?\]", held)
                    if foot:
                        start += foot.end()
                return text[:start], text[start:]
        return text, ""

    def clean(self, text: str) -> str:
        """Replace complete cite patterns with label + ``[n]`` markers."""
        if text is None:
            text = ""
        text = f"{self._hold}{text}"
        self._hold = ""
        if not text:
            return ""

        text, self._hold = self._split_incomplete(text)
        if not text:
            return ""
        if not _has_cite_noise(text):
            return text

        def md_repl(match: re.Match[str]) -> str:
            label = (match.group(1) or "").strip()
            key = _normalize_cite_key(match.group(2) or "")
            num = self._num_for(key)
            return f"{label}[{num}]" if label else f"[{num}]"

        def bare_repl(match: re.Match[str]) -> str:
            key = _normalize_cite_key(match.group(1) or match.group(0) or "")
            num = self._num_for(key)
            return f"[{num}]"

        def loose_repl(match: re.Match[str]) -> str:
            key = _normalize_cite_key(match.group(1) or match.group(0) or "")
            num = self._num_for(key)
            return f"[{num}]"

        def bracket_repl(match: re.Match[str]) -> str:
            idx = int(match.group(1))
            suffix = (match.group(2) or "").lower()
            key = f"bracket{idx}-{suffix}" if suffix else f"bracket{idx}"
            num = self._num_for(key, preferred=idx)
            return f"[{num}]"

        cleaned = _MARKDOWN_CITE_RE.sub(md_repl, text)
        cleaned = _BARE_PUA_CITE_RE.sub(bare_repl, cleaned)
        # If a footnote is immediately followed by the same turn token residual
        # (common when a prior delta already emitted [n] and a later delta
        # carries turnNsearchM), drop the residual instead of doubling.
        cleaned = re.sub(
            rf"(\[\^?\d+\^?\])\s*[-]?turn\d+(?:search|file|news|image|video|base|knowledge|gpt|cid|doc)\d+[-]?",
            r"\1",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = _LOOSE_TURN_CITE_RE.sub(loose_repl, cleaned)
        cleaned = _BRACKET_CITE_RE.sub(bracket_repl, cleaned)
        cleaned = _SYDNEY_FOOTNOTE_RE.sub(r"[\1]", cleaned)
        cleaned = re.sub(r"[-]+", "", cleaned)
        cleaned = re.sub(r"[^\S\n]{2,}", " ", cleaned)
        # Re-hold if replacements left a new incomplete tail.
        cleaned, more_hold = self._split_incomplete(cleaned)
        self._hold = more_hold + self._hold
        return cleaned

    def flush(self) -> str:
        """Force-emit any held incomplete tail (best-effort, never leaks tokens)."""
        if not self._hold:
            return ""
        held, self._hold = self._hold, ""
        held = _MARKDOWN_CITE_RE.sub(lambda m: (m.group(1) or "").strip(), held)
        held = _BARE_PUA_CITE_RE.sub("", held)
        held = _LOOSE_TURN_CITE_RE.sub("", held)
        held = _BRACKET_CITE_RE.sub("", held)
        held = re.sub(r"[-]+", "", held)
        held = re.sub(r"turn\d+\w*", "", held, flags=re.I)
        held = re.sub(r"\bcite\b", "", held, flags=re.I)
        held = re.sub(r"\[[^\]]*$", "", held)
        held = re.sub(r"【[^】]*$", "", held)
        return held


def clean_m365_citations(text: str, tracker: CitationTracker | None = None) -> str:
    """Resolve M365 citation markers to plain ``[n]`` markers (or strip-compatible).

    When ``tracker`` is provided, marker numbers are stable across deltas of
    the same turn and incomplete openers are held. Without a tracker each call
    uses a fresh sequence (back-compat for one-shot cleaners / tests).
    """
    active = tracker if tracker is not None else CitationTracker()
    out = active.clean(text)
    if tracker is None:
        out += active.flush()
    return out


# Full-removal patterns for dedupe signatures / fallback comparison only.
# Body streaming uses CitationTracker.clean (keeps labels + [n]); signatures
# must ignore both the opaque form AND the resolved marker form so a
# "URL variant" and a "cite variant" of the same answer still match.
_STRIP_MARKDOWN_CITE_RE = re.compile(
    r"\[[^\]]*\]\(\s*[-]*cite[-][^)]*\)",
    re.IGNORECASE,
)
_STRIP_BARE_PUA_CITE_RE = re.compile(
    r"[-]cite[-][A-Za-z0-9_]*[-]?",
    re.IGNORECASE,
)
_STRIP_LOOSE_TURN_RE = re.compile(
    r"[-]?turn\d+(?:search|file|news|image|video|base|knowledge|gpt|cid|doc)\d+[-]?",
    re.IGNORECASE,
)
_STRIP_BRACKET_CITE_RE = re.compile(
    r"【\s*\d{1,3}\s*(?:[-–—]\s*[0-9a-fA-F]{3,16})?\s*】"
)
# ``[n]`` (legacy) and plain ``[n]`` (current emit form). Cap digits so we do
# not strip arbitrary ``[2026]``-style years / code indexes aggressively in the
# hot path — cite counts from M365 stay small.
_STRIP_FOOTNOTE_RE = re.compile(r"\[\^\d{1,3}\^?\]|\[\d{1,3}\]")


def strip_m365_citations(text: str) -> str:
    """Remove citation markers entirely (labels, PUA targets, footnotes).

    Used by ``_dedupe_signature`` so re-emissions that only swap a raw URL for a
    cite link / footnote are recognized as duplicates. Not used on the streamed
    body — that path wants the human-visible label preserved.
    """
    if not text:
        return ""
    if not (
        "cite" in text.lower()
        or "turn" in text.lower()
        or "[^" in text
        or re.search(r"\[\d{1,3}\]", text) is not None
        or "【" in text
        or any("" <= c <= "" for c in text)
    ):
        return text
    cleaned = _STRIP_MARKDOWN_CITE_RE.sub("", text)
    cleaned = _STRIP_BARE_PUA_CITE_RE.sub("", cleaned)
    cleaned = _STRIP_LOOSE_TURN_RE.sub("", cleaned)
    cleaned = _STRIP_BRACKET_CITE_RE.sub("", cleaned)
    cleaned = _STRIP_FOOTNOTE_RE.sub("", cleaned)
    cleaned = re.sub(r"[-]+", "", cleaned)
    cleaned = re.sub(r"[^\S\n]{2,}", " ", cleaned)
    return cleaned


def _parse_reference_metadata(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _normalize_source_attribution(item: object, *, ref_key: str = "") -> dict | None:
    """Normalize one M365 citation entry into a plain dict.

    Supports both legacy list entries::

        {"providerDisplayName": "...", "seeMoreUrl": "https://...", "referenceMetadata": "{...}"}

    and the current ``messages[].references`` map values (observed 2026-08)::

        {
          "targetLink": "https://news.weather.com.cn/...",
          "displayData": {
            "type": "text/json",
            "renderType": "CITATION",
            "content": "{\"metadata\":{\"referenceId\":\"turn1search10\",\"type\":\"Web\"},
                         \"label\":\"1\",\"providerDisplayName\":\"...\",\"Title\":\"...\",\"snippet\":\"...\"}"
          },
          "isCitedInResponse": true
        }

    ``ref_key`` is the map key (e.g. ``1-1e0595``) used by body markers ``【1-1e0595】``.
    """
    if not isinstance(item, dict):
        return None

    # Newer references-map shape: lift displayData.content JSON up.
    display = item.get("displayData")
    content: dict = {}
    if isinstance(display, dict):
        raw_content = display.get("content")
        if isinstance(raw_content, dict):
            content = raw_content
        elif isinstance(raw_content, str) and raw_content.strip():
            try:
                parsed = json.loads(raw_content)
                if isinstance(parsed, dict):
                    content = parsed
            except (TypeError, ValueError, json.JSONDecodeError):
                content = {}

    meta = _parse_reference_metadata(
        item.get("referenceMetadata")
        or content.get("metadata")
        or item.get("metadata")
        or {}
    )
    # Merge content-level fields as a shallow overlay for title/url lookup.
    merged = {**content, **item}

    title = str(
        merged.get("providerDisplayName")
        or merged.get("Title")
        or merged.get("title")
        or merged.get("sourceName")
        or merged.get("name")
        or meta.get("title")
        or ""
    ).strip()
    url = str(
        merged.get("targetLink")
        or merged.get("seeMoreUrl")
        or merged.get("url")
        or merged.get("sourceUrl")
        or merged.get("displayUrl")
        or merged.get("path")
        or meta.get("url")
        or ""
    ).strip()
    if url.lower() in {"null", "none"}:
        url = ""
    ref_type = str(
        meta.get("type")
        or meta.get("refType")
        or meta.get("typeDescription")
        or meta.get("sourceType")
        or merged.get("refType")
        or ""
    ).strip()
    snippet = str(
        merged.get("snippet")
        or meta.get("snippet")
        or item.get("snippet")
        or ""
    ).strip()
    search_query = str(
        merged.get("searchQuery")
        or meta.get("searchQuery")
        or ""
    ).strip()
    # Ids that body markers may use.
    ref_id = str(
        meta.get("referenceId")
        or meta.get("citationRefId")
        or merged.get("referenceId")
        or ""
    ).strip()
    label = str(merged.get("label") or meta.get("label") or "").strip()
    key = (ref_key or "").strip()
    if not title and not url and not key and not ref_id:
        return None
    if not title:
        title = url or key or ref_id or "来源"
    return {
        "title": title,
        "url": url,
        "type": ref_type,
        "snippet": snippet,
        "search_query": search_query,
        "ref_key": key,          # e.g. 1-1e0595  (matches 【1-1e0595】)
        "ref_id": ref_id,        # e.g. turn1search10 (matches citeturn1search10)
        "label": label,          # e.g. "1"
    }


def extract_source_attributions(value: object) -> list[dict]:
    """Collect normalized citations from a SignalR message / entry tree.

    Walks:
      * legacy ``sourceAttributions`` / ``citations`` lists
      * current ``references`` dicts keyed by ``1-1e0595``-style ids
      * already-resolved ``[label](https://…)`` links in final bot text

    De-duplicates by URL, then by ref_key / ref_id / title.
    """
    found: list[dict] = []
    seen: set[str] = set()
    list_keys = {
        "sourceAttributions",
        "source_attributions",
        "SourceAttributions",
        "citations",
        "Citations",
        "grounding",
    }
    # `references` is a dict in modern payloads; also accept list just in case.
    map_keys = {"references", "References"}

    def add(item: object, *, ref_key: str = "") -> None:
        normalized = _normalize_source_attribution(item, ref_key=ref_key)
        if normalized is None:
            return
        # Prefer stable identity: url > ref_key > ref_id > title
        key = (
            (normalized.get("url") or "").lower()
            or (normalized.get("ref_key") or "").lower()
            or (normalized.get("ref_id") or "").lower()
            or (normalized.get("title") or "")
        )
        if not key or key in seen:
            # If we already have this key but the new entry has a URL and the
            # old one doesn't, upgrade in place.
            if key and key in seen and normalized.get("url"):
                for existing in found:
                    ek = (
                        (existing.get("url") or "").lower()
                        or (existing.get("ref_key") or "").lower()
                        or (existing.get("ref_id") or "").lower()
                        or (existing.get("title") or "")
                    )
                    if ek == key and not existing.get("url"):
                        existing.update({k: v for k, v in normalized.items() if v})
                        break
            return
        seen.add(key)
        found.append(normalized)

    def add_inline_markdown_links(text: str) -> None:
        if not text or "http" not in text:
            return
        for match in re.finditer(r"\[([^\]]{0,200})\]\((https?://[^)\s]+)\)", text):
            label = (match.group(1) or "").strip() or match.group(2)
            if label.startswith("!") or "m365-media?" in match.group(2):
                continue
            add({"providerDisplayName": label, "seeMoreUrl": match.group(2), "refType": "Web"})

    def walk(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        for key in list_keys:
            attrs = node.get(key)
            if isinstance(attrs, list):
                for item in attrs:
                    add(item)
            elif isinstance(attrs, dict):
                # Sometimes a single attribution object.
                if any(k in attrs for k in ("seeMoreUrl", "providerDisplayName", "url", "targetLink")):
                    add(attrs)
                else:
                    for rk, rv in attrs.items():
                        add(rv, ref_key=str(rk))
        for key in map_keys:
            refs = node.get(key)
            if isinstance(refs, dict):
                for rk, rv in refs.items():
                    add(rv, ref_key=str(rk))
            elif isinstance(refs, list):
                for item in refs:
                    add(item)
        if node.get("author") not in (None, "user") and isinstance(node.get("text"), str):
            add_inline_markdown_links(node["text"])
        if isinstance(node.get("hiddenText"), str):
            add_inline_markdown_links(node["hiddenText"])
        for child in node.values():
            if isinstance(child, (dict, list)):
                walk(child)

    walk(value)
    return found


def match_sources_for_tracker(
    sources: list[dict],
    tracker: "CitationTracker | None",
) -> list[dict]:
    """Order/filter sources to match in-body ``[n]`` numbering.

    Body markers are rewritten to ``[n]`` from, in priority order:
      * ``【1-1e0595】`` → tracker key ``bracket1-1e0595`` / raw ``1-1e0595``
      * ``citeturn1search10`` → tracker key ``turn1search10``
    Upstream ``references`` carry both ``ref_key`` (1-1e0595) and ``ref_id``
    (turn1search10). Match them so footnote 1 points at the right URL.
    """
    usable = [s for s in sources if isinstance(s, dict) and (s.get("title") or s.get("url") or s.get("ref_key"))]
    if not usable:
        return []
    if tracker is None or not tracker.keys_in_order:
        # No body footnotes recorded — keep upstream order, drop URL-less noise.
        return [s for s in usable if s.get("url") or s.get("title")]

    by_ref_key: dict[str, dict] = {}
    by_ref_id: dict[str, dict] = {}
    by_label: dict[str, dict] = {}
    for s in usable:
        rk = str(s.get("ref_key") or "").lower()
        rid = str(s.get("ref_id") or "").lower()
        lab = str(s.get("label") or "").strip()
        if rk:
            by_ref_key[rk] = s
            # Also index without thinking about "bracket" prefix the tracker uses.
            if rk.startswith("bracket"):
                by_ref_key[rk[len("bracket"):]] = s
            else:
                by_ref_key[f"bracket{rk}"] = s
        if rid:
            by_ref_id[rid] = s
        if lab:
            by_label[lab] = s

    ordered: list[dict] = []
    used: set[int] = set()
    for key in tracker.keys_in_order:
        k = (key or "").lower()
        hit = by_ref_key.get(k) or by_ref_id.get(k)
        if hit is None and k.startswith("bracket"):
            # bracket1-1e0595 or bracket1
            rest = k[len("bracket"):]
            hit = by_ref_key.get(rest) or by_ref_key.get(k)
            if hit is None and re.fullmatch(r"\d+", rest or ""):
                hit = by_label.get(rest)
        if hit is None and re.fullmatch(r"bracket\d+-[0-9a-f]+", k):
            # direct ref_key form
            hit = by_ref_key.get(k) or by_ref_key.get(k.split("bracket", 1)[-1])
        if hit is not None and id(hit) not in used:
            ordered.append(hit)
            used.add(id(hit))

    # Append any remaining sources with real URLs that were not cited inline.
    for s in usable:
        if id(s) not in used and s.get("url"):
            ordered.append(s)
            used.add(id(s))
    return ordered


def align_sources_with_tracker(sources: list[dict], tracker: CitationTracker | None) -> list[dict]:
    """Back-compat wrapper — prefer ``match_sources_for_tracker``."""
    return match_sources_for_tracker(sources, tracker)


def format_sources_markdown(sources: list[dict], *, heading: str = "### \u53c2\u8003\u6765\u6e90") -> str:
    """Render source attributions as a trailing Markdown block for clients.

    Returns an empty string when there is nothing useful to show. Each entry is
    a numbered markdown link when a URL is present, otherwise plain text, with
    an optional type hint (Web / Outlook / \u2026). Numbers match in-body ``[n]``.
    """
    usable = [s for s in sources if isinstance(s, dict) and (s.get("title") or s.get("url"))]
    if not usable:
        return ""
    lines = ["", "", "---", "", heading, ""]
    for index, source in enumerate(usable, 1):
        title = str(source.get("title") or source.get("url") or f"\u6765\u6e90 {index}").strip()
        url = str(source.get("url") or "").strip()
        ref_type = str(source.get("type") or "").strip()
        snippet = str(source.get("snippet") or "").strip()
        if len(snippet) > 120:
            snippet = snippet[:117].rstrip() + "\u2026"
        if url:
            safe_title = title.replace("[", "\\[").replace("]", "\\]")
            line = f"{index}. [{safe_title}]({url})"
        else:
            line = f"{index}. {title}"
        if ref_type:
            line += f" \uff08{ref_type}\uff09"
        lines.append(line)
        if snippet and snippet not in title:
            lines.append(f"   > {snippet}")
    lines.append("")
    return "\n".join(lines)


def aligned_url_sources_for_stream(
    sources: list[dict],
    tracker: CitationTracker | None = None,
) -> list[dict]:
    """Return URL-bearing sources ordered to match in-body ``[n]`` markers.

    URL-less placeholders are dropped: protocol-native citation fields and the
    Markdown bibliography both need a real link to be useful.
    """
    aligned = match_sources_for_tracker(sources, tracker)
    return [s for s in aligned if isinstance(s, dict) and str(s.get("url") or "").strip()]


def _footnote_span(text: str, index: int) -> tuple[int, int] | None:
    """Return ``[start, end)`` for the first ``[index]`` (or legacy ``[^index]``) marker."""
    if index <= 0 or not text:
        return None
    for marker in (f"[{index}]", f"[^{index}]"):
        start = text.find(marker)
        if start >= 0:
            return start, start + len(marker)
    return None


def openai_url_citations_from_sources(text: str, sources: list[dict]) -> list[dict]:
    """Map normalized sources to OpenAI Responses ``url_citation`` annotations.

    ``start_index`` / ``end_index`` prefer the in-body ``[n]`` marker span so
    clients can highlight the cite marker. When a marker is missing the span is
    placed at EOF (zero-width) so the annotation still carries url/title.
    """
    body = text or ""
    annotations: list[dict] = []
    for index, source in enumerate(sources, 1):
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        if not url:
            continue
        title = str(source.get("title") or url).strip() or url
        span = _footnote_span(body, index)
        if span is None:
            start = end = len(body)
        else:
            start, end = span
        annotations.append(
            {
                "type": "url_citation",
                "start_index": start,
                "end_index": end,
                "url": url,
                "title": title,
            }
        )
    return annotations


def anthropic_web_citations_from_sources(text: str, sources: list[dict]) -> list[dict]:
    """Map normalized sources to Anthropic ``web_search_result_location`` citations.

    These hang on a text content block's ``citations`` array (and as streaming
    ``citations_delta`` events). ``encrypted_index`` is an opaque required field
    in the official SDK; we emit a stable synthetic token per source.
    """
    body = text or ""
    citations: list[dict] = []
    for index, source in enumerate(sources, 1):
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        if not url:
            continue
        title = str(source.get("title") or url).strip() or url
        snippet = str(source.get("snippet") or "").strip()
        span = _footnote_span(body, index)
        if span is not None:
            cited_text = body[span[0]:span[1]]
        elif snippet:
            cited_text = snippet[:200]
        else:
            cited_text = title
        citations.append(
            {
                "type": "web_search_result_location",
                "url": url,
                "title": title,
                "cited_text": cited_text,
                # Opaque per Anthropic SDK; not a real encrypted payload.
                "encrypted_index": f"m365_{index}",
            }
        )
    return citations


def sources_markdown_for_stream(
    streamed_text: str,
    sources: list[dict],
    tracker: CitationTracker | None = None,
) -> str:
    """Return a trailing sources block with real URLs when available.

    Only emits URL-less placeholder rows when we truly have nothing better
    (no ``references`` / ``sourceAttributions`` at all). Matching prefers
    ``ref_key`` (【1-1e0595】) and ``ref_id`` (turn1searchN) against the
    in-body marker order recorded by ``CitationTracker``.
    """
    aligned = aligned_url_sources_for_stream(sources, tracker)
    body = streamed_text or ""
    has_footnotes = bool(re.search(r"\[\^?\d{1,3}\^?\]", body))

    if not aligned:
        return ""

    block = format_sources_markdown(aligned)
    if not block:
        return ""
    urls = [str(s.get("url") or "").strip() for s in aligned if isinstance(s, dict)]
    urls = [u for u in urls if u]
    if urls and all(u in body for u in urls) and not has_footnotes:
        return ""
    if "参考来源" in body and not urls and not has_footnotes:
        return ""
    return block


def _capture_suspicious_response_event(sink, msg: dict) -> None:
    if sink is None:
        return
    try:
        probe = json.dumps(msg, ensure_ascii=False).lower()
    except (TypeError, ValueError):
        return
    if any(
        key in probe
        for key in (
            "image",
            "card",
            "render",
            "attachment",
            "contenturl",
            "downloadurl",
            "filetoken",
            "thumbnail",
            "generatedgraphic",
            "generatedaudio",
            "asyncgw",
            "citation",
        )
    ):
        sink(msg)


def _dedupe_signature(text: str) -> str:
    # Strip (not resolve) citations so URL-vs-cite re-emissions share a signature.
    # Observed near-duplicate finals differ only by cite/link presentation, e.g.:
    #   streamed:  类似 `[[1]](url)` 的来源链接
    #   fallback:  类似 `url` 的来源链接
    # or bare backticks vs markdown links vs resolved [n] markers. Treating all
    # of those as noise keeps the prose signature stable across re-emissions.
    normalized = strip_m365_citations(text)
    normalized = re.sub(r"\[\[[^\]]*\]\([^)]*\)", "", normalized)  # [[1]](url)
    normalized = re.sub(r"\[[^\]]+\]\([^)]*\)", "", normalized)     # [text](any)
    normalized = re.sub(r"`[^`]+`", "", normalized)                 # `url` / `[[1]](url)`
    normalized = re.sub(r"https?://\S+", "", normalized)            # bare URLs
    normalized = re.sub(r"\[\^?\d{1,3}\^?\]", "", normalized)       # resolved [n] / [^n]
    normalized = re.sub(r"\s+", "", normalized)
    return normalized


def _strip_stream_edge_noise(text: str) -> str:
    """Trim trailing whitespace and a lone dangling ``\\`` often left by a cut delta."""
    return text.rstrip(" \t\r\n\\")


def _unsent_fallback_suffix(streamed_text: str, fallback_text: str) -> str | None:
    """Best-effort recovery of the fallback tail that has not been streamed yet.

    Used when raw ``startswith`` fails because of cite/link markup drift or a
    dangling stream-edge character, but the bodies still largely align. Returns
    ``None`` when alignment is too weak to trust.
    """
    from difflib import SequenceMatcher

    streamed = _strip_stream_edge_noise(streamed_text)
    if not streamed:
        return fallback_text
    if fallback_text.startswith(streamed):
        return fallback_text[len(streamed):]

    matcher = SequenceMatcher(None, streamed, fallback_text, autojunk=False)
    blocks = [(a, b, size) for a, b, size in matcher.get_matching_blocks() if size > 0]
    if not blocks:
        return None
    matched = sum(size for _, _, size in blocks)
    # Require most of the streamed body to appear in the fallback, otherwise we
    # risk inventing a "suffix" from an unrelated final.
    if matched < int(0.80 * len(streamed)):
        return None
    # Take the match that advances farthest through the streamed text; the
    # fallback index just after that block is the unsent tail start.
    a_idx, b_idx, size = max(blocks, key=lambda item: item[0] + item[2])
    if a_idx + size < int(0.85 * len(streamed)):
        return None
    return fallback_text[b_idx + size:]


def _meaningful_remainder(text: str) -> str:
    """Keep a recovered tail only when it still has real prose after cite/link strip."""
    if not text:
        return ""
    if not _dedupe_signature(text):
        return ""
    return text


def _final_fallback_remainder(streamed_text: str, fallback_text: str) -> str:
    """Final (t==3) reconciliation ONLY: return the tail of the whole fallback
    answer that has not been streamed yet.

    This compares the ENTIRE streamed-so-far text against the ENTIRE fallback
    answer and is meant to run exactly once, after the stream ends. Do NOT use
    it as a per-delta guard: its ``fallback in streamed`` / signature-subset
    branches would drop small repeated fragments (``2a_1``, a closing ``}``)
    and corrupt formulas or code. Per-delta dedupe belongs in
    ``_dedupe_repeated_delta``.

    Near-duplicate / signature-subset guards must NOT discard a fallback that
    still carries an unsent prose tail (production: long essay cut at
    ``学习顺序是：`` while type=2 held the numbered list).
    """
    if not fallback_text:
        return ""
    if not streamed_text:
        return fallback_text
    if fallback_text.startswith(streamed_text):
        return fallback_text[len(streamed_text):]
    # Stream often ends mid-control-char / trailing backslash while type=2 is
    # the clean full answer; treat that edge noise as already consumed.
    streamed_clean = _strip_stream_edge_noise(streamed_text)
    if streamed_clean and fallback_text.startswith(streamed_clean):
        return fallback_text[len(streamed_clean):]
    if fallback_text in streamed_text:
        return ""
    streamed_sig = _dedupe_signature(streamed_text)
    fallback_sig = _dedupe_signature(fallback_text)
    if streamed_sig and fallback_sig:
        if streamed_sig == fallback_sig:
            return ""
        # Fallback already fully covered by what we streamed (possibly with
        # extra stream-only noise) — nothing left to emit.
        if fallback_sig in streamed_sig:
            return ""
        # Streamed body is a prefix (modulo cite/URL noise) of the final. Old
        # code returned "" here and swallowed real endings such as a trailing
        # numbered list. Recover the unsent suffix instead.
        if streamed_sig in fallback_sig:
            recovered = _unsent_fallback_suffix(streamed_text, fallback_text)
            if recovered is None:
                return ""
            return _meaningful_remainder(recovered)
    # Near-duplicate finals: same essay with slightly different cite markup that
    # still leaves a tiny signature residual after stripping. Only engage for
    # long answers so short legitimate revisions are not swallowed — and only
    # drop when there is no meaningful unsent tail.
    if (
        streamed_sig
        and fallback_sig
        and len(streamed_sig) >= 200
        and len(fallback_sig) >= 200
        and abs(len(streamed_sig) - len(fallback_sig))
        <= max(40, int(0.05 * max(len(streamed_sig), len(fallback_sig))))
    ):
        from difflib import SequenceMatcher

        ratio = SequenceMatcher(None, streamed_sig, fallback_sig, autojunk=False).ratio()
        if ratio >= 0.97:
            recovered = _unsent_fallback_suffix(streamed_text, fallback_text)
            if recovered is None:
                return ""
            remainder = _meaningful_remainder(recovered)
            # Ignore tiny markup-only drift; keep real prose tails (lists, etc.).
            if not remainder:
                return ""
            if _dedupe_signature(remainder) in streamed_sig:
                return ""
            # Short residual with almost-equal signatures is usually cite churn;
            # require either a longer tail or a clear length advantage on fallback.
            rem_sig = _dedupe_signature(remainder)
            if len(rem_sig) < 8 and len(fallback_sig) <= len(streamed_sig) + 20:
                return ""
            return remainder
    return fallback_text


def _dedupe_repeated_delta(streamed_text: str, delta: str) -> str:
    r"""Per-delta guard for the SSE response layer (response_helpers).

    ``chat_stream`` already yields a deduplicated incremental stream (the t==3
    fallback reconciliation happens inside substrate_client). As defense in
    depth the response layer must still drop a delta that RE-EMITS the entire
    answer so far -- observed with media answers, where the model restates the
    whole message swapping a raw backtick-wrapped URL for a
    ``[text](cite...)`` link.

    It must NOT drop a delta merely because its short text already appeared
    earlier: math and code answers legitimately repeat tokens such as ``2a_1``,
    ``+ 3d = 6`` or a closing ``}`` across separate deltas. Dropping those
    corrupts formulas (``\\frac{8}{2}(2a_1+7d)`` losing ``2a_1``) and silently
    deletes code.

    Rule: drop the delta only when its dedupe-signature is a SUPERSET of the
    whole streamed-so-far signature (the delta reproduces everything already
    emitted, modulo URL/citation noise). Incremental fragments never satisfy
    this because their signature is a small subset, not a superset.
    """
    if not delta or not streamed_text:
        return delta
    streamed_sig = _dedupe_signature(streamed_text)
    delta_sig = _dedupe_signature(delta)
    if streamed_sig and delta_sig and streamed_sig in delta_sig:
        return ""
    return delta


# Message types that can never be the final answer body. M365 sends these as
# regular ``messages`` entries in type=1/type=2 payloads; treating their text as
# a fallback answer leaks chain-of-thought / search progress / safety refusals
# ("Hmm...it looks like I can't chat about this...") onto the end of the reply.
_NON_BODY_MESSAGE_TYPES = {
    "Progress",            # incl. ChainOfThoughtSummary + EarlyProgress
    "ReferencesListComplete",
    "InternalLoaderMessage",
    "SearchQuery",
    "InternalSearchQuery",
    "AdsQuery",
    "SemanticSerp",
    "GenerateContentQuery",
    "GenerateGraphicArt",
    "Suggestion",
    "Disengaged",          # Copilot safety refusal
    "ConfirmationCard",
    "HintInvocation",
    "EndOfRequest",
    "EscapeHatch",
    "DeveloperLogs",
    "AuthError",
    "MemoryUpdate",
    "RenderCardRequest",
}


def is_body_message(entry: dict) -> bool:
    """True when ``entry`` may carry the final answer text.

    Real body messages (contentOrigin ``DeepLeo``) have NO ``messageType``
    field, so the whitelist-by-default here cannot misclassify them; every
    progress / search / refusal type is excluded explicitly.
    """
    return str(entry.get("messageType") or "").strip() not in _NON_BODY_MESSAGE_TYPES


def is_chain_of_thought_message(entry: dict) -> bool:
    """True for M365 deep-think chain-of-thought summaries.

    Deep-thinking tones (``Gpt_5_6_Reasoning`` / ``Copilot_深度思考``) stream
    these as ``messageType: Progress`` + ``contentOrigin: ChainOfThoughtSummary``
    entries, e.g. ``**Refusing unauthorized actions** I'm focusing on ...``.
    They must be surfaced as structured reasoning, never as fallback body text.
    """
    return (
        str(entry.get("messageType") or "") == "Progress"
        and str(entry.get("contentOrigin") or "") == "ChainOfThoughtSummary"
    )


def _message_content(entry: dict) -> str:
    text = clean_m365_citations(normalize_m365_media_text(str(entry.get("text") or "")))
    image_urls = _extract_image_urls(entry)
    if image_urls and _is_image_loading_placeholder(text):
        text = ""
    image_markdown = [_image_markdown(url) for url in image_urls]
    parts = [part for part in [text, *image_markdown] if part]
    return "\n\n".join(parts)


def _is_image_loading_placeholder(text: str) -> bool:
    return text.strip().lower() == "loading image"


def _image_markdown(url: str) -> str:
    return f"![image]({url})"


def _extract_image_urls(value: object) -> list[str]:
    urls: list[str] = []

    def add(url: object) -> None:
        if not isinstance(url, str):
            return
        cleaned = url.strip().strip("`").strip()
        if not cleaned.startswith(("http://", "https://")):
            return
        if cleaned not in urls:
            urls.append(cleaned)

    def walk(node: object, image_context: bool = False) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, image_context)
            return
        if isinstance(node, str):
            if image_context:
                add(node)
            return
        if not isinstance(node, dict):
            return

        type_value = str(node.get("type") or node.get("contentType") or node.get("mediaType") or "").lower()
        kind_value = str(node.get("kind") or node.get("role") or "").lower()
        local_image_context = image_context or type_value == "image" or type_value.startswith("image/") or "image" in kind_value

        for key in ("url", "contentUrl", "source", "src", "imageUrl", "thumbnailUrl"):
            if key in node and local_image_context:
                add(node.get(key))

        for key, child in node.items():
            key_image_context = local_image_context or key in {"adaptiveCards", "attachments", "images", "image", "thumbnail", "previewImage"}
            walk(child, key_image_context)

    walk(value)
    return urls


def _combine_text(prompt: str, context: list[str]) -> str:
    if not context:
        return prompt
    has_tools = any("tool_call" in c for c in context)
    result = "\n\n".join(context) + "\n\n---\n\n" + prompt
    if has_tools:
        result += (
            "\n\n[FORMAT] Respond with a ```tool_call``` JSON block for any file action. "
            "Example: ```tool_call\n"
            '{"name": "Write", "arguments": {"file_path": "S:/path/file.ext", "content": "..."}}\n'
            "``` No other output format is valid for file operations.[/FORMAT]"
        )
    return result
