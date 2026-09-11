# Audible Downloader

A Docker container that automatically downloads Audible audiobooks, converts AAX/AAXC to M4B and stores them below `/audiobooks`.

The program checks periodically for new books in the Audible library. Technical multipart books returned as several AAX/AAXC files for one ASIN are kept together as one audiobook.

It also provides a small server-rendered web interface for searching the library,
refreshing Audible metadata, inspecting processing errors and manually converting
or reconverting one title at a time.

## Build

```sh
docker build -t audible-downloader .
```

Example run:

```sh
docker run -d \
  --name=audiobookDownloader \
  -p 8080:8080 \
  -e AUDIOBOOK_FOLDERS='True' \
  -e SINGLE_BOOK_PATH_TEMPLATE='{author}/{title}/{title}.m4b' \
  -e SERIES_BOOK_PATH_TEMPLATE='{author}/{series}/Band {series_index} - {title}/{title}.m4b' \
  -v /path/to/audiobookDownloader/config:/config \
  -v /path/to/audiobookDownloader/audiobooks:/audiobooks \
  audible-downloader
```

Open `http://localhost:8080`. The web interface has no authentication in V1, so
do not expose it directly to an untrusted network.

The automatic six-hour cycle remains enabled. Configure the runtime with:

```text
WEB_PORT=8080                 Web server port
WEB_HOST=0.0.0.0              Web server bind address
AUTO_PROCESS_ENABLED=True     Enable automatic refresh and conversion
AUTO_PROCESS_INTERVAL=21600   Seconds between automatic cycles (minimum 60)
AUDIBLE_WORK_DIRECTORY=/app/.audible-downloads
                              Temporary AAX/AAXC and voucher staging
LOG_LEVEL=INFO                Python log level
```

The work directory is scratch space for library exports and the download stage
of the processing pipeline. Completed and failed conversion jobs remove their
job-specific directory. It does not need a persistent volume: after a restart,
a job waiting for media processing continues only when its recorded staged
inputs still exist; otherwise it is marked as interrupted/failed. In-place M4B
normalization uses a temporary file beside the M4B for atomic replacement, not
this work directory.

`Bibliothek aktualisieren` only synchronizes Audible metadata; it never starts a
conversion. Manual conversions run in the background and show their current step
and last error on the book page. Web actions are persisted in SQLite before the
request returns and are shown under `Jobs`.

The proposed target can be changed for one operation without changing either
global template. Absolute and relative inputs are accepted, but the normalized
file must remain below `/audiobooks`. A previous history entry requires the
explicit reconvert action; an existing target additionally requires the separate
overwrite confirmation.

## Loudness normalization

Normalization is opt-in. An ordinary conversion still uses FFmpeg stream copy
(`-c copy`) and therefore does not decode or re-encode audio. Select
`Lautstärke normalisieren` for a particular conversion to use EBU R128 two-pass
`loudnorm` followed by AAC encoding.

```text
AUDIO_NORMALIZE_TARGET_I=-18      Integrated loudness in LUFS
AUDIO_NORMALIZE_TARGET_TP=-1.5    True-peak ceiling in dBTP
AUDIO_NORMALIZE_TARGET_LRA=11     Loudness range target
```

For an existing book, `Lautstärke normalisieren` uses the actual `output_files`
rows from the latest successful processing run. It never recalculates the path
from today's templates and does not download from Audible. All saved multipart
files are handled serially.

The first FFmpeg pass measures the source:

```text
ffmpeg [input/decryption options] -i INPUT -map 0:a:0 \
  -af loudnorm=I=-18:TP=-1.5:LRA=11:print_format=json -f null -
```

The second pass maps every stream and copies non-audio streams, global metadata
and chapters while only audio is encoded again:

```text
ffmpeg [input/decryption options] -i INPUT -map 0:a -map 0:v? -map 0:s? -map_metadata 0 \
  -map_chapters 0 -c copy -c:a aac -b:a BITRATE \
  -ar:a SOURCE_RATE -ac:a SOURCE_CHANNELS \
  -af loudnorm=I=-18:TP=-1.5:LRA=11:measured_... OUTPUT.m4b
```

When FFprobe reports a source bitrate, the encoder reuses it within a defensive
32–256 kbit/s range. If it cannot be determined, the fallback is 64 kbit/s per
channel, capped at 192 kbit/s. Source sample rate and channel count are retained.
If FFmpeg reports non-finite measurement values (for example for an extremely
short or silent input), the encode pass uses loudnorm's dynamic mode instead of
passing invalid two-pass parameters.

Existing M4B files are never passed to FFmpeg as its output. FFmpeg writes a
uniquely named temporary M4B in the same directory; the app validates its audio
stream and duration, flushes it, and only then uses an atomic replacement.
Failures leave the original untouched. Interrupted normalization temporaries are
removed on the next startup.

## Persistent processing queue

SQLite stores every web and automatic processing request. One download worker
and one media worker form a small pipeline: one Audible download may overlap one
FFmpeg conversion, but two downloads or two media operations can never run at
the same time. Existing-file normalization skips the download stage.

Jobs move through `queued`, `downloading`, `waiting_for_processing`,
`processing`, and then `completed` or `failed`; waiting jobs can also become
`cancelled`. A second active job for the same book is rejected to protect its
files and avoid double-click duplicates. A failed job does not stop either
worker.

The Jobs, library and active-book views display two separate progress values.
`Gesamt` is the weighted pipeline estimate and is therefore labelled `ca.` while
a job is running. The second bar is exact for the active measurable phase:
downloads use transferred bytes divided by the server-reported content length;
FFmpeg work uses processed media time divided by the probed duration. Loudnorm
passes and multipart FFmpeg operations are included. For a multipart Audible
download, the byte bar describes the current file and starts again when the next
part begins. Preparation, queue waiting, validation and metadata writes have no
invented phase percentage.

The library page does not reload itself while jobs are active, so searching,
scrolling and opening books are not interrupted. The dedicated Jobs and active
book pages continue to refresh while work is running. Completed, failed and
cancelled jobs can be removed from the Jobs view. This sets `dismissed_at` on
the queue row; it does not delete the processing run, output paths or error
history.

On startup, queued jobs remain queued. Downloaded jobs waiting for media keep
their stage only if all staged inputs are still present; otherwise they fail with
an interruption error. Jobs found in an active download or media operation are
marked failed with an interruption error, because blindly replaying those operations
would not be safely idempotent. The user can submit a new job from the book page.

The additive database migration creates `jobs` and adds operation,
normalization, and job linkage fields to `processing_runs`, plus a nullable
normalization status on `output_files`. Queue rows also store the weighted
`progress_percent`, exact `phase_progress_percent`, its `phase_label`, and the
optional `dismissed_at` timestamp. Existing histories and output paths are not
rewritten. A null normalization value intentionally displays as
`Normalisierung unbekannt` for pre-feature files.

### Manually associating existing M4B files

The book page links to `Bestehende M4B-Dateien verknüpfen`. This view scans the
configured audiobook directory on demand and shows whether each M4B already has
a successful database association. One or several files can be selected; manual
relative or absolute paths below the audiobook root are also accepted.

Associating files does not run FFmpeg, move a file, or change its contents. It
adds a successful `link_existing` processing run whose `output_files` become the
book's authoritative current paths. Multipart selections are stored in stable
path order. Newly discovered files start with an unknown normalization state;
relinking a file already associated with the same book preserves the state known
by the app. The normal existing-file normalization action can then operate on
those exact paths. A file
whose latest successful association belongs to another book is rejected to
avoid ambiguous library state.

## Configurable book paths

When `AUDIOBOOK_FOLDERS=True`, the complete relative output path can be configured separately for standalone books and series books.

Defaults:

```text
SINGLE_BOOK_PATH_TEMPLATE={author}/{title}/{title}.m4b
SERIES_BOOK_PATH_TEMPLATE={author}/{series}/Band {series_index} - {title}/{title}.m4b
```

Everything outside `{...}` is normal literal text. `/` creates another directory level. This means text such as `Band `, brackets, dashes and other labels can be placed directly in the template.

For example:

```text
SERIES_BOOK_PATH_TEMPLATE={author}/{series}/Band {series_index} - {title}/{title}.m4b
```

can produce:

```text
/audiobooks/Ada Autor/Meine Serie/Band 3 - Serienbuch/Serienbuch.m4b
```

Available placeholders:

```text
{author}        Author(s)
{title}         Book title
{subtitle}      Subtitle, if supplied by Audible
{series}        Series name
{series_index}         Volume/sequence value supplied by Audible
{series_index_padded}  Same value, but 1-9 become 01-09
{year}          Release year
{asin}          Audible ASIN
{narrator}      Narrator(s)
```

A different example using subtitle and arbitrary literal text:

```text
SINGLE_BOOK_PATH_TEMPLATE={author}/{year} - {title}/{title} - {subtitle}.m4b
```

`{series_index_padded}` adds exactly one leading zero only for single-digit integer indices: `1` becomes `01`, `9` becomes `09`, while `10`, `123` and values such as `1.5` stay unchanged. Use `{series_index}` when no padding is wanted.

If a placeholder has no value, it expands to an empty string. Literal text surrounding it is deliberately preserved; the application does not try to guess whether a dash, bracket or label should also disappear.

If the configured final filename does not end in `.m4b`, `.m4b` is appended automatically.

### Multipart audiobooks

If Audible returns several audio files for the same ASIN, the application
produces exactly one M4B at the calculated or manually edited target path. It
does not publish separate `Part 01`, `Part 02`, ... files.

Each AAX/AAXC part is first decrypted through the existing stream-copy path.
FFmpeg then creates audio-only temporary MP4 fragments and concatenates their
AAC streams with `-c copy`; an ordinary multipart conversion therefore still
does not re-encode audio. The final mux maps the first available attached cover,
merges global tags from all parts, and writes one combined chapter table. Every
chapter start/end is shifted by the summed audio duration of the preceding
parts. If a part contains no chapters, a chapter named `Teil NN` is created for
that part. If Audible unexpectedly supplies incompatible codecs, sample rates or
channel layouts between parts, only then are the temporary part streams aligned
to the first part with AAC before concatenation.

The merge output is written to a unique same-directory temporary file, checked
for a valid audio stream and a duration matching the sum of all parts, and only
then atomically moved to the target. A failed merge leaves an existing target
unchanged. With normalization enabled, all parts are merged first and the
complete audiobook is then passed once through two-pass loudnorm, so loudness is
measured over the full book rather than independently per part.

### Already downloaded detection and history

After every attempt, SQLite stores an append-only processing record. Successful
attempts also store the actual output paths relative to `/audiobooks`. New
multipart downloads store the one merged M4B; manually associated or historical
multipart books may still have several rows. Status checks use those saved paths, so changing a template
does not hide an older output. The legacy migration scan continues to recognize
template outputs and older filenames containing an ASIN.

An existing history entry prevents automatic repeat processing. Use `Erneut
konvertieren` in the web interface to create a new forced attempt. Existing target
files are never overwritten unless the overwrite checkbox is explicitly selected.

`AUDIOBOOK_FOLDER_INCLUDE_NARRATOR` is no longer needed for template-based paths. Add `{narrator}` to a template only if the narrator should be part of the path.

## First run

Start the container, then open a shell in it and run:

```sh
audible quickstart
```

Complete the Audible authentication flow. The auth file must not be encrypted.

## Development

Run the automated suite and syntax check with:

```sh
python3 -m pytest -q
python3 -m compileall -q app tests
```

The service and database decisions are described in `ARCHITECTURE.md`.

## Limitations

The web interface intentionally has no users or authentication and exposes only
the latest relevant book status; recent processing requests remain visible on
the Jobs page. Legacy files can be
imported when they match the configured path at migration time or contain a known
ASIN in their filename; other manually renamed legacy files cannot be assigned to
a book automatically. Queue claims are durable, while resource locks are local
to one application process, so the container must run a single web process.
