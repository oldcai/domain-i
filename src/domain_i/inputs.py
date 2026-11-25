"""Input cleaning and query classification."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

Splitter = Callable[[str], list[str]]

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://")
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_WORD = re.compile(r"[a-z0-9]+")


class InputError(ValueError):
    pass


@dataclass(frozen=True)
class DomainQuery:
    domain: str


@dataclass(frozen=True)
class NameQuery:
    words: tuple[str, ...]


def clean(raw: str) -> str:
    """Strip whitespace, scheme, credentials, port, path and a leading ``www.``."""
    text = raw.strip().lower()
    text = _SCHEME.sub("", text)
    text = re.split(r"[/?#]", text, maxsplit=1)[0]
    text = text.rsplit("@", 1)[-1]
    text = re.sub(r":\d*$", "", text)
    text = text.strip(".")
    if text.startswith("www.") and text.count(".") >= 2:
        text = text[4:]
    return text


def _to_ascii(domain: str) -> str:
    if domain.isascii():
        return domain
    try:
        return domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InputError(f"not a valid domain: {domain}") from exc


def _valid_domain(domain: str) -> bool:
    labels = domain.split(".")
    return len(labels) >= 2 and all(_LABEL.match(label) for label in labels)


def parse_query(args: Sequence[str], splitter: Splitter) -> DomainQuery | NameQuery:
    tokens = [t for arg in args for t in arg.split()]
    if not tokens:
        raise InputError("give a domain (example.com) or a name (foo bar)")
    if len(tokens) == 1:
        cleaned = clean(tokens[0])
        if "." in cleaned:
            domain = _to_ascii(cleaned)
            if not _valid_domain(domain):
                raise InputError(f"not a valid domain: {tokens[0]}")
            return DomainQuery(domain)
        if "-" not in cleaned:
            if not _WORD.search(cleaned):
                raise InputError(f"no usable characters in: {tokens[0]}")
            return NameQuery(words_for(cleaned, splitter))
        tokens = [cleaned]
    words: list[str] = []
    for token in tokens:
        if "." in token:
            raise InputError("a multi-word name cannot contain a domain suffix")
        for piece in token.lower().split("-"):
            words.extend(_WORD.findall(piece))
    if not words:
        raise InputError("no usable characters in input")
    return NameQuery(tuple(words))


def words_for(name: str, splitter: Splitter) -> tuple[str, ...]:
    """Words of a single name: split on hyphens if present, else ask ``splitter``."""
    if "-" in name:
        return tuple(w for piece in name.split("-") for w in _WORD.findall(piece))
    word = "".join(_WORD.findall(name))
    parts = [p for p in (s.lower() for s in splitter(word)) if _WORD.fullmatch(p)]
    # wordninja happily shreds gibberish into single letters ("g-it-hub");
    # treat such splits as "no real words found".
    if "".join(parts) != word or any(len(p) == 1 and p not in ("a", "i") for p in parts):
        parts = [word]
    return tuple(parts)


def variants(words: tuple[str, ...]) -> tuple[str, str | None]:
    plain = "".join(words)
    hyphen = "-".join(words) if len(words) >= 2 else None
    return plain, hyphen


def name_from_domain(domain: str) -> str:
    return domain.split(".", 1)[0]
