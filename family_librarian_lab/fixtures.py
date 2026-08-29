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
    """Return a structurally valid EPUB containing the standard EICAR signature.

    The signature must be the chapter entry's *entire* raw content, not text
    wrapped inside the usual XHTML template: confirmed directly against a
    real ClamAV instance that wrapping the identical bytes in
    ``<html>...<body><p>...</p></body></html>`` reliably defeats detection
    (clean "OK" verdict) while the same bytes as a bare entry are reliably
    flagged ("Eicar-Test-Signature FOUND") -- ClamAV does not treat every
    byte-for-byte occurrence of the signature the same once it is embedded in
    HTML markup. `clean_epub`/`identity_mismatched_epub`/`large_epub` don't
    need real detection to fire, so they keep the normal HTML-wrapped chapter.
    """
    return _build_epub(chapter_text=EICAR_TEST_STRING.decode("ascii"), wrap_chapter_in_html=False)


def invalid_epub() -> bytes:
    """Return a ZIP-shaped EPUB that fails structural EPUB validation.

    The HTTP upload boundary rightly rejects arbitrary non-ZIP bytes before
    they become a MediaAsset.  This fixture gets past that shallow signature
    check so the real security pipeline records its scanner and validator
    evidence before rejecting the malformed package.
    """
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
        _write(archive, "OEBPS/content.opf", b"This is deliberately not OPF XML.", ZIP_DEFLATED)
    return output.getvalue()


def identity_mismatched_epub() -> bytes:
    """Return a well-formed EPUB whose package metadata is not The Hobbit."""
    return _build_epub(
        chapter_text="A valid fixture whose package identity must not match the requested work.",
        title="A Tale of Two Cities",
        author="Charles Dickens",
        identifier="urn:uuid:family-librarian-lab-mismatch",
    )


def large_epub() -> bytes:
    """Return a valid EPUB large enough to observe a real handoff in flight.

    The deliberately incompressible stored payload prevents ZIP compression
    from turning a nominally large synthetic fixture into a few kilobytes.
    It remains comfortably below the lab ClamAV stream limit.
    """
    return _build_epub(
        chapter_text="A deterministic large-fixture chapter.",
        extra_payload=bytes(range(256)) * (128 * 1024),  # 32 MiB
    )


def clean_audiobook(*, repeat: int = 20) -> bytes:
    """Return a deterministic, genuinely ffprobe-decodable MP3 (repeated
    MPEG-1 Layer III frame headers -- silent, not intended for listening).

    A bare ID3v2 tag or arbitrary bytes with a ".mp3" extension is not
    enough: Audiobookshelf's own scanner runs ffprobe against the real
    content and drops the file with "Invalid data found when processing
    input" if it can't decode a stream (confirmed against a real
    Audiobookshelf instance) -- matching
    FamilyLibrarian.Infrastructure.Acquisition.SignatureFileTypeDetector's
    own bare-MPEG-frame-sync magic-byte check on Family Librarian's side.

    ``repeat`` controls the file's frame count/size -- letting
    `multi_track_audiobook()` build tracks that are still each individually
    genuinely decodable but distinguishable from one another by size, so a
    bundle scenario can assert order was preserved rather than merely that
    three tracks arrived.
    """
    # MPEG-1 Layer III, 128kbps, 44100Hz, mono, no CRC, no padding.
    frame_header = bytes([0xFF, 0xFB, 0x90, 0xC0])
    frame_size = 417  # floor(144 * 128000 / 44100)
    frame = frame_header + b"\x00" * (frame_size - len(frame_header))
    return frame * repeat  # 20 repeats is ~0.5s -- enough for ffprobe to report a duration


def malformed_but_plausible_mp3() -> bytes:
    """Return an MP3 that clears the shallow magic-byte content-type sniff
    (a real MPEG frame sync) but fails `AudioValidator`'s structural check.

    Distinct from `malformed_audiobook()`, which fails even the shallow
    sniff -- this exercises the deeper structural gate specifically (mirrors
    how `identity_mismatched_epub()` exercises identity verification
    separately from `invalid_epub()`'s structural rejection). The first
    frame header is genuinely valid (same header `clean_audiobook()` uses),
    but everything after it is zero bytes: `AudioValidator` requires a
    second structurally valid frame header at the first frame's declared
    length, and an all-zero tail can never produce one (no `0xFF` byte to
    even start a candidate sync), so this fails "No valid MPEG audio frame
    was found" rather than the file simply being too short to check --
    confirmed against `AudioValidator.ValidateMp3Async`'s logic.
    """
    frame_header = bytes([0xFF, 0xFB, 0x90, 0xC0])
    frame_size = 417
    first_frame = frame_header + b"\x00" * (frame_size - len(frame_header))
    return first_frame + b"\x00" * 2_000


def malformed_but_plausible_m4b() -> bytes:
    """Return an M4B that clears the shallow magic-byte content-type sniff
    (a real `ftyp` box) but fails `AudioValidator`'s structural check.

    A real encoder failure plausibly stops after writing `ftyp` (the first
    box any ISO-BMFF writer emits) without ever reaching `moov` -- this
    fixture is exactly that shape, confirmed against
    `AudioValidator.ValidateM4bAsync`'s "has no moov box" rejection.
    """
    ftyp_payload = b"M4B \x00\x00\x02\x00isomiso2mp41"
    ftyp_box = _u32(8 + len(ftyp_payload)) + b"ftyp" + ftyp_payload
    return ftyp_box


def _u32(value: int) -> bytes:
    return value.to_bytes(4, "big")


def malformed_audiobook() -> bytes:
    """Return bytes that cannot pass Family Librarian's audio type gate.

    Keeping this distinct from the valid MP3 fixture makes ABS-07 prove that
    an explicitly rejected audio submission has no publishing side effect.
    """
    return b"This is deliberately not an MP3 or any other supported audio format."


def multi_track_audiobook() -> tuple[tuple[str, bytes], ...]:
    """Return ordered, independently decodable tracks for bundle scenarios.

    Each track has a distinct frame count (and so a distinct, checkable
    byte size) rather than being byte-identical copies -- three identical
    tracks can prove a bundle of three arrived, but cannot prove *order*
    survived the pipeline. A scenario can assert on `len(content)` (or a
    downstream duration/size an API exposes) to confirm track N really is
    track N, not just "some track."
    """
    return (
        ("01-the-hobbit.mp3", clean_audiobook(repeat=15)),
        ("02-the-hobbit.mp3", clean_audiobook(repeat=20)),
        ("03-the-hobbit.mp3", clean_audiobook(repeat=25)),
    )


def _build_epub(
    *,
    chapter_text: str,
    title: str = "The Hobbit",
    author: str = "J. R. R. Tolkien",
    identifier: str = "urn:uuid:family-librarian-lab-the-hobbit",
    extra_payload: bytes | None = None,
    wrap_chapter_in_html: bool = True,
) -> bytes:
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
            + f'<dc:identifier id="book-id">{identifier}</dc:identifier>'.encode("utf-8")
            + f'<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>'.encode("utf-8")
            + b'<dc:language>en</dc:language></metadata><manifest>'
            + b'<item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
            + b'</manifest><spine><itemref idref="chapter"/></spine></package>',
            ZIP_DEFLATED,
        )
        if wrap_chapter_in_html:
            chapter = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>The Hobbit</title></head>'
                f"<body><p>{chapter_text}</p></body></html>"
            ).encode("utf-8")
        else:
            chapter = chapter_text.encode("utf-8")
        _write(archive, "OEBPS/chapter.xhtml", chapter, ZIP_DEFLATED)
        if extra_payload is not None:
            _write(archive, "OEBPS/transfer-observer.bin", extra_payload, ZIP_STORED)
    return output.getvalue()


def _write(archive: ZipFile, name: str, content: bytes, compression: int) -> None:
    entry = ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
    entry.compress_type = compression
    archive.writestr(entry, content)
