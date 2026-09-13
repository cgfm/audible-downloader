# Bugfixes in this build

This build keeps the original application architecture and fixes two failure modes:

1. **Multipart audiobooks**
   - Each Audible ASIN downloads into its own temporary working directory.
   - Multiple `.aax` / `.aaxc` files returned for one ASIN are treated as parts of one audiobook.
   - AAXC files use the voucher with the exact same basename instead of an arbitrary voucher from the download directory.
   - In `AUDIOBOOK_FOLDERS=False` mode, multipart titles are put into one dedicated folder so Audiobookshelf imports the parts as one book.
   - The database is marked downloaded only after every part converted successfully.

2. **Already downloaded books**
   - Before downloading, the program scans `/audiobooks` recursively for existing `.m4b` files created with the `asin_ascii` filename convention.
   - ASINs found on disk are marked downloaded in SQLite, so a recreated or incomplete `audiobooks.db` does not cause those books to be downloaded again.

## Important distinction

The multipart fix applies when Audible returns several audio files for **one library ASIN** (a technical multipart book). If Audible exposes "Teil 1" and "Teil 2" as two completely separate library products with two different ASINs, they are still separate books; automatically merging different ASINs would require an additional title/series rule and can otherwise merge genuine series volumes by mistake.

5. **Cleaner folder naming**
   - Narrators are no longer appended to audiobook folder names by default.
   - Set `AUDIOBOOK_FOLDER_INCLUDE_NARRATOR=True` to opt into the previous behavior.
   - Path components are sanitized before creating directories.

6. **Configurable output path templates**
   - `SINGLE_BOOK_PATH_TEMPLATE` controls standalone books.
   - `SERIES_BOOK_PATH_TEMPLATE` controls books that have Audible series metadata.
   - Supported placeholders: `{author}`, `{title}`, `{subtitle}`, `{series}`, `{series_index}`, `{year}`, `{asin}`, `{narrator}`.
   - Literal/free text is preserved, so templates such as `Band {series_index} - {title}` work directly.
   - `/` in a template creates directory levels; metadata values are sanitized so they cannot accidentally create extra levels.
   - Technical multipart books receive ` - Part 01`, ` - Part 02`, ... suffixes when needed to avoid filename collisions.
   - Already-downloaded reconciliation now understands the configured template destinations as well as legacy ASIN-based filenames.

## Optional one-zero series padding

Path templates now also support `{series_index_padded}`. It pads single-digit integer volume numbers to two characters (`1` -> `01`) but does not guess a larger series width. `{series_index}` remains available unchanged.
