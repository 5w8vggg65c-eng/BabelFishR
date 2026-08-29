"""Operator vocabulary: callsigns, unit names, brevity codes, do-not-translate.

Radio traffic is full of terms a general-purpose model mangles: callsigns
("KD8XYZ"), unit designators, place names, brevity words and coordinates.  The
glossary steers both stages - it biases the recogniser and constrains the
translator - and can mark terms that must survive translation untouched.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re
from typing import Dict, Iterable, List, Optional, Sequence

#: Categories are informational, but they keep a long list navigable in the UI.
CATEGORIES = ("callsign", "unit", "place", "terminology", "acronym",
              "coordinate", "brevity", "other")


@dataclasses.dataclass
class GlossaryEntry:
    term: str
    """The term as it is heard/written in the source language."""

    translation: str = ""
    """Preferred rendering in the target language. Empty = leave as-is."""

    category: str = "other"
    never_translate: bool = False
    notes: str = ""
    languages: List[str] = dataclasses.field(default_factory=list)
    """Restrict to these source languages; empty means all."""

    def applies_to(self, language: Optional[str]) -> bool:
        if not self.languages:
            return True
        return language is not None and language in self.languages

    def to_dict(self) -> Dict[str, object]:
        return dataclasses.asdict(self)


class Glossary:
    """A user-managed term list, persisted as JSON."""

    def __init__(self, entries: Optional[Iterable[GlossaryEntry]] = None):
        self.entries: List[GlossaryEntry] = list(entries or [])

    # -- persistence -----------------------------------------------------
    @classmethod
    def load(cls, path: str) -> "Glossary":
        file = pathlib.Path(path)
        if not file.exists():
            return cls()
        raw = json.loads(file.read_text(encoding="utf-8"))
        items = raw.get("entries", raw) if isinstance(raw, dict) else raw
        return cls(GlossaryEntry(**item) for item in items)

    def save(self, path: str) -> str:
        file = pathlib.Path(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(
            {"format": "babelfishr.glossary.v1",
             "entries": [e.to_dict() for e in self.entries]},
            indent=2, ensure_ascii=False), encoding="utf-8")
        return str(file)

    # -- editing ---------------------------------------------------------
    def add(self, term: str, translation: str = "", category: str = "other",
            never_translate: bool = False, **kwargs) -> GlossaryEntry:
        entry = GlossaryEntry(term=term, translation=translation,
                              category=category, never_translate=never_translate,
                              **kwargs)
        self.remove(term)
        self.entries.append(entry)
        return entry

    def remove(self, term: str) -> bool:
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.term.lower() != term.lower()]
        return len(self.entries) != before

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)

    # -- use -------------------------------------------------------------
    def vocabulary(self, language: Optional[str] = None) -> List[str]:
        """Terms to bias the recogniser with."""
        return [e.term for e in self.entries if e.applies_to(language)]

    def mapping(self, language: Optional[str] = None) -> Dict[str, str]:
        """term -> preferred translation, for terms that have one."""
        return {e.term: e.translation for e in self.entries
                if e.translation and e.applies_to(language)}

    def protected(self, language: Optional[str] = None) -> List[str]:
        """Terms that must appear verbatim in the translation."""
        return [e.term for e in self.entries
                if e.never_translate and e.applies_to(language)]

    def prompt_hint(self, language: Optional[str] = None,
                    limit: int = 200) -> str:
        """A compact instruction block for engines that accept guidance."""
        lines: List[str] = []
        for entry in self.entries[:limit]:
            if not entry.applies_to(language):
                continue
            if entry.never_translate:
                lines.append(f"- {entry.term}: keep exactly as written")
            elif entry.translation:
                lines.append(f"- {entry.term} -> {entry.translation}")
            else:
                lines.append(f"- {entry.term}: recognised term, do not alter")
        return "\n".join(lines)


_TOKEN = "␂{}␃"  # unlikely to appear in speech or be translated


def protect_terms(text: str, terms: Sequence[str]) -> tuple:
    """Replace protected terms with placeholders. Returns ``(text, mapping)``.

    For engines that cannot be instructed (offline MT models), this is the only
    reliable way to stop a callsign being "translated" into a word.
    """
    mapping: Dict[str, str] = {}
    out = text
    for index, term in enumerate(sorted(set(terms), key=len, reverse=True)):
        if not term:
            continue
        placeholder = _TOKEN.format(index)
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        if pattern.search(out):
            out = pattern.sub(placeholder, out)
            mapping[placeholder] = term
    return (out, mapping)


def restore_terms(text: str, mapping: Dict[str, str]) -> str:
    for placeholder, term in mapping.items():
        text = text.replace(placeholder, term)
    return text
