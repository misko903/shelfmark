import requests

from shelfmark.core.cache import get_metadata_cache
from shelfmark.metadata_providers import MetadataSearchOptions, SearchType
from shelfmark.metadata_providers.databazeknih import DatabazeknihProvider

SEARCH_HTML = """
<html><body>
<div id="content">
  <p class="new">
    <a href="knihy/saturnin-15"><img class="search_graphic"
       src="https://www.databazeknih.cz/img/books/15_/15/mid_saturnin-15.jpg" /></a>
    <a href="knihy/saturnin-15" class="new" type="book">Saturnin</a>
    <span class="smallfind">1942, <a href="autori/zdenek-jirotka-117">Zdeněk Jirotka</a></span>
  </p>
  <p class="new">
    <a href="knihy/saturnin-15" class="new">Saturnin</a>
  </p>
  <p class="new">
    <a href="knihy/saturnin-se-vraci-24577" class="new" type="book">Saturnin se vrací</a>
    <span class="smallfind">2009, Miroslav Macek</span>
  </p>
  <p><a href="autori/zdenek-jirotka-117">Zdeněk Jirotka</a></p>
</div>
</body></html>
"""

BOOK_HTML = """
<html><head>
<link rel="canonical" href="https://www.databazeknih.cz/knihy/saturnin-15" />
<meta property="og:image" content="https://www.databazeknih.cz/img/books/15_/15/saturnin-15.jpg" />
</head><body>
<div id="content">
  <h1 itemprop="name">Saturnin <span class="edition">(e-kniha)</span></h1>
  <h2 class="jmenaautoru"><a href="autori/zdenek-jirotka-117">Zdeněk Jirotka</a></h2>
  <img class="kniha_img" src="https://www.databazeknih.cz/img/books/15_/15/big_saturnin-15.jpg" />
  <a class="bpoints">89%</a>
  <p>Série: <a href="serie/saturnin-1234">Saturnin</a> (1. díl)</p>
  <p itemprop="description">Humoristický román o sluhovi.<span class="end_text">
     Humoristický román o sluhovi Saturninovi a jeho pánovi.</span>... celý text</p>
  <span itemprop="genre">Literatura česká, Romány</span>
  <span itemprop="numberOfPages">208</span>
  <span itemprop="inLanguage">český</span>
</div>
</body></html>
"""

MORE_INFO_HTML = """
<div>
  Vydáno: <span itemprop="datePublished">2008</span>,
  <span itemprop="publisher"><a href="nakladatelstvi/druhe-mesto">Druhé město</a></span>
  ISBN: <span itemprop="isbn">978-80-7227-282-5</span>
</div>
"""


class _Response:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class _Session:
    """Serves canned HTML per URL substring."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = 0
        self.urls = []
        self.headers = {}

    def get(self, url, **kwargs):
        self.calls += 1
        self.urls.append(url)
        for fragment, html in self.pages.items():
            if fragment in url:
                return _Response(html)
        raise requests.HTTPError(f"unexpected URL: {url}")


class _FlakySession(_Session):
    def get(self, url, **kwargs):
        if self.calls == 0:
            self.calls += 1
            raise requests.Timeout
        return super().get(url, **kwargs)


def _provider(session):
    provider = DatabazeknihProvider()
    provider.session = session
    return provider


class TestDatabazeknihSearch:
    def test_search_parses_results(self):
        get_metadata_cache().clear()
        provider = _provider(_Session({"/search": SEARCH_HTML}))

        books = provider.search(MetadataSearchOptions(query="saturnin"))

        # Duplicate slug deduped, author link ignored
        assert [b.provider_id for b in books] == ["saturnin-15", "saturnin-se-vraci-24577"]
        book = books[0]
        assert book.provider == "databazeknih"
        assert book.title == "Saturnin"
        assert book.authors == ["Zdeněk Jirotka"]
        assert book.publish_year == 1942
        assert book.cover_url == "https://www.databazeknih.cz/img/books/15_/15/mid_saturnin-15.jpg"
        assert book.source_url == "https://www.databazeknih.cz/knihy/saturnin-15"
        # Author without a link is read from the plain "year, author" text
        assert books[1].authors == ["Miroslav Macek"]
        assert books[1].publish_year == 2009

    def test_search_landing_on_book_page(self):
        get_metadata_cache().clear()
        provider = _provider(
            _Session({"/search": BOOK_HTML, "/book-detail-more-info/": MORE_INFO_HTML})
        )

        books = provider.search(MetadataSearchOptions(query="saturnin jirotka"))

        assert len(books) == 1
        assert books[0].provider_id == "saturnin-15"

    def test_search_second_page_is_empty(self):
        provider = _provider(_Session({"/search": SEARCH_HTML}))

        assert provider.search(MetadataSearchOptions(query="saturnin", page=2)) == []

    def test_search_does_not_cache_request_failures(self):
        get_metadata_cache().clear()
        session = _FlakySession({"/search": SEARCH_HTML})
        provider = _provider(session)
        options = MetadataSearchOptions(query="saturnin")

        assert provider.search(options) == []
        assert len(provider.search(options)) == 2
        assert session.calls == 2


class TestDatabazeknihGetBook:
    def test_get_book_parses_details(self):
        get_metadata_cache().clear()
        session = _Session({"/knihy/": BOOK_HTML, "/book-detail-more-info/15": MORE_INFO_HTML})
        provider = _provider(session)

        book = provider.get_book("saturnin-15")

        assert book is not None
        # Nested edition badge excluded from the title
        assert book.title == "Saturnin"
        assert book.authors == ["Zdeněk Jirotka"]
        assert book.cover_url == (
            "https://www.databazeknih.cz/img/books/15_/15/big_saturnin-15.jpg"
        )
        assert book.description == "Humoristický román o sluhovi Saturninovi a jeho pánovi."
        assert book.genres == ["Literatura česká", "Romány"]
        assert book.language == "cs"
        assert book.series_name == "Saturnin"
        assert book.series_position == 1
        # Lazily loaded block supplies ISBN, publisher and year
        assert book.isbn_13 == "9788072272825"
        assert book.publisher == "Druhé město"
        assert book.publish_year == 2008
        labels = {f.label: f.value for f in book.display_fields}
        assert labels["Rating"] == "89%"
        assert labels["Pages"] == "208"
        assert labels["Series"] == "Saturnin 1."

    def test_get_book_survives_missing_more_info(self):
        get_metadata_cache().clear()
        provider = _provider(_Session({"/knihy/": BOOK_HTML}))

        book = provider.get_book("saturnin-15")

        assert book is not None
        assert book.isbn_13 is None
        assert book.publisher is None

    def test_isbn_search_resolves_first_result(self):
        get_metadata_cache().clear()
        provider = _provider(
            _Session(
                {
                    "/search": SEARCH_HTML,
                    "/knihy/": BOOK_HTML,
                    "/book-detail-more-info/": MORE_INFO_HTML,
                }
            )
        )

        book = provider.search(
            MetadataSearchOptions(query="978-80-7227-282-5", search_type=SearchType.ISBN)
        )[0]

        assert book.provider_id == "saturnin-15"
        assert book.isbn_13 == "9788072272825"
