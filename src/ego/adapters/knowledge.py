"""File knowledge store (`knowledge = "file"`): an index plus one file per topic.

.ego/knowledge/
  index.md           one line per topic: slug, summary, keywords. Read this first.
  topics/<slug>.md   one reading unit: the facts of one topic as bullets.

A reader opens `index.md`, decides which topics matter for the task and opens only those files.
A writer reads the index first: a fact that matches an existing topic is merged into that file
(duplicates are skipped); only a fact with no matching topic creates a new file.
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import re
from typing import Any
from ..shared.util import EgoError, atomic_text, exclusive_lock

INDEX_HEADER = ("# Knowledge index\n\n"
                "Read this first. Open only the topic files that match the task; add facts with `ego knowledge add`.\n\n")
MAX_TEXT = 20000
MAX_KEYWORDS = 12
LEGACY_FILE = Path(".ego") / "knowledge" / "domain.md"  # the single-file layout of earlier versions
_ENTRY = re.compile(r"^- \[(?P<slug>[^\]]+)\]\((?P<path>[^)]+)\) — (?P<summary>.*?)(?: \| keywords: (?P<keywords>.*))?$")
_STOP = {"the", "and", "for", "with", "that", "this", "from", "are", "was", "not", "use", "used", "when", "into", "than"}
_CJK = re.compile(r"[぀-ヿ㐀-鿿]+")


def tokens(text: str) -> set[str]:
    """Lowercase ASCII words and, for Japanese/Chinese runs, character bigrams (those scripts have no spaces)."""
    out = {w for w in re.findall(r"[a-z0-9_]+", text.lower()) if len(w) >= 2 and w not in _STOP}
    for run in _CJK.findall(text):
        if len(run) == 1:
            out.add(run)
        else:
            out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48].strip("-")
    return slug or "topic-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def _norm(line: str) -> str:
    return " ".join(line.lower().split())


def _facts(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        line = line[2:].strip() if line.startswith(("- ", "* ")) else line
        if line:
            lines.append(" ".join(line.split()))
    return lines


class KnowledgeStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.base = self.root / ".ego" / "knowledge"
        self.index_path = self.base / "index.md"
        self.topics = self.base / "topics"
        self.lock = self.root / ".ego" / "locks" / "knowledge.lock"

    # -- reading ---------------------------------------------------------------------------------
    def entries(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if self.index_path.exists():
            for line in self.index_path.read_text(encoding="utf-8").splitlines():
                m = _ENTRY.match(line.strip())
                if m:
                    kws = [k.strip() for k in (m.group("keywords") or "").split(",") if k.strip()]
                    found.append({"slug": m.group("slug"), "path": m.group("path"), "summary": m.group("summary").strip(), "keywords": kws})
        if (self.root / LEGACY_FILE).exists() and not any(e["slug"] == "domain" for e in found):
            found.append({"slug": "domain", "path": "domain.md", "summary": "Legacy single-file project knowledge", "keywords": [], "legacy": True})
        return found

    def index_text(self) -> str:
        return self.index_path.read_text(encoding="utf-8") if self.index_path.exists() else INDEX_HEADER

    def _topic_file(self, entry: dict[str, Any]) -> Path:
        return (self.base / entry["path"]).resolve()

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        want = tokens(query)
        scored = []
        for entry in self.entries():
            have = tokens(" ".join([entry["slug"].replace("-", " "), entry["summary"], *entry["keywords"]]))
            shared = len(want & have)
            if shared or not want:
                scored.append((shared, entry))
        scored.sort(key=lambda pair: -pair[0])
        return [{**entry, "score": shared} for shared, entry in scored[:limit]]

    def get(self, slug: str) -> dict[str, Any]:
        for entry in self.entries():
            if entry["slug"] == slug:
                path = self._topic_file(entry)
                if path.is_file() and self.base.resolve() in path.parents:
                    return {**entry, "text": path.read_text(encoding="utf-8")}
        raise EgoError("KNOWLEDGE_TOPIC_NOT_FOUND", f"No topic {slug!r}; run `ego knowledge index`", 2)

    # -- writing ---------------------------------------------------------------------------------
    def _match(self, entries: list[dict[str, Any]], facts: list[str], keywords: list[str]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """The existing topic a new fact belongs to, with the candidates considered (strongest first)."""
        want_kw = {k.lower() for k in keywords}
        want = tokens(" ".join([*keywords, *facts]))
        scored = []
        for entry in entries:
            if entry.get("legacy"):
                continue  # the legacy file is read-only
            have = tokens(" ".join([entry["slug"].replace("-", " "), entry["summary"], *entry["keywords"]]))
            shared = len(want & have)
            keyword_hit = bool(want_kw & {k.lower() for k in entry["keywords"]})
            ratio = shared / max(1, min(len(want), len(have)))
            scored.append((keyword_hit, shared, ratio, entry))
        scored.sort(key=lambda s: (s[0], s[1], s[2]), reverse=True)
        considered = [{"slug": e["slug"], "shared": shared, "keyword_hit": hit} for hit, shared, _, e in scored[:3]]
        for hit, shared, ratio, entry in scored:
            if hit or (shared >= 2 and ratio >= 0.5):
                return entry, considered
        return None, considered

    def add(self, text: str, *, topic: str | None = None, summary: str | None = None, keywords: list[str] | None = None) -> dict[str, Any]:
        facts = _facts(text)
        if not facts or len(text) > MAX_TEXT:
            raise EgoError("INVALID_KNOWLEDGE", "Empty or too large", 2)
        kws = [k.strip() for k in (keywords or []) if k and k.strip()]
        with exclusive_lock(self.lock):
            entries = [e for e in self.entries() if not e.get("legacy")]
            target, considered = (None, [])
            if topic:
                slug = slugify(topic)
                target = next((e for e in entries if e["slug"] == slug), None)
                if target is None:
                    target = {"slug": slug, "path": f"topics/{slug}.md", "summary": "", "keywords": []}
                    entries_new = True
                else:
                    entries_new = False
            else:
                match, considered = self._match(entries, facts, kws)
                if match:
                    target, entries_new = match, False
                else:
                    seed = kws[0] if kws else " ".join(sorted(w for w in tokens(facts[0]) if w.isascii())[:3]) or facts[0]
                    base_slug = slug = slugify(seed)
                    n = 2
                    while any(e["slug"] == slug for e in entries):  # a different topic already owns this name
                        slug, n = f"{base_slug}-{n}", n + 1
                    target = {"slug": slug, "path": f"topics/{slug}.md", "summary": "", "keywords": []}
                    entries_new = True
            path = self._topic_file(target)
            if not self.base.resolve() in path.parents:
                raise EgoError("INVALID_KNOWLEDGE", "Topic path escapes .ego/knowledge", 2)
            existing = [ln[2:].strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")] if path.exists() else []
            added = merged = skipped = 0
            for fact in facts:
                n = _norm(fact)
                if any(n in _norm(old) for old in existing):
                    skipped += 1
                    continue
                richer = next((i for i, old in enumerate(existing) if _norm(old) in n), None)
                if richer is not None:  # the new line says the same and more: replace instead of keeping both
                    existing[richer] = fact
                    merged += 1
                else:
                    existing.append(fact)
                    added += 1
            if added or merged:
                title = next((ln[2:].strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("# ")), "") if path.exists() else ""
                atomic_text(path, f"# {title or (summary or facts[0])[:60]}\n\n" + "\n".join(f"- {ln}" for ln in existing) + "\n")
            target["summary"] = (summary or target["summary"] or facts[0])[:100].replace("\n", " ")
            target["keywords"] = list(dict.fromkeys([*target["keywords"], *kws]))[:MAX_KEYWORDS]
            if added or merged or entries_new or summary or kws:
                by_slug = {e["slug"]: e for e in entries}
                by_slug[target["slug"]] = target
                self._write_index(list(by_slug.values()))
        action = "created" if entries_new and (added or merged) else "appended" if (added or merged) else "duplicate"
        return {"action": action, "topic": target["slug"], "path": str(path.relative_to(self.root)),
                "added": added, "merged": merged, "skipped": skipped, "considered": considered}

    def _write_index(self, entries: list[dict[str, Any]]) -> None:
        body = INDEX_HEADER
        for e in sorted(entries, key=lambda x: x["slug"]):
            kw = f" | keywords: {', '.join(e['keywords'])}" if e["keywords"] else ""
            body += f"- [{e['slug']}]({e['path']}) — {e['summary']}{kw}\n"
        atomic_text(self.index_path, body)


def auto_record_enabled() -> bool:
    return os.environ.get("EGO_KNOWLEDGE_AUTO_RECORD", "1").strip().lower() not in ("0", "false", "off", "no")


def record_checkpoint_decisions(root: Path, checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Automatic recording for `knowledge = "file"`: the decisions of a saved checkpoint go into the store.

    No model is called: the goal's words become keywords so related decisions merge into one topic.
    Fail-open: a problem here must never fail the checkpoint."""
    if not auto_record_enabled() or not isinstance(checkpoint, dict):
        return {"status": "skipped"}
    decisions = [str(d).strip() for d in (checkpoint.get("decisions") or [])[:8] if str(d).strip()]
    if not decisions:
        return {"status": "skipped", "reason": "no decisions"}
    goal_words = [w for w in re.findall(r"[a-z0-9_]{3,}", str(checkpoint.get("goal") or "").lower()) if w not in _STOP][:3]
    try:
        store = KnowledgeStore(root)
        return {"status": "recorded", "results": [store.add(d, keywords=goal_words) for d in decisions]}
    except (EgoError, OSError) as exc:
        return {"status": "error", "reason": str(exc)}
