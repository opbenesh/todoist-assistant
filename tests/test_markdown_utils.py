from __future__ import annotations

from lib.markdown_utils import escape_markdown


def test_escape_markdown_simple() -> None:
    assert escape_markdown("hello") == "hello"
    assert escape_markdown("hello_world") == "hello\\_world"
    assert escape_markdown("hello*world") == "hello\\*world"
    assert escape_markdown("hello[world") == "hello\\[world"
    assert escape_markdown("hello`world") == "hello\\`world"


def test_escape_markdown_link() -> None:
    assert (
        escape_markdown("[תשלום לקצה](https://www.gov.il/he/service/company_partnership_payment)")
        == "[תשלום לקצה](https://www.gov.il/he/service/company\\_partnership\\_payment)"
    )


def test_escape_markdown_mixed() -> None:
    assert (
        escape_markdown("Read *this* [link](http://test_url) first")
        == "Read \\*this\\* [link](http://test\\_url) first"
    )
