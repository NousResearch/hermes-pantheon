"""Memory Wiki aggregation — deterministic subject/daily-log derivation over session history.

Originally ``hermes_cli/memory_wiki.py`` by Abdullah Raja (@Araja119, hermes-agent PR #31244),
salvaged in #89940 and moved into this standalone plugin. Stays stdlib-only: the helpers are
deterministic heuristics over the session/message dictionaries ``hermes_state.SessionDB``
returns (``list_sessions_rich`` / ``get_messages``); no LLM calls, no writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re


@dataclass(frozen=True)
class SubjectCandidate:
    """A possible subject extracted from session metadata or messages."""

    name: str
    slug: str
    score: int = 1
    source: str = "text"
    user_supported: bool = False


_STOPWORDS = {
    "a",
    "about",
    "add",
    "agent",
    "an",
    "and",
    "are",
    "as",
    "assistant",
    "be",
    "build",
    "can",
    "chat",
    "conversation",
    "debug",
    "discuss",
    "do",
    "for",
    "from",
    "help",
    "hermes",
    "i",
    "in",
    "into",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "please",
    "session",
    "that",
    "the",
    "this",
    "to",
    "want",
    "we",
    "work",
    "with",
    "you",
}

_PACKAGE_NAMES = {
    "django",
    "fastapi",
    "flask",
    "hermes-agent",
    "nextjs",
    "nodejs",
    "pytest",
    "react",
    "ruff",
    "svelte",
    "vite",
    "vue",
}

_PATH_RE = re.compile(
    r"(?<![\w.-])(?:[~.]?/)?(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+(?:\.[A-Za-z0-9]+)?"
)
_CAMEL_CASE_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")
_TITLE_CASE_PHRASE_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:\s+[A-Z][a-z0-9]+){1,3}\b")
_QUOTED_PHRASE_RE = re.compile(r"[\"'“‘]([^\"'”’]{3,80})[\"'”’]")
_SLASH_COMMAND_RE = re.compile(r"(?<!\S)/[A-Za-z][A-Za-z0-9_-]*\b")
_TOOL_NAME_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:[_.][A-Za-z_][A-Za-z0-9_]*)+\b")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_PACKAGE_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(name) for name in sorted(_PACKAGE_NAMES, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)
_REPO_STYLE_RE = re.compile(r"\b[a-z][a-z0-9]+(?:-[a-z0-9]+)+\b")


def slugify_subject(name: str) -> str:
    """Return a stable URL slug for a subject name."""

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "subject"


def extract_subject_candidates(session: dict, messages: list[dict]) -> list[SubjectCandidate]:
    """Extract deterministic subject candidates from a session and its messages.

    Only user content and the title introduce or score topics. User evidence
    ranks first; assistant content is used separately for matching context.
    """

    candidates: dict[str, SubjectCandidate] = {}

    def add(name: str, *, score: int = 1, source: str = "text") -> None:
        cleaned = _clean_candidate_name(name)
        if not cleaned:
            return
        slug = slugify_subject(cleaned)
        existing = candidates.get(slug)
        if existing is None:
            candidates[slug] = SubjectCandidate(cleaned, slug, score, source, source != "title")
        else:
            candidates[slug] = SubjectCandidate(
                existing.name,
                existing.slug,
                existing.score + score,
                existing.source,
                existing.user_supported or source != "title",
            )

    title = str(session.get("title") or "")
    if title:
        title_phrase = _keyword_phrase(title)
        if title_phrase:
            add(title_phrase, score=6, source="title")

    for message in messages:
        if message.get("role") != "user":
            continue
        content = _message_text(message)
        _extract_from_text(content, add, base_score=3)
        phrase = _keyword_phrase(content)
        if phrase:
            add(phrase, score=3, source="user_phrase")

    return sorted(candidates.values(), key=lambda c: (
        not c.user_supported, -c.score, c.name.lower(),
    ))


def _extract_from_text(text: str, add, *, base_score: int) -> None:
    for match in _TOOL_NAME_RE.findall(text):
        add(match, score=base_score + 1, source="identifier")
    for match in _PATH_RE.findall(text):
        add(match, score=base_score + 3, source="path")
    for match in _QUOTED_PHRASE_RE.findall(text):
        phrase = _keyword_phrase(match)
        if phrase:
            add(phrase, score=base_score + 2, source="quote")
    for match in _SLASH_COMMAND_RE.findall(text):
        add(match, score=base_score + 1, source="slash_command")
    for match in _TITLE_CASE_PHRASE_RE.findall(text):
        phrase = _keyword_phrase(match)
        if phrase:
            add(phrase, score=base_score + 2, source="title_case_phrase")
    for match in _CAMEL_CASE_RE.findall(text):
        add(match, score=base_score + 1, source="identifier")
    for match in _PACKAGE_RE.findall(text):
        add(match.lower(), score=base_score + 2, source="package")
    for match in _REPO_STYLE_RE.findall(text):
        add(match, score=base_score + 2, source="package")


def _keyword_phrase(text: str) -> str:
    normalized = re.sub(r"(?<=[A-Za-z0-9])[-_](?=[A-Za-z0-9])", " ", text)
    words = [word.lower() for word in _WORD_RE.findall(normalized)]
    words = [word for word in words if word not in _STOPWORDS and len(word) > 1]
    if len(words) < 2:
        return ""
    return " ".join(words[:4])


def _clean_candidate_name(name: str) -> str:
    cleaned = " ".join(str(name).strip().split())
    if not cleaned:
        return ""
    if cleaned.lower() in _STOPWORDS:
        return ""
    return cleaned


def build_memory_subjects(db, *, limit: int = 100, query: str | None = None) -> list[dict]:
    """Aggregate subject dictionaries from recent ``SessionDB`` sessions."""

    if limit <= 0:
        return []
    sessions = _load_memory_sessions(db, limit=max(limit * 5, limit, 100))
    subjects: dict[str, dict] = {}

    for session, messages in sessions:
        session_info = _session_info(session)
        candidates = extract_subject_candidates(session, messages)
        if not candidates:
            continue
        message_count = len(messages)
        first_seen = _first_timestamp(session, messages)
        last_seen = _last_timestamp(session, messages)
        snippets_by_slug = _snippets_for_candidates(session, messages, candidates)

        for candidate in candidates:
            subject = subjects.setdefault(
                candidate.slug,
                {
                    "slug": candidate.slug,
                    "name": candidate.name,
                    "keywords": [],
                    "session_count": 0,
                    "message_count": 0,
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                    "sessions": [],
                    "snippets": [],
                    "_score": 0,
                    "_user_supported": False,
                    "_session_ids": set(),
                },
            )
            subject["_score"] += candidate.score
            subject["_user_supported"] |= candidate.user_supported
            if candidate.name not in subject["keywords"]:
                subject["keywords"].append(candidate.name)
            subject["first_seen"] = min(subject["first_seen"], first_seen)
            subject["last_seen"] = max(subject["last_seen"], last_seen)
            if session_info["id"] not in subject["_session_ids"]:
                subject["_session_ids"].add(session_info["id"])
                subject["sessions"].append(session_info)
                subject["session_count"] += 1
                subject["message_count"] += message_count
            subject["snippets"].extend(snippets_by_slug.get(candidate.slug, []))

    ranked = sorted(subjects.values(), key=lambda s: (
        not s["_user_supported"], -s["_score"], -s["session_count"], s["name"].lower(),
    ))
    results = [_finalize_subject(subject) for subject in ranked]
    if query:
        needle = query.lower().strip()
        results = [
            subject
            for subject in results
            if needle in subject["slug"]
            or needle in subject["name"].lower()
            or any(needle in keyword.lower() for keyword in subject["keywords"])
        ]

    return results[:limit]


def get_memory_subject(db, slug: str) -> dict | None:
    """Return one aggregated memory subject by slug, or ``None``."""

    normalized = slugify_subject(slug)
    for subject in build_memory_subjects(db, limit=1000):
        if subject["slug"] == normalized:
            return subject
    return None


def build_daily_logs(db, *, limit_days: int = 60) -> list[dict]:
    """Aggregate recent sessions into API-ready day log dictionaries."""

    if limit_days <= 0:
        return []
    sessions = _load_memory_sessions(db, limit=max(limit_days * 25, 100))
    days: dict[str, dict] = {}

    for session, messages in sessions:
        day_key = _date_key(float(session.get("started_at") or _first_timestamp(session, messages)))
        started = float(session.get("started_at") or _first_timestamp(session, messages))
        last_active = _last_timestamp(session, messages)
        day = days.setdefault(
            day_key,
            {
                "date": day_key,
                "started_at_min": started,
                "last_active_max": last_active,
                "session_count": 0,
                "message_count": 0,
                "subjects": [],
                "sessions": [],
                "work_items": [],
                "_subjects": {},
            },
        )
        day["started_at_min"] = min(day["started_at_min"], started)
        day["last_active_max"] = max(day["last_active_max"], last_active)
        day["session_count"] += 1
        day["message_count"] += len(messages)
        day["sessions"].append(_session_info(session))

        for candidate in extract_subject_candidates(session, messages):
            entry = day["_subjects"].setdefault(
                candidate.slug,
                {"slug": candidate.slug, "name": candidate.name, "count": 0,
                 "_user_supported": False, "_score": 0},
            )
            entry["count"] += 1
            entry["_user_supported"] |= candidate.user_supported
            entry["_score"] += candidate.score

        day["work_items"].extend(_work_items_for_session(session, messages))

    logs = []
    for day in days.values():
        subjects = list(day.pop("_subjects").values())
        subjects.sort(key=lambda s: (
            not s["_user_supported"], -s["_score"], -s["count"], s["name"].lower(),
        ))
        for subject in subjects:
            subject.pop("_user_supported")
            subject.pop("_score")
        day["subjects"] = subjects[:20]
        day["sessions"].sort(key=lambda s: (-s["last_active"], s["id"]))
        day["work_items"].sort(key=lambda w: (w["timestamp"], w["session_id"], w["text"]))
        logs.append(day)

    logs.sort(key=lambda d: d["date"], reverse=True)
    return logs[:limit_days]


def get_daily_log(db, date: str) -> dict | None:
    """Return one daily memory log by ``YYYY-MM-DD`` date, or ``None``."""

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        return None
    for day in build_daily_logs(db, limit_days=1000):
        if day["date"] == date:
            return day
    return None


def build_memory_notes(memory_dir: Path | None = None) -> dict:
    """Return the curated persistent memory entries (MEMORY.md / USER.md), read-only.

    Every derived wiki view should link back to the memory the agent actually carries so the
    user can audit and prune it. Files are parsed with the same public entry delimiter the
    ``memory`` tool writes (``tools.memory_tool.ENTRY_DELIMITER``), so the wiki never disagrees
    with the system prompt about what an "entry" is. Edits stay with the ``memory`` tool.
    """

    from tools.memory_tool import ENTRY_DELIMITER, get_memory_dir

    root = memory_dir if memory_dir is not None else get_memory_dir()
    notes: dict[str, list[str]] = {}
    for target, filename in (("memory", "MEMORY.md"), ("user", "USER.md")):
        notes[target] = _read_memory_entries(root / filename, ENTRY_DELIMITER)
    return {
        "memory": notes["memory"],
        "user": notes["user"],
        "memory_count": len(notes["memory"]),
        "user_count": len(notes["user"]),
    }


def _read_memory_entries(path: Path, delimiter: str) -> list[str]:
    """Stripped, non-empty entries of one memory file; ``[]`` when missing or unreadable.
    ``utf-8-sig`` drops a Notepad BOM off the first entry, matching the memory tool's reader."""

    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    return [entry for entry in (chunk.strip() for chunk in raw.split(delimiter)) if entry]


def build_memory_overview(db, *, subject_limit: int = 50, day_limit: int = 30) -> dict:
    """Return overview payload for Memory Wiki dashboard/API consumers."""

    recent = db.list_sessions_rich(
        limit=10,
        include_children=False,
        order_by_last_active=True,
    )
    return {
        "subjects": build_memory_subjects(db, limit=subject_limit),
        "daily_logs": build_daily_logs(db, limit_days=day_limit),
        "recent_sessions": [_session_info(session) for session in recent],
        "notes": build_memory_notes(),
    }


def _load_memory_sessions(db, *, limit: int) -> list[tuple[dict, list[dict]]]:
    sessions = db.list_sessions_rich(
        limit=limit,
        include_children=False,
        order_by_last_active=True,
    )
    return [(session, db.get_messages(session["id"])) for session in sessions]


def _session_info(session: dict) -> dict:
    return {
        "id": session.get("id"),
        "title": session.get("title"),
        "source": session.get("source"),
        "started_at": float(session.get("started_at") or 0),
        "last_active": float(session.get("last_active") or session.get("started_at") or 0),
        "preview": session.get("preview") or "",
    }


def _first_timestamp(session: dict, messages: list[dict]) -> float:
    values = [float(m["timestamp"]) for m in messages if m.get("timestamp") is not None]
    values.append(float(session.get("started_at") or 0))
    return min(values)


def _last_timestamp(session: dict, messages: list[dict]) -> float:
    values = [float(m["timestamp"]) for m in messages if m.get("timestamp") is not None]
    values.append(float(session.get("last_active") or session.get("started_at") or 0))
    return max(values)


def _date_key(timestamp: float) -> str:
    """Return the local-calendar date key for ``timestamp``."""

    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        content = " ".join(
            block["text"] for block in content
            if isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    if isinstance(content, str):
        return " ".join(content.split())
    return ""


def _snippets_for_candidates(
    session: dict,
    messages: list[dict],
    candidates: list[SubjectCandidate],
) -> dict[str, list[dict]]:
    snippets: dict[str, list[dict]] = {candidate.slug: [] for candidate in candidates}
    for message in messages:
        if message.get("role") not in ("user", "assistant"):
            continue
        text = _message_text(message)
        for candidate in candidates:
            if _matches_candidate(text, candidate):
                snippets[candidate.slug].append(_snippet(session, message, text))

    return snippets


def _matches_candidate(text: str, candidate: SubjectCandidate) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not normalized:
        return False
    if f"-{candidate.slug}-" in f"-{normalized}-":
        return True
    # Normalize whole-message, quoted and title-case phrases as extraction does.
    texts = [text, *_QUOTED_PHRASE_RE.findall(text), *_TITLE_CASE_PHRASE_RE.findall(text)]
    for phrase_text in texts:
        phrase = _keyword_phrase(phrase_text)
        if phrase and candidate.slug == slugify_subject(phrase):
            return True
    return False


def _snippet(session: dict, message: dict, text: str) -> dict:
    return {
        "message_id": message.get("id"),
        "session_id": session.get("id"),
        "role": message.get("role"),
        "timestamp": float(message.get("timestamp") or session.get("started_at") or 0),
        "text": text[:240],
    }


def _finalize_subject(subject: dict) -> dict:
    result = dict(subject)
    result.pop("_score", None)
    result.pop("_user_supported", None)
    result.pop("_session_ids", None)
    result["keywords"] = result["keywords"][:10]
    result["sessions"].sort(key=lambda s: (-s["last_active"], s["id"]))
    result["sessions"] = result["sessions"][:10]
    result["snippets"].sort(key=lambda s: (
        s["role"] != "user", s["timestamp"], s["session_id"], s.get("message_id") or 0,
    ))
    result["snippets"] = result["snippets"][:5]
    return result


def _work_items_for_session(session: dict, messages: list[dict]) -> list[dict]:
    users = []
    support = []
    candidates = extract_subject_candidates(session, messages)
    for message in messages:
        role = message.get("role")
        text = _message_text(message)
        if role == "user" and text:
            users.append(_work_item(session, message, _classify_work(text), text))
        elif role == "assistant" and text:
            lowered = text.lower()
            if (any(word in lowered for word in ("implemented", "added", "fixed", "updated"))
                    and any(_matches_candidate(text, candidate) for candidate in candidates)):
                item = _work_item(session, message, _classify_work(text), text)
                if not any(existing["text"] == item["text"] for existing in support):
                    support.append(item)
    return (users + support)[:8]


def _work_item(session: dict, message: dict, kind: str, text: str) -> dict:
    return {
        "session_id": session.get("id"),
        "kind": kind,
        "text": " ".join(text.split())[:240],
        "timestamp": float(message.get("timestamp") or session.get("started_at") or 0),
    }


def _classify_work(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ("test", "fix", "debug", "implement", "code", ".py", ".tsx")):
        return "coding"
    if any(word in lowered for word in ("research", "investigate", "review", "search")):
        return "research"
    if any(word in lowered for word in ("plan", "roadmap", "design")):
        return "planning"
    return "conversation"
