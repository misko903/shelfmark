"""Databazeknih.cz metadata provider. Czech book catalog, no API key required.

The site has no public API, so this scrapes its HTML. Parsing prefers the
schema.org microdata (itemprop) and OpenGraph tags the book pages carry, and
falls back to the site's own CSS classes, so small layout changes don't break it.
"""

import re
from typing import Any, ClassVar
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup, Tag

from shelfmark.core.cache import cacheable
from shelfmark.core.logger import setup_logger
from shelfmark.core.settings_registry import (
    ActionButton,
    CheckboxField,
    HeadingField,
    SettingsField,
    register_settings,
)
from shelfmark.download.network import get_ssl_verify
from shelfmark.metadata_providers import (
    BookMetadata,
    DisplayField,
    MetadataProvider,
    MetadataSearchOptions,
    SearchField,
    SearchType,
    SortOrder,
    TextSearchField,
    register_provider,
)
from shelfmark.metadata_providers.moly import (
    ISBN_13_LENGTH,
    RateLimiter,
    _clean_text,
    _valid_isbn,
)

logger = setup_logger(__name__)

DBK_BASE_URL = "https://www.databazeknih.cz"
DBK_BOOK_URL = f"{DBK_BASE_URL}/knihy/"
DBK_SEARCH_URL = f"{DBK_BASE_URL}/search?q="
# Publisher/ISBN block that the book page loads lazily.
DBK_MORE_INFO_URL = f"{DBK_BASE_URL}/book-detail-more-info/"

# Be polite: databazeknih.cz is a community site
RATE_LIMIT_REQUESTS = 30
RATE_LIMIT_WINDOW_SECONDS = 60

REQUEST_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"),
    "Accept-Language": "cs,sk;q=0.9,en;q=0.7",
}

# Book slugs always end with the numeric book id: "saturnin-15".
_BOOK_HREF_RE = re.compile(r"(?:^|/)knihy/([\w-]+-(\d+))(?:[/?#]|$)")
_YEAR_RE = re.compile(r"\b(1[4-9]\d\d|20\d\d)\b")
_SERIES_POSITION_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*\.?\s*díl", re.IGNORECASE)

_LANGUAGE_MAP = {
    "česky": "cs",
    "český": "cs",
    "čeština": "cs",
    "slovensky": "sk",
    "slovenský": "sk",
    "slovenština": "sk",
    "anglicky": "en",
    "anglický": "en",
    "angličtina": "en",
    "německy": "de",
    "německý": "de",
    "němčina": "de",
    "polsky": "pl",
    "polský": "pl",
    "polština": "pl",
}

# Trailing "show more/less" toggles in descriptions
_DESCRIPTION_NOISE = ("... celý text", "…celý text", "celý text", "méně textu")

_rate_limiter = RateLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS)


def _absolute_url(url: str | None) -> str | None:
    if not url:
        return None
    return urljoin(DBK_BASE_URL + "/", url)


def _meta_content(soup: BeautifulSoup, prop: str) -> str | None:
    node = soup.select_one(f'meta[property="{prop}"][content], meta[name="{prop}"][content]')
    return _clean_text(str(node.get("content"))) if node else None


def _itemprop_text(soup: BeautifulSoup | Tag, prop: str) -> str | None:
    node = soup.select_one(f'[itemprop="{prop}"]')
    if node is None:
        return None
    if node.name == "meta":
        return _clean_text(str(node.get("content") or "")) or None
    return _clean_text(node.get_text(" ")) or None


def _is_placeholder_cover(url: str) -> bool:
    lowered = url.lower()
    return "nocover" in lowered or "no_image" in lowered or "noimage" in lowered


@register_provider("databazeknih")
class DatabazeknihProvider(MetadataProvider):
    """Databazeknih.cz metadata provider (HTML scraping, Czech/Slovak catalog)."""

    name = "databazeknih"
    display_name = "Databazeknih.cz"
    requires_auth = False
    supported_sorts: ClassVar[tuple[SortOrder, ...]] = (SortOrder.RELEVANCE,)
    search_fields: ClassVar[tuple[SearchField, ...]] = (
        TextSearchField(
            key="author",
            label="Author",
            description="Search by author name",
        ),
        TextSearchField(
            key="title",
            label="Title",
            description="Search by book title",
        ),
    )

    def __init__(self) -> None:
        """Initialize provider."""
        self.session = requests.Session()
        self.session.headers.update(REQUEST_HEADERS)

    def is_available(self) -> bool:
        """Databazeknih.cz needs no authentication."""
        return True

    def _fetch(self, url: str, timeout: int = 15, *, quiet: bool = False) -> str | None:
        _rate_limiter.wait_if_needed()
        try:
            response = self.session.get(url, timeout=timeout, verify=get_ssl_verify(DBK_BASE_URL))
            response.raise_for_status()
        except requests.Timeout:
            logger.warning("Databazeknih.cz request timed out: %s", url)
            return None
        except requests.RequestException:
            if quiet:
                logger.debug("Databazeknih.cz optional request failed: %s", url)
            else:
                logger.exception("Databazeknih.cz request failed: %s", url)
            return None
        return response.text

    def search(self, options: MetadataSearchOptions) -> list[BookMetadata]:
        """Search databazeknih.cz's site search."""
        if options.search_type == SearchType.ISBN:
            result = self.search_by_isbn(options.query)
            return [result] if result else []

        # The site search returns a single ranked page.
        if options.page > 1:
            return []

        author_value = (options.fields.get("author") or "").strip()
        title_value = (options.fields.get("title") or "").strip()
        terms = " ".join(t for t in (title_value, author_value) if t)
        query = terms or options.query.strip()
        if not query:
            return []

        fields_key = ":".join(f"{k}={v}" for k, v in sorted(options.fields.items()))
        cache_key = f"{query}:{options.search_type.value}:{options.limit}:{fields_key}"
        return self._search_cached(cache_key, query, options.limit) or []

    @cacheable(ttl_key="METADATA_CACHE_SEARCH_TTL", ttl_default=300, key_prefix="dbk:search")
    def _search_cached(self, cache_key: str, query: str, limit: int) -> list[BookMetadata] | None:
        # Return None (not []) on fetch failure so the failure is not cached.
        html = self._fetch(DBK_SEARCH_URL + quote(query.encode("utf-8")))
        if html is None:
            return None

        soup = BeautifulSoup(html, "html.parser")

        # An unambiguous query can land straight on a book page.
        direct = self._book_slug_from_page(soup)
        if direct:
            book = self._parse_book_page(soup, direct)
            return [book] if book else []

        books: list[BookMetadata] = []
        seen: set[str] = set()
        covers = self._result_covers(soup)

        for anchor in soup.select("a[href]"):
            match = _BOOK_HREF_RE.search(str(anchor.get("href") or ""))
            if not match:
                continue
            slug = match.group(1)
            title = _clean_text(anchor.get_text(" "))
            # Cover-image links share the href but carry no text.
            if slug in seen or not title:
                continue

            seen.add(slug)
            author, year = self._result_author_year(anchor, title)
            books.append(
                BookMetadata(
                    provider=self.name,
                    provider_id=slug,
                    provider_display_name=self.display_name,
                    title=title,
                    authors=[author] if author else [],
                    cover_url=covers.get(slug),
                    publish_year=year,
                    source_url=DBK_BOOK_URL + slug,
                    search_title=title,
                    search_author=author,
                )
            )
            if len(books) >= limit:
                break

        logger.info("Databazeknih.cz search '%s' returned %s results", query, len(books))
        return books

    def _result_covers(self, soup: BeautifulSoup) -> dict[str, str]:
        covers: dict[str, str] = {}
        for img in soup.select("a[href] img[src]"):
            anchor = img.find_parent("a")
            if anchor is None:
                continue
            match = _BOOK_HREF_RE.search(str(anchor.get("href") or ""))
            src = _absolute_url(str(img.get("data-src") or img.get("src") or ""))
            if match and src and not _is_placeholder_cover(src):
                covers.setdefault(match.group(1), src)
        return covers

    def _result_author_year(self, anchor: Tag, title: str) -> tuple[str | None, int | None]:
        """Read the 'year, author' line printed next to a search result title."""
        container = anchor.parent
        if container is None:
            return None, None

        author_link = container.select_one('a[href*="autori/"]')
        author = _clean_text(author_link.get_text(" ")) if author_link else None

        info = container.select_one(".smallfind, .pozn, small")
        text = _clean_text((info or container).get_text(" ")) or ""
        if info is None:
            text = text.replace(title, "", 1)

        year_match = _YEAR_RE.search(text)
        year = int(year_match.group(1)) if year_match else None

        if not author:
            remainder = _YEAR_RE.sub("", text)
            author = " ".join(remainder.replace(",", " ").split()) or None
        return author, year

    @cacheable(ttl_key="METADATA_CACHE_BOOK_TTL", ttl_default=600, key_prefix="dbk:book")
    def get_book(self, book_id: str) -> BookMetadata | None:
        """Get book details by databazeknih.cz slug (e.g. 'saturnin-15')."""
        html = self._fetch(DBK_BOOK_URL + quote(book_id))
        if html is None:
            return None
        return self._parse_book_page(BeautifulSoup(html, "html.parser"), book_id)

    @cacheable(ttl_key="METADATA_CACHE_BOOK_TTL", ttl_default=600, key_prefix="dbk:isbn")
    def search_by_isbn(self, isbn: str) -> BookMetadata | None:
        """The site search resolves ISBN queries."""
        isbn = isbn.replace("-", "").strip()
        if not isbn:
            return None
        html = self._fetch(DBK_SEARCH_URL + quote(isbn))
        if html is None:
            return None
        soup = BeautifulSoup(html, "html.parser")

        direct = self._book_slug_from_page(soup)
        if direct:
            return self._parse_book_page(soup, direct)

        for anchor in soup.select("a[href]"):
            match = _BOOK_HREF_RE.search(str(anchor.get("href") or ""))
            if match:
                return self.get_book(match.group(1))
        return None

    def _book_slug_from_page(self, soup: BeautifulSoup) -> str | None:
        """Return the slug if this page is a book detail page, else None."""
        if soup.select_one('h1[itemprop="name"]') is None:
            return None
        for selector, attr in (
            ('link[rel="canonical"]', "href"),
            ('meta[property="og:url"]', "content"),
        ):
            node = soup.select_one(selector)
            if node:
                match = _BOOK_HREF_RE.search(str(node.get(attr) or ""))
                if match:
                    return match.group(1)
        return None

    def _parse_book_page(self, soup: BeautifulSoup, book_id: str) -> BookMetadata | None:
        title = self._parse_title(soup)
        if not title:
            logger.warning("Databazeknih.cz book page missing title: %s", book_id)
            return None

        authors = self._parse_authors(soup)
        isbn_13, isbn_10 = self._parse_isbns(soup)
        publisher = _itemprop_text(soup, "publisher")
        publish_year = self._parse_year(soup)

        if not (isbn_13 or isbn_10) or not publisher:
            more = self._fetch_more_info(book_id)
            if more is not None:
                if not (isbn_13 or isbn_10):
                    isbn_13, isbn_10 = self._parse_isbns(more)
                publisher = publisher or _itemprop_text(more, "publisher")
                publish_year = publish_year or self._parse_year(more)

        series_name, series_position = self._parse_series(soup)
        genres = self._parse_genres(soup)

        display_fields: list[DisplayField] = []
        rating = self._parse_rating(soup)
        if rating:
            display_fields.append(DisplayField(label="Rating", value=rating, icon="star"))
        pages = _itemprop_text(soup, "numberOfPages")
        if pages:
            display_fields.append(DisplayField(label="Pages", value=pages, icon="book"))
        if series_name:
            series_label = series_name
            if series_position is not None:
                series_label = f"{series_name} {series_position:g}."
            display_fields.append(DisplayField(label="Series", value=series_label, icon="editions"))

        return BookMetadata(
            provider=self.name,
            provider_id=book_id,
            provider_display_name=self.display_name,
            title=title,
            authors=authors,
            isbn_13=isbn_13,
            isbn_10=isbn_10,
            cover_url=self._parse_cover(soup),
            description=self._parse_description(soup),
            publisher=publisher,
            publish_year=publish_year,
            language=self._parse_language(soup),
            genres=genres,
            source_url=DBK_BOOK_URL + book_id,
            series_name=series_name,
            series_position=series_position,
            search_title=title,
            search_author=authors[0] if authors else None,
            display_fields=display_fields,
        )

    def _fetch_more_info(self, book_id: str) -> BeautifulSoup | None:
        match = re.search(r"-(\d+)$", book_id)
        if not match:
            return None
        html = self._fetch(DBK_MORE_INFO_URL + match.group(1), quiet=True)
        return BeautifulSoup(html, "html.parser") if html else None

    def _parse_title(self, soup: BeautifulSoup) -> str | None:
        node = soup.select_one('h1[itemprop="name"]') or soup.select_one("h1")
        if node:
            # Edition/series badges are nested tags; only direct text is the title.
            direct = _clean_text("".join(node.find_all(string=True, recursive=False)))
            title = direct or _clean_text(node.get_text(" "))
            if title:
                return title
        og_title = _meta_content(soup, "og:title")
        if og_title:
            return og_title.split(" | ")[0].strip() or None
        return None

    def _parse_authors(self, soup: BeautifulSoup) -> list[str]:
        nodes = soup.select(
            '[itemprop="author"] [itemprop="name"], a[itemprop="author"], '
            '[itemprop="author"] a, h2.jmenaautoru a'
        )
        authors: list[str] = []
        for node in nodes:
            name = _clean_text(node.get_text(" "))
            if name and name not in authors:
                authors.append(name)
        return authors

    def _parse_isbns(self, soup: BeautifulSoup) -> tuple[str | None, str | None]:
        isbn_13 = isbn_10 = None
        candidates = [n.get_text(" ") for n in soup.select('[itemprop="isbn"]')]
        candidates += re.findall(r"ISBN[:\s]*([\dXx-]{10,17})", soup.get_text(" "))
        for raw in candidates:
            for candidate in re.findall(r"[\dXx-]{10,17}", raw):
                isbn = _valid_isbn(candidate)
                if not isbn:
                    continue
                if len(isbn) == ISBN_13_LENGTH and not isbn_13:
                    isbn_13 = isbn
                elif len(isbn) != ISBN_13_LENGTH and not isbn_10:
                    isbn_10 = isbn
        return isbn_13, isbn_10

    def _parse_year(self, soup: BeautifulSoup) -> int | None:
        text = _itemprop_text(soup, "datePublished")
        if text:
            match = _YEAR_RE.search(text)
            if match:
                return int(match.group(1))
        return None

    def _parse_cover(self, soup: BeautifulSoup) -> str | None:
        for selector, attr in (
            ("img.kniha_img", "src"),
            ('img[itemprop="image"]', "src"),
            ('meta[property="og:image"]', "content"),
        ):
            node = soup.select_one(selector)
            if node:
                url = _absolute_url(str(node.get(attr) or ""))
                if url and not _is_placeholder_cover(url):
                    return url
        return None

    def _parse_description(self, soup: BeautifulSoup) -> str | None:
        node = soup.select_one('[itemprop="description"]')
        if node is not None and node.name != "meta":
            # The full text sits in a hidden span next to a truncated preview.
            full = node.select_one(".end_text, .celytext")
            text = _clean_text((full or node).get_text(" ")) or ""
        else:
            text = _meta_content(soup, "og:description") or ""
        for noise in _DESCRIPTION_NOISE:
            if text.endswith(noise):
                text = text[: -len(noise)].rstrip()
        return text or None

    def _parse_series(self, soup: BeautifulSoup) -> tuple[str | None, float | None]:
        node = soup.select_one('a[href*="serie/"]')
        if node is None:
            return None, None
        name = _clean_text(node.get_text(" "))
        if not name:
            return None, None
        position = None
        context = node.parent.get_text(" ") if node.parent else ""
        match = _SERIES_POSITION_RE.search(context)
        if match:
            position = float(match.group(1).replace(",", "."))
        return name, position

    def _parse_genres(self, soup: BeautifulSoup) -> list[str]:
        genres: list[str] = []
        nodes = soup.select('[itemprop="genre"]') or soup.select('a[href*="zanry/"]')
        for node in nodes:
            for part in node.get_text(",").split(","):
                genre = _clean_text(part)
                if genre and genre not in genres:
                    genres.append(genre)
        return genres

    def _parse_rating(self, soup: BeautifulSoup) -> str | None:
        node = soup.select_one(".bpoints, .hodnoceni_rating")
        if node:
            return _clean_text(node.get_text(" "))
        value = _itemprop_text(soup, "ratingValue")
        if value:
            return value if value.endswith("%") else f"{value}%"
        return None

    def _parse_language(self, soup: BeautifulSoup) -> str:
        text = (_itemprop_text(soup, "inLanguage") or "").lower()
        for word in text.replace(",", " ").split():
            code = _LANGUAGE_MAP.get(word)
            if code:
                return code
        return "cs"


def _test_databazeknih_connection() -> dict[str, Any]:
    """Test connectivity to databazeknih.cz."""
    try:
        provider = DatabazeknihProvider()
        response = provider.session.get(
            DBK_SEARCH_URL + quote("test"),
            timeout=10,
            verify=get_ssl_verify(DBK_BASE_URL),
        )
        response.raise_for_status()
    except requests.Timeout:
        return {"success": False, "message": "Connection timed out"}
    except requests.RequestException as e:
        return {"success": False, "message": f"Connection failed: {e}"}
    if "databazeknih" in response.text.lower():
        return {"success": True, "message": "Successfully connected to databazeknih.cz"}
    return {"success": False, "message": "Unexpected response from databazeknih.cz"}


@register_settings(
    "databazeknih", "Databazeknih.cz", icon="library", order=55, group="metadata_providers"
)
def databazeknih_settings() -> list[SettingsField]:
    """Databazeknih.cz metadata provider settings."""
    return [
        HeadingField(
            key="databazeknih_heading",
            title="Databazeknih.cz",
            description=(
                "Czech community book catalog with excellent coverage of Czech and "
                "Slovak editions and translations. No API key required."
            ),
            link_url="https://www.databazeknih.cz",
            link_text="databazeknih.cz",
        ),
        CheckboxField(
            key="DATABAZEKNIH_ENABLED",
            label="Enable Databazeknih.cz",
            description="Enable Databazeknih.cz as a metadata provider for book searches",
            default=False,
        ),
        ActionButton(
            key="test_connection",
            label="Test Connection",
            description="Verify databazeknih.cz is accessible",
            style="primary",
            callback=_test_databazeknih_connection,
        ),
    ]
