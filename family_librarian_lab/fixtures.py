"""Small deterministic fixture payloads for black-box lab scenarios.

The EPUB builders deliberately use fixed ZIP timestamps and entry ordering, so
their bytes and checksums do not vary between runs.  They carry the metadata
of Family Librarian's local ``demo/the-hobbit`` catalog entry; that is the only
deterministic catalog work available through the product's public API today.
"""

from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo


EICAR_TEST_STRING = (
    b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
)


def clean_epub() -> bytes:
    """Return a minimal valid EPUB whose package identity matches The Hobbit."""
    return _build_epub(chapter_text="A deterministic, harmless lab fixture.")


def eicar_epub() -> bytes:
    """Return a structurally valid EPUB containing the standard EICAR signature."""
    return _build_epub(chapter_text=EICAR_TEST_STRING.decode("ascii"))


def _build_epub(*, chapter_text: str) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        _write(archive, "mimetype", b"application/epub+zip", ZIP_STORED)
        _write(
            archive,
            "META-INF/container.xml",
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            b'<rootfiles><rootfile full-path="OEBPS/content.opf" '
            b'media-type="application/oebps-package+xml"/></rootfiles></container>',
            ZIP_DEFLATED,
        )
        _write(
            archive,
            "OEBPS/content.opf",
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">'
            b'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            b'<dc:identifier id="book-id">urn:uuid:family-librarian-lab-the-hobbit</dc:identifier>'
            b'<dc:title>The Hobbit</dc:title><dc:creator>J. R. R. Tolkien</dc:creator>'
            b'<dc:language>en</dc:language></metadata><manifest>'
            b'<item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
            b'</manifest><spine><itemref idref="chapter"/></spine></package>',
            ZIP_DEFLATED,
        )
        chapter = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>The Hobbit</title></head>'
            f"<body><p>{chapter_text}</p></body></html>"
        ).encode("utf-8")
        _write(archive, "OEBPS/chapter.xhtml", chapter, ZIP_DEFLATED)
    return output.getvalue()


def _write(archive: ZipFile, name: str, content: bytes, compression: int) -> None:
    entry = ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
    entry.compress_type = compression
    archive.writestr(entry, content)
