import pytest

from domain_i.inputs import (
    DomainQuery,
    InputError,
    NameQuery,
    clean,
    name_from_domain,
    parse_query,
    variants,
)


def fake_splitter(word: str) -> list[str]:
    table = {"foobar": ["foo", "bar"], "google": ["google"], "passwordmanager": ["password", "manager"]}
    return table.get(word, [word])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Example.COM  ", "example.com"),
        ("https://www.Example.com/path?q=1#x", "example.com"),
        ("http://user@example.com:8080/", "example.com"),
        ("example.com.", "example.com"),
        ("www.github.com", "github.com"),
        ("foobar", "foobar"),
    ],
)
def test_clean(raw: str, expected: str) -> None:
    assert clean(raw) == expected


def test_parse_query_with_suffix_is_domain_query() -> None:
    assert parse_query(["https://Example.com/a"], fake_splitter) == DomainQuery("example.com")


def test_parse_query_single_word_uses_splitter() -> None:
    assert parse_query(["FooBar"], fake_splitter) == NameQuery(("foo", "bar"))


def test_parse_query_unsplittable_word() -> None:
    assert parse_query(["google"], fake_splitter) == NameQuery(("google",))


def test_parse_query_spaces_and_hyphens_split_without_splitter() -> None:
    assert parse_query(["password", "manager"], fake_splitter) == NameQuery(("password", "manager"))
    assert parse_query(["password manager"], fake_splitter) == NameQuery(("password", "manager"))
    assert parse_query(["foo-bar"], fake_splitter) == NameQuery(("foo", "bar"))


@pytest.mark.parametrize("args", [[], [""], ["   "], ["!!!"], ["foo", "bar.com"], ["exa mple.com"]])
def test_parse_query_rejects_bad_input(args: list[str]) -> None:
    with pytest.raises(InputError):
        parse_query(args, fake_splitter)


def test_variants() -> None:
    assert variants(("foo", "bar")) == ("foobar", "foo-bar")
    assert variants(("google",)) == ("google", None)


def test_name_from_domain_uses_first_label() -> None:
    assert name_from_domain("example.co.uk") == "example"
    assert name_from_domain("foo-bar.app") == "foo-bar"
