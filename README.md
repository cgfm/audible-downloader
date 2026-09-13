# Audible Downloader

A Docker container that automatically downloads Audible audiobooks, converts AAX/AAXC to M4B and stores them below `/audiobooks`.

The program checks periodically for new books in the Audible library. Technical multipart books returned as several AAX/AAXC files for one ASIN are kept together as one audiobook.

It also provides a small server-rendered web interface for searching the library,
refreshing Audible metadata, inspecting processing errors and manually converting
or reconverting one title at a time.

## Build

Successful CI runs on `main` publish a multi-platform image for AMD64 and ARM64
with build-provenance and SBOM metadata:

```sh
docker pull ghcr.io/cgfm/audible-downloader:latest
```

Tags named `vX.Y.Z` additionally publish `vX.Y.Z`, `X.Y.Z`, and `X.Y`; every
published revision also receives an immutable `sha-...` tag. GitHub Container
Registry packages are private by default until their visibility is changed in
the package settings, so a private package requires `docker login ghcr.io`.

To build the image locally instead:

```sh
docker build -t audible-downloader .
```

Example run:

```sh
docker run -d \
  --name=audiobookDownloader \
  -p 8080:8080 \
  -e WEB_HOST='0.0.0.0' \
  -e WEB_AUTH_MODE='form' \
  -e WEB_ADMIN_PASSWORD='use-a-long-random-password' \
  -e WEB_SECRET_KEY='use-an-independent-random-secret' \
  -e WEB_SECURE_COOKIES='False' \
  -e AUDIOBOOK_FOLDERS='True' \
  -e SINGLE_BOOK_PATH_TEMPLATE='{author}/{title}/{asin} - {title}.m4b' \
  -e SERIES_BOOK_PATH_TEMPLATE='{author}/{series}/Band {series_index} - {title}/{asin} - {title}.m4b' \
  -v /path/to/audiobookDownloader/config:/config \
  -v /path/to/audiobookDownloader/audiobooks:/audiobooks \
  -v /path/to/audiobookDownloader/work:/work \
  audible-downloader
```

Open `http://localhost:8080`. On first access the web interface guides you
through the Audible authentication. `WEB_AUTH_MODE=form` protects the complete
web interface with a normal login page whose username and password fields work
with browser password managers. The username defaults to `admin` and can be
changed with `WEB_ADMIN_USERNAME`. `WEB_AUTH_MODE=basic` retains HTTP Basic
authentication for existing installations and API-style clients. The service
binds only to `127.0.0.1` by default. Binding to a non-local address fails closed
unless authentication is configured. The example explicitly uses `0.0.0.0` so
Docker port publishing works, and disables secure cookies only because it uses
local plain HTTP. Use HTTPS and keep the default secure cookies for network
access, because credentials are otherwise not encrypted in transit.

`WEB_ADMIN_PASSWORD_FILE` loads the password from a Docker-secret-style file
and takes precedence over the environment value. A trusted authenticating
reverse proxy can be used with `WEB_AUTH_MODE=proxy` and an explicit
`WEB_PROXY_AUTH_HEADER`; the application then requires a non-empty value in
that header. The application must be reachable only through that proxy, which
must remove client-supplied copies of the header. `WEB_AUTH_MODE=disabled` is
accepted only for a local bind address.

TLS termination or forwarding through a reverse proxy alone is not
authentication. Keep application form or Basic authentication enabled unless
the proxy itself authenticates users and injects the configured trusted header.
A container that must be reached by a proxy in another container normally still needs
`WEB_HOST=0.0.0.0`; Docker network isolation determines whether that port is
published externally.

The automatic six-hour cycle remains enabled. Configure the runtime with:

```text
WEB_PORT=8080                 Web server port
WEB_HOST=127.0.0.1            Safe default bind address; use 0.0.0.0 explicitly for Docker publishing
WEB_AUTH_MODE=basic           form (login page), basic, proxy, or disabled (local bind only)
WEB_ADMIN_USERNAME=admin      Username for form or HTTP Basic authentication
WEB_ADMIN_PASSWORD=           Required for form mode and non-local basic mode
WEB_ADMIN_PASSWORD_FILE=      Preferred password file / Docker Secret
WEB_PROXY_AUTH_HEADER=        Trusted identity header required in proxy mode
WEB_SECRET_KEY=               Persistent random secret for browser sessions
WEB_SESSION_HOURS=12          Lifetime of a form-login session (minimum 1 hour)
WEB_SECURE_COOKIES=True       Set False only for deliberately local plain HTTP
WEB_MAX_REQUEST_BYTES=1048576 Maximum HTTP request size
WEB_HSTS_SECONDS=0            HSTS max-age when Flask sees an HTTPS request
WEB_THREADS=4                 Waitress request threads (minimum 2)
AUTO_DOWNLOAD_FROM_DATE=      Inclusive purchase-date boundary for automatic downloads
AUDIOBOOK_FOLDERS=True        Use the configured folder/path templates
SINGLE_BOOK_PATH_TEMPLATE={author}/{title}/{asin} - {title}.m4b
SERIES_BOOK_PATH_TEMPLATE={author}/{series}/Band {series_index} - {title}/{asin} - {title}.m4b
AUTO_PROCESS_ENABLED=True     Enable automatic refresh and conversion
AUTO_PROCESS_INTERVAL=21600   Seconds between automatic cycles (minimum 60)
AUDIBLE_WORK_DIRECTORY=/work
                              Temporary AAX/AAXC and voucher staging
AUDIBLE_NETWORK_TIMEOUT=1800  Audible operation timeout in seconds
MEDIA_PROCESS_TIMEOUT=21600   FFmpeg timeout in seconds
MEDIA_PROBE_TIMEOUT=120       ffprobe/activation lookup timeout in seconds
PROCESS_TERMINATE_GRACE=10    Grace before an aborted child process is killed
MINIMUM_FREE_BYTES=536870912  Required free bytes on work and output filesystems
MAX_INPUT_BYTES=0             Maximum downloaded bytes per job; 0 means unlimited
MAX_QUEUE_LENGTH=1000         Maximum number of active queue entries
JOB_RETRY_MAX_ATTEMPTS=3      Total attempts for classified temporary failures
JOB_RETRY_BASE_SECONDS=15     Exponential retry backoff base
DATABASE_BACKUP_RETENTION=7   Number of startup/manual SQLite backups retained
JOB_HISTORY_RETENTION_DAYS=0  Prune old dismissed jobs after N days; 0 disables
LOG_LEVEL=INFO                Python log level
LOG_FORMAT=json               Structured JSON; use text for traditional logs
```

On the first database initialization, the automatic download boundary is saved
as the current date. Automatic processing includes purchases made on that date
or later and excludes older titles as well as entries without a valid purchase
date. Manual conversion remains possible for every library title. The boundary,
folder mode, both path templates, automatic processing, its interval, and the
log level can be changed on the Settings page.

An explicitly present environment variable always takes precedence over the
corresponding database value. Its input is disabled on the Settings page and is
labelled with the owning variable. Removing the variable restores the last
database value after a container restart. Use `AUTO_DOWNLOAD_FROM_DATE` in
`YYYY-MM-DD` format to override the purchase boundary.

The work directory is scratch space for library exports and the download stage
of the processing pipeline. Completed and failed conversion jobs remove their
job-specific directory. Mount `/work` when waiting downloads should survive a
container replacement. After a restart, a job waiting for media processing
continues only when its recorded staged inputs still exist; otherwise it is
marked as interrupted/failed. In-place M4B
normalization uses a temporary file beside the M4B for atomic replacement, not
this work directory.

Audible downloads may contain AAX, AAXC, or the MP3 fallback emitted by
audible-cli 0.6.0. MP3 input skips activation-key lookup and is transcoded to
AAC/M4B; a matching leftover voucher is removed only after successful atomic
publication.

The image runs as UID/GID 1000, while application code and the virtual
environment are root-owned and read-only. For a read-only root filesystem, add
`--read-only --tmpfs /tmp:rw,noexec,nosuid,size=256m` and keep `/config`,
`/audiobooks`, and `/work` mounted writable.

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

Existing M4B files are never passed to FFmpeg as its output. This applies to
ordinary conversion, multipart merge, and normalization. FFmpeg writes a
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
`processing`, and then `completed` or `failed`; any active job can also become
`cancelled`. Active Audible/FFmpeg processes run in their own process groups;
an abort first terminates and then kills them after the configured grace period.
Classified network/time-limit failures are retried with exponential backoff,
and failed/cancelled jobs offer a manual retry that preserves their options.
A second active job for the same book is rejected to protect its
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

The additive database migration creates `jobs`, `target_path_claims`, and
`runtime_state`, and adds operation,
normalization, and job linkage fields to `processing_runs`, plus a nullable
normalization status on `output_files`. Queue rows also store the weighted
`progress_percent`, exact `phase_progress_percent`, its `phase_label`, and the
optional `dismissed_at` timestamp, retry counters, next-attempt time, and a
cancel flag. Existing histories and output paths are not
rewritten. A null normalization value intentionally displays as
`Normalisierung unbekannt` for pre-feature files.

Subprocess stdout/stderr is captured and converted to centrally redacted public
errors. Activation bytes, AAXC key/IV, authorization codes, bearer tokens, and
known auth-file secrets are removed before logging, SQLite persistence, or HTML
rendering. Startup also scrubs recognizable secrets from legacy error rows.

`target_path_claims` gives every relative output path one global ASIN owner.
Ownership is checked when a job is queued and again immediately before atomic
publication; `overwrite` never overrides another book's ownership. Automatic
template collisions receive an ASIN suffix. Existing ambiguous legacy history
is not silently reassigned.

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
SINGLE_BOOK_PATH_TEMPLATE={author}/{title}/{asin} - {title}.m4b
SERIES_BOOK_PATH_TEMPLATE={author}/{series}/Band {series_index} - {title}/{asin} - {title}.m4b
```

Everything outside `{...}` is normal literal text. `/` creates another directory level. This means text such as `Band `, brackets, dashes and other labels can be placed directly in the template.

For example:

```text
SERIES_BOOK_PATH_TEMPLATE={author}/{series}/Band {series_index} - {title}/{asin} - {title}.m4b
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

An existing history entry prevents automatic repeat processing. Use `Erneut konvertieren` in the web interface to create a new forced attempt. Existing target
files are never overwritten unless the overwrite checkbox is explicitly selected.

`AUDIOBOOK_FOLDER_INCLUDE_NARRATOR` is no longer needed for template-based paths. Add `{narrator}` to a template only if the narrator should be part of the path.

## First run

Start the container and open `http://localhost:8080`. If no valid Audible
profile exists below `/config`, all application pages redirect to the integrated
setup:

1. Select the Audible marketplace.
2. Open the generated Amazon login link in a new browser tab and complete the Amazon login, including any CAPTCHA or two-factor challenge.
3. Amazon eventually displays an expected error page. Copy the complete URL from that page's address bar back into the setup form.
4. The application registers the device in a temporary location and verifies it with a real library export. Only a successfully verified, unencrypted auth file replaces the previous authentication.

The library is synchronized immediately after a successful setup. If automatic
processing is enabled, only missing titles whose purchase date is on or after
the persisted automatic download boundary are added to the queue. On first
initialization this boundary is the current date, so an existing library is not
downloaded as a backlog. Use
`Einstellungen` to test the connection or start the same flow again for
reauthentication. The old auth file remains active if the new login is cancelled
or fails validation. Each successful rotation publishes a uniquely named auth
file before atomically switching `config.toml`; startup removes abandoned private
staging directories and unreferenced managed auth files. Auth/config/database
files use mode `0600`, and private directories use `0700`.

Each complete library refresh records when a title was last seen. Titles absent
from the next export are marked removed/archived and hidden by the default
library filter; they can be displayed separately and are reactivated if they
return. Local media is never deleted by this lifecycle change. The library page
uses server-side search, batched status queries, and pages of 100 rows.

The Settings page controls the persisted application values listed above and
can additionally pause automatic queueing without disabling metadata refreshes;
manual jobs remain available. It also shows the last automatic cycle and can
create an immediate database backup. `/healthz` reports process
liveness, `/readyz` checks SQLite, and authenticated `/metrics` returns basic
book/job/automatic-cycle counts.

### Database backups and restore

Before every startup migration, an existing SQLite database is backed up
consistently into `/config/backups`; the Settings action creates another backup.
Only the newest `DATABASE_BACKUP_RETENTION` files are retained, all with mode
`0600`. To restore, stop the container, preserve the current database and any
`audiobooks.db-wal`/`audiobooks.db-shm` sidecars outside `/config`, copy the
chosen backup to `/config/audiobooks.db`, then restart. Never restore while the
service is running.

The command-line quickstart remains available as a fallback:

```sh
docker exec -it audiobookDownloader audible quickstart
```

The auth file must not be encrypted. Authentication files, browser callback
URLs and their authorization codes are sensitive and must never be committed or
copied into logs.

## Development

Run the automated suite and syntax check with:

```sh
python3 -m pytest -q
python3 -m compileall -q app tests
ruff check app tests
bandit -q -r app -ll
pip-audit -r requirements.lock
```

Production and development dependencies are fully pinned with hashes in
`requirements.lock` and `requirements-dev.lock`. CI runs syntax, lint, tests,
dependency audit, a cache-independent image build, and a High/Critical Trivy
gate; Dependabot proposes weekly lock/base-image/action updates.

The service and database decisions are described in `ARCHITECTURE.md`.

## Limitations

The web interface has one shared form or HTTP Basic login (or a trusted-proxy
identity header) rather than a user and role system. Recent processing requests
remain visible on the Jobs page.
Legacy files can be
imported when they match the configured path at migration time or contain a known
ASIN in their filename; other manually renamed legacy files cannot be assigned to
a book automatically. Queue claims are durable, while resource locks are local
to one application process, so the container must run a single web process.
