"""Deterministic Gutenberg RDF/archive fixtures for the `gutenberg` lab suite.

Every element/namespace here was read directly out of the real parser
(`GutenbergCatalogSynchronizer.ParseBook`, family-librarian main @ 5e9823f),
not guessed from the Gutenberg catalog implementation plan -- that plan
predates the real implementation and gets some details wrong (see the
session notes: HTTPS-only URLs, MinimumBookCount's real default, etc.). Keep
this file in sync with the parser if it changes; a fixture that silently
doesn't match `ParseBook`'s expectations fails as a *skipped* record, not a
loud error (a blank/missing title is dropped without being counted as a
parse error), which would otherwise look like a passing sync that quietly
imported fewer books than intended.
"""

from __future__ import annotations

import io
import tarfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from family_librarian_lab.fixtures import multi_track_audiobook

RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
DCTERMS_NS = "http://purl.org/dc/terms/"
PGTERMS_NS = "http://www.gutenberg.org/2009/pgterms/"


@dataclass(frozen=True, slots=True)
class Person:
    name: str
    birth_year: int | None = None
    death_year: int | None = None


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One `pgterms:file`. `source_path` is the absolute path Gutenberg's
    RDF uses (e.g. "/files/10001/10001-8.txt" -- the "historic" convention
    GutenbergFileResolver keeps verbatim for www.gutenberg.org, or
    "/ebooks/10003.epub3.images" -- the "generated" shorthand it expands
    per-mirror). Real record: `rdf:about` is `https://www.gutenberg.org` +
    this path -- TryGetSourcePath only cares about the URI's AbsolutePath,
    not which host serves it, so this host is fixed and not configurable.
    """

    source_path: str
    mime_type: str
    extent_bytes: int = 1024


@dataclass(frozen=True, slots=True)
class Book:
    gutenberg_id: int
    title: str
    authors: tuple[Person, ...] = ()
    editors: tuple[Person, ...] = ()
    translators: tuple[Person, ...] = ()
    languages: tuple[str, ...] = ("en",)
    media_type: str = "Text"
    rights_text: str | None = "Public domain in the USA."
    downloads: int | None = 100
    files: tuple[FileEntry, ...] = ()


def _person_xml(tag: str, people: tuple[Person, ...]) -> str:
    parts = []
    for person in people:
        agent_children = [f"<pgterms:name>{escape(person.name)}</pgterms:name>"]
        if person.birth_year is not None:
            agent_children.append(f"<pgterms:birthdate>{person.birth_year}</pgterms:birthdate>")
        if person.death_year is not None:
            agent_children.append(f"<pgterms:deathdate>{person.death_year}</pgterms:deathdate>")
        parts.append(f"<{tag}><pgterms:agent>{''.join(agent_children)}</pgterms:agent></{tag}>")
    return "".join(parts)


def build_rdf(book: Book) -> bytes:
    """One book's RDF/XML document, matching ParseBook's expectations
    exactly: `pgterms:ebook/@rdf:about` ends in the numeric Gutenberg id
    (`TryGetGutenbergId` just splits on '/' and parses the last segment --
    "ebooks/{id}" is the real convention, but any trailing-id path works)."""
    creators = _person_xml("dcterms:creator", book.authors)
    contributors = _person_xml("dcterms:contributor", book.editors)
    translators = _person_xml("dcterms:translator", book.translators)
    languages = "".join(
        f'<dcterms:language><rdf:Description><rdf:value>{escape(lang)}</rdf:value></rdf:Description></dcterms:language>'
        for lang in book.languages
    )
    rights = f"<dcterms:rights>{escape(book.rights_text)}</dcterms:rights>" if book.rights_text else ""
    downloads = f"<pgterms:downloads>{book.downloads}</pgterms:downloads>" if book.downloads is not None else ""
    files = "".join(
        f'<pgterms:file rdf:about="https://www.gutenberg.org{escape(f.source_path)}">'
        f'<dcterms:format><rdf:Description><rdf:value>{escape(f.mime_type)}</rdf:value></rdf:Description></dcterms:format>'
        f'<dcterms:extent>{f.extent_bytes}</dcterms:extent>'
        f'<dcterms:modified>2020-01-01T00:00:00</dcterms:modified>'
        f'</pgterms:file>'
        for f in book.files
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<rdf:RDF xmlns:rdf="{RDF_NS}" xmlns:dcterms="{DCTERMS_NS}" xmlns:pgterms="{PGTERMS_NS}">'
        f'<pgterms:ebook rdf:about="ebooks/{book.gutenberg_id}">'
        f'<dcterms:title>{escape(book.title)}</dcterms:title>'
        f'{creators}{contributors}{translators}'
        f'<dcterms:type><rdf:Description><rdf:value>{escape(book.media_type)}</rdf:value></rdf:Description></dcterms:type>'
        f'{languages}{rights}{downloads}'
        f'</pgterms:ebook>'
        f'{files}'
        '</rdf:RDF>'
    )
    return xml.encode("utf-8")


def build_archive(books: list[Book]) -> bytes:
    """Package `books` into a real `.tar.bz2`, matching what
    `GutenbergCatalogSynchronizer` actually reads: shells out to `bzip2
    --decompress --stdout` then parses via `System.Formats.Tar.TarReader`,
    treating any regular-file entry named `*.rdf` (anywhere in the tar, no
    directory convention required) as one book. Python's `tarfile`
    "w:bz2" mode produces a standard bzip2-compressed tar, byte-for-byte
    the same shape a real decompressor expects."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:bz2") as archive:
        for book in books:
            rdf_bytes = build_rdf(book)
            info = tarfile.TarInfo(name=f"{book.gutenberg_id}/pg{book.gutenberg_id}.rdf")
            info.size = len(rdf_bytes)
            info.mtime = 1577836800  # 2020-01-01T00:00:00Z, deterministic
            archive.addfile(info, io.BytesIO(rdf_bytes))
    return buffer.getvalue()


def build_recent_updates_rss(gutenberg_ids: list[int]) -> bytes:
    """The incremental-sync feed: an RSS 2.0 document with one `<item><link>`
    per updated id, matching `DownloadRecentUpdatesAsync`'s real parsing
    (`XDocument`, `Descendants("item")`, `.Element("link")`, ids extracted
    from a `.../ebooks/{id}` link shape)."""
    items = "".join(
        f"<item><link>https://www.gutenberg.org/ebooks/{gid}</link></item>" for gid in gutenberg_ids
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Project Gutenberg Recent Updates</title>'
        f"{items}"
        "</channel></rss>"
    )
    return xml.encode("utf-8")


def diversity_books() -> list[Book]:
    """Records covering every shape the Gutenberg plan's own diversity list
    calls for (multiple authors, translator, editor, multiple languages, no
    EPUB, unknown copyright, missing optional fields, a historic /files/
    URL) that don't need to resolve to a specific demo Work -- spread across
    a handful of books rather than one-per-item, matching how a real archive
    mixes shapes. GUT-05/GUT-06's own target books (which DO need to match a
    real demo catalog title so `resolve_demo_work()` can find them) are
    built separately in test_gutenberg.py, next to the demo-slug they must
    match -- keeping that product-test coupling out of this generic module.
    """
    return [
        # Multiple authors, a translator, an editor (dcterms:contributor),
        # multiple languages, historic /files/ URL.
        Book(
            gutenberg_id=10002,
            title="Little Women",
            authors=(Person("Louisa May Alcott"), Person("Anna Katharine Green")),
            editors=(Person("Some Editor"),),
            translators=(Person("Some Translator"),),
            languages=("en", "fr"),
            files=(FileEntry("/files/10002/10002-images.epub", "application/epub+zip"),),
        ),
        # No EPUB format at all (plain text only).
        Book(
            gutenberg_id=10005,
            title="Text Only No Epub",
            authors=(Person("Plain Text Author"),),
            files=(FileEntry("/files/10005/10005-8.txt", "text/plain"),),
        ),
        # Unknown copyright (rights text matches neither "public domain" nor
        # "copyright").
        Book(
            gutenberg_id=10006,
            title="Unknown Copyright Test",
            authors=(Person("Unclear Rights Author"),),
            rights_text="See the archived page for further details.",
            files=(FileEntry("/files/10006/10006-images.epub", "application/epub+zip"),),
        ),
        # Missing optional fields: no downloads count, no rights element.
        Book(
            gutenberg_id=10007,
            title="Minimal Fields Test",
            authors=(Person("Minimal Author"),),
            rights_text=None,
            downloads=None,
            files=(FileEntry("/files/10007/10007-images.epub", "application/epub+zip"),),
        ),
    ]


# family_librarian_lab.api.FamilyLibrarianApi.resolve_demo_work() can only
# resolve DemoBookMetadataProvider's fixed catalog entries ("the-hobbit",
# "a-wrinkle-in-time", "project-hail-mary") -- there is no endpoint that
# resolves an arbitrary title. A case that needs its Gutenberg fixture to
# come back through `fulfillment_options()` must therefore give its book
# one of those exact titles/authors. "the-hobbit" is left alone here since
# every other suite already targets it; these two are dedicated to the
# gutenberg suite so its cases stay easy to reason about in isolation.
SEARCH_TARGET_BOOKS = [
    # GUT-05's preference-order target: three EPUB formats, deliberately
    # archived out of preference order (no-images first) so passing requires
    # real preference logic, not archive order. Also GUT-01/GUT-02's
    # searchable/acquirable example.
    Book(
        gutenberg_id=10003,
        title="Project Hail Mary",
        authors=(Person("Andy Weir"),),
        files=(
            FileEntry("/ebooks/10003.epub", "application/epub+zip"),
            FileEntry("/ebooks/10003.epub.images", "application/epub+zip"),
            FileEntry("/ebooks/10003.epub3.images", "application/epub+zip"),
        ),
    ),
    # GUT-06's non-ebook-search target: a real catalog entry whose title
    # matches a real demo Work, but classified Sound -- proving it's
    # excluded from an *ebook*-media-type fulfillment lookup specifically,
    # not merely absent from the catalog.
    Book(
        gutenberg_id=10004,
        title="A Wrinkle in Time",
        authors=(Person("Madeleine L'Engle"),),
        media_type="Sound",
        files=(FileEntry("/files/10004/10004-64kb.mp3", "audio/mpeg"),),
    ),
    # ABS-05's direct-acquisition target: a realistic, chaptered Sound
    # record whose three separate MP3 files must remain one ordered
    # Audiobookshelf item.  The source paths deliberately use Gutenberg's
    # public /files layout so the real mirror resolver has to translate them
    # to its split-digit mirror layout before fetching the fixture payloads.
    Book(
        gutenberg_id=10008,
        title="The Hobbit",
        authors=(Person("J. R. R. Tolkien"),),
        media_type="Sound",
        files=tuple(
            FileEntry(f"/files/10008/mp3/10008-{index:02}.mp3", "audio/mpeg", len(content))
            for index, (_, content) in enumerate(multi_track_audiobook(), start=1)
        ),
    ),
]


def multi_track_mirror_files() -> tuple[tuple[str, bytes], ...]:
    """Return the real mirror paths and payloads for ABS-05's audio bundle.

    GutenbergFileResolver translates /files/{id}/... to this split-digit
    layout for every mirror other than www.gutenberg.org.  Keeping the map
    beside the matching RDF record makes the fixture prove that translation
    as well as the bundle pipeline, rather than bypassing it with a custom
    provider stub.
    """
    return tuple(
        (f"/1/0/0/0/8/10008/mp3/10008-{index:02}.mp3", content)
        for index, (_, content) in enumerate(multi_track_audiobook(), start=1)
    )
