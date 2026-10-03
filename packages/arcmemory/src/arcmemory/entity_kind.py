"""Entity kind taxonomy — one canonical ``entity_type`` and topical-only ``tags``.

The distiller names entity types as free text, so one store held ``thing``,
``unknown``, ``note``, ``business-idea``, ``email_account`` and ``entity`` side by
side, and a legacy import wrote the category a second time into ``tags``
(``company`` / ``people`` / ``project``). Two rules fix both:

* ``entity_type`` is one KIND from a small controlled vocabulary. Free-text values
  fold onto the closest kind through :data:`_SYNONYMS`; anything unrecognized is
  ``other``. Machine-written system kinds (connector ``source`` / ``mapping`` cards,
  ``blob_folder``, ``db_table``) pass through untouched — code filters on them.
* ``tags`` are free topical labels (domains, initiatives, clients). A tag that only
  restates the kind — the kind itself, its plural, or any synonym of it — carries
  no information and is dropped.

Kinds are RANKED so a merge or a later write keeps the more specific one: ``other``
(rank 0) < generic ``concept`` (rank 1) < a specific kind (rank 2). Identity kinds
(person, company, team, place, account) are mutually exclusive: a person is never
a place, so two cards of different identity kinds are never the same entity.
"""

from __future__ import annotations

from arcmemory.slug import canonical_slug

#: The fallback kind for a type that says nothing ("thing", "unknown", "note").
OTHER = "other"

#: Kinds written by code, not the model. Never renamed, never merged.
SYSTEM_KINDS = frozenset({"source", "mapping", "blob_folder", "db_table"})

#: Specific kinds that name WHAT a real-world thing is; two different ones never merge.
IDENTITY_KINDS = frozenset({"person", "company", "team", "place", "account"})

#: Specific kinds that name a piece of work or knowledge.
WORK_KINDS = frozenset({"project", "product", "deal", "event", "thesis", "document", "issue"})

#: The one generic kind (rank 1): an idea, framework, insight or research topic.
GENERIC_KINDS = frozenset({"concept"})

#: Every kind a model-written card may carry.
KINDS = IDENTITY_KINDS | WORK_KINDS | GENERIC_KINDS | {OTHER}

_SYNONYMS: dict[str, str] = {
    # vague -> other
    "thing": OTHER,
    "unknown": OTHER,
    "note": OTHER,
    "entity": OTHER,
    "fact": OTHER,
    "item": OTHER,
    "misc": OTHER,
    # people / organizations / places
    "people": "person",
    "human": "person",
    "contact": "person",
    "individual": "person",
    "organization": "company",
    "organisation": "company",
    "org": "company",
    "vendor": "company",
    "partner": "company",
    "customer": "company",
    "client": "company",
    "agency": "company",
    "business": "company",
    "group": "team",
    "location": "place",
    "city": "place",
    "site": "place",
    "email-account": "account",
    "user-account": "account",
    # work
    "program": "project",
    "initiative": "project",
    "effort": "project",
    "workstream": "project",
    "tool": "product",
    "technology": "product",
    "software": "product",
    "service": "product",
    "platform": "product",
    "skill": "product",
    "repo": "product",
    "opportunity": "deal",
    "contract": "deal",
    "meeting": "event",
    "call": "event",
    "theses": "thesis",
    "doc": "document",
    "article": "document",
    "report": "document",
    "writeup": "document",
    "problem": "issue",
    "bug": "issue",
    "gap": "issue",
    "risk": "issue",
    # generic
    "idea": "concept",
    "business-idea": "concept",
    "insight": "concept",
    "framework": "concept",
    "research": "concept",
    "knowledge": "concept",
    "topic": "concept",
    "principle": "concept",
    "decision": "concept",
    "policy": "concept",
}


def normalize_kind(raw: str) -> str:
    """Fold a free-text ``entity_type`` onto one canonical kind.

    System kinds pass through verbatim. A known kind or synonym (plural forms
    included) maps onto its kind; anything else is ``other``.
    """
    key = canonical_slug(raw or "")
    if key.replace("-", "_") in SYSTEM_KINDS:
        return key.replace("-", "_")
    if key in KINDS:
        return key
    if key in _SYNONYMS:
        return _SYNONYMS[key]
    singular = key[:-3] + "y" if key.endswith("ies") else key.removesuffix("s")
    if singular in KINDS:
        return singular
    return _SYNONYMS.get(singular, OTHER)


def kind_rank(kind: str) -> int:
    """0 for ``other``, 1 for the generic kind, 2 for any specific kind."""
    normalized = normalize_kind(kind)
    if normalized == OTHER:
        return 0
    if normalized in GENERIC_KINDS:
        return 1
    return 2


def kinds_compatible(a: str, b: str) -> bool:
    """True when two cards' kinds do not rule out their being one entity.

    Equal kinds, or either side vague/generic, are compatible. Two different
    identity kinds (a person and a place) never are, nor is an identity kind
    against a work kind (a person is not a project). System kinds never merge.
    """
    ka, kb = normalize_kind(a), normalize_kind(b)
    if ka in SYSTEM_KINDS or kb in SYSTEM_KINDS:
        return False
    if ka == kb or kind_rank(ka) < 2 or kind_rank(kb) < 2:
        return True
    return ka not in IDENTITY_KINDS and kb not in IDENTITY_KINDS


def more_specific_kind(kinds: list[str]) -> str:
    """The highest-ranked kind among ``kinds`` (first one wins a tie)."""
    normalized = [normalize_kind(k) for k in kinds] or [OTHER]
    return max(normalized, key=kind_rank)


def restates_kind(tag: str) -> bool:
    """True when ``tag`` only names a kind (any kind, or a synonym/plural of one).

    Such a tag says what the card IS, which ``entity_type`` already says; it is
    never a topic, so it is never kept as a tag.
    """
    key = canonical_slug(tag)
    return key in KINDS or key in _SYNONYMS or key == "unknown" or normalize_kind(key) != OTHER


def clean_tags(tags: list[str]) -> list[str]:
    """Drop tags that restate a kind; dedupe case-insensitively, keep order."""
    seen: set[str] = set()
    kept: list[str] = []
    for tag in tags:
        label = str(tag).strip()
        key = label.casefold()
        if not label or key in seen or restates_kind(label):
            continue
        seen.add(key)
        kept.append(label)
    return kept


def infer_kind(raw_type: str, tags: list[str], name: str) -> str:
    """The best kind for a legacy card: its type, else a category tag, else its name.

    A vague type (``thing``/``unknown``) is upgraded by a legacy category tag
    (``thesis``, ``people``, ``product``) or a "Thesis N" style name, so the
    migration recovers the kind the old importer stored in the wrong field.
    """
    kind = normalize_kind(raw_type)
    if kind in SYSTEM_KINDS or kind_rank(kind) == 2:
        return kind
    candidates = [kind] + [normalize_kind(t) for t in tags]
    numbered = numbered_label(name)
    if numbered is not None:
        candidates.append(normalize_kind(numbered[0]))
    return more_specific_kind(candidates)


def numbered_label(text: str) -> tuple[str, str] | None:
    """``("thesis", "5")`` for "Thesis 5: ..." / "ai-thesis-5" — a series member key."""
    tokens = canonical_slug(text).split("-")
    for index, token in enumerate(tokens[:-1]):
        if token in _SERIES_LABELS and tokens[index + 1].isdigit():
            return (_SERIES_LABELS[token], str(int(tokens[index + 1])))
    return None


#: Words that introduce a numbered series member ("Thesis 5", "Concept 1").
_SERIES_LABELS: dict[str, str] = {
    "thesis": "thesis",
    "theses": "thesis",
    "concept": "concept",
    "principle": "principle",
    "pillar": "pillar",
    "idea": "idea",
    "law": "law",
    "rule": "rule",
}


__all__ = [
    "GENERIC_KINDS",
    "IDENTITY_KINDS",
    "KINDS",
    "OTHER",
    "SYSTEM_KINDS",
    "WORK_KINDS",
    "clean_tags",
    "infer_kind",
    "kind_rank",
    "kinds_compatible",
    "more_specific_kind",
    "normalize_kind",
    "numbered_label",
    "restates_kind",
]
