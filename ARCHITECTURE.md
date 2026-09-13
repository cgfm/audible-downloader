# Architecture

`AudibleService` in `app/audible_service.py` is the sole application service.
The legacy-compatible direct call and the persistent workers share the same
download, multipart merge, AAXC voucher, FFmpeg and output-recording helpers;
there is no parallel conversion implementation.

The existing `audiobooks` table remains compatible and keeps `downloaded` only as
a legacy history marker. New processing state is append-only:

- `processing_runs` stores each attempt, requested target, override/force flags,
  timestamps, current step and the last error.
- `output_files` stores every produced M4B path relative to the configured
  audiobook root, including multipart numbers.
- `jobs` persists manual and automatic requests, options, target, stage,
  percentage, timestamps, cancel state, retry/backoff state and redacted errors.
  A partial unique index allows only one
  active job per ASIN. Dismissing a terminal job only sets `dismissed_at`, so
  the database audit trail and associated processing run remain intact.
- `target_path_claims` assigns each relative output path to exactly one ASIN
  across runs and jobs. Transactional reservations and a publication-time check
  prevent `overwrite` from reassigning another book's path.
- `runtime_state` persists editable application settings, the
  automatic-processing pause state and the last scheduler result. Explicit
  environment variables override, but do not overwrite, their database values.

`processing_runs` remains the processing history and is linked to a queue job;
it additionally records operation type and whether normalization was requested.
`output_files.normalization_status` is nullable so migrated files are not
incorrectly described as unnormalized.

`audiobooks.last_seen_at`, `library_state`, and `removed_at` implement a soft
library lifecycle. A complete export marks missing products as removed without
deleting local output; a later appearance reactivates them.

The UI determines file presence from the output rows belonging to the latest
successful run and checks those exact paths on disk. It does not reconstruct old
locations from today's templates. On migration, the legacy reconciliation scan
creates one successful historical run for files it can identify.

The on-demand manual association view scans M4B files without changing them.
Its selection creates a successful `link_existing` run and nullable-normalization
`output_files`, so manually named and multipart files immediately participate in
the same presence checks and normalization pipeline. The latest successful
association prevents the same path from being assigned ambiguously to another
ASIN.

The library page uses SQL search, pages of at most 100 rows, and a fixed set of
batch queries for run/job/output state rather than per-book SQLite queries. It
deliberately has no timed full-page refresh. The Jobs and
active book pages can refresh without disturbing library search, sort or scroll
state. Queue rows keep the weighted overall pipeline estimate separate from an
exact active-phase percentage. A small wrapper replaces audible-cli's
terminal-only progress bar and reports transferred bytes against its HTTP
content length. For multipart titles this represents the current downloaded
file. FFmpeg's `-progress pipe:1` timestamp is measured against the probed input
duration and weighted by media duration across loudnorm passes and multipart
operations.

Manual targets may be absolute or relative, but are normalized below the
audiobook root to prevent the web process from writing elsewhere. Path
components and total UTF-8 path length are bounded. Existing single-file or
multipart destinations require the explicit `overwrite` flag. Automatic path
collisions use an ASIN suffix; foreign ownership is never inferred from
`overwrite`.

The production container runs the Flask application through Waitress. That
single process also owns a lightweight daemon scheduler and two workers, with a
SIGTERM/SIGINT shutdown path that terminates active child process groups. The
scheduler performs the same refresh-and-process cycle as the former shell loop
every six hours by default. It enqueues work rather than running media inline,
records its last result, and observes the persisted automatic-processing pause.

Audible enrollment is a two-step external-browser flow owned by
`AudibleService`. A daemon authentication thread obtains the Amazon login URL
from `Authenticator.from_login_external()` and waits for the user to return the
final `/ap/maplanding` URL through the web form. Both login and callback URLs
are accepted only over HTTPS from the selected marketplace host and normal TLS
ports; the callback must contain an authorization code. It remains in memory
just long enough to wake the waiting callback and is never logged or persisted.

New credentials and a matching `config.toml` are written into a private staging
directory below `/config`. An isolated `audible library export` validates the
staged profile before either file is installed. A unique final auth file is
fsynced and installed first; the atomically replaced config then switches its
pointer. Startup removes interrupted staging trees and unreferenced managed auth
files. This means a cancelled or failed reauthentication leaves the active
profile untouched. After installation the
already validated export synchronizes SQLite, and optional automatic processing
enqueues missing titles. Before initial enrollment the scheduler skips its cycle
instead of producing an expected authentication error.

The first schema initialization stores the current UTC date as the inclusive
automatic purchase boundary. `process_pending()` only queues active books with
a valid purchase date on or after that value. This prevents an existing Audible
library from becoming an accidental first-run backlog while leaving manual jobs
available. A later boundary update also cancels still-queued automatic jobs that
are no longer eligible; manual and already-processing jobs are untouched.

Production POST forms carry a session CSRF token. The server binds locally by
default and a non-local form/Basic-auth bind without a password is rejected. A
password file and explicit trusted-proxy mode are supported. Form authentication
uses an HttpOnly, SameSite session containing only an HMAC-derived login proof;
password changes invalidate existing sessions. Form and Basic credentials are
compared as SHA-256 digests of UTF-8 input so non-ASCII input yields a normal
authentication failure. Per-client request limits protect authentication and
expensive endpoints; the application also caps request bodies. Dynamic responses
disable caching and set restrictive CSP, frame, referrer and content-type
headers. Cover URLs are limited to HTTPS Amazon media hosts.

Two daemon workers claim SQLite rows transactionally. The download worker moves
conversion jobs from `queued` to `waiting_for_processing`; the media worker
handles both conversion and existing-file normalization. Process-wide download
and media locks are also used by the compatibility `process_book()` call, which
keeps the resource limits intact. This permits one download and one media task
to overlap without permitting two of either class.

Audible, FFmpeg and ffprobe invocations capture output, use configurable hard
timeouts, and never expose their full credential-bearing command in public
errors. Active job subprocesses start new process groups. Cancellation and
shutdown send TERM, wait a bounded grace period, and then use KILL. Temporary
network/time-limit failures return to their durable stage with exponential
backoff and a bounded attempt count; permanent failures require explicit manual
retry.

Loudnorm is implemented as a two-pass extension of `_convert_file()`. The normal
branch remains its original `-c copy` command. The normalized branch probes the
source layout/bitrate, measures EBU R128 values, maps all input streams, copies
non-audio streams/metadata/chapters, and encodes only audio as AAC. Every final
media path, including ordinary conversion, writes and validates a same-directory
temporary file before `fsync` and `os.replace()`; startup removes only the app's
distinct temporary-name pattern.
Non-finite first-pass values fall back to loudnorm's dynamic mode. Chapter data
tracks are not copied as MP4 streams; chapters are preserved through
`-map_chapters`, while audio, cover/video and optional subtitle streams are
mapped explicitly.

Multipart Audible downloads are decrypted one after another into private M4B
fragments. Audio-only remuxes keep attached pictures and chapter data out of the
concat demuxer's timeline, after which AAC is concatenated with stream copy.
Global tags are merged with first-value precedence and authoritative library
title/author fallbacks. Chapter timestamps from every part are offset onto one
continuous timeline; the first available attached cover is mapped separately.
The merged file is duration-validated and atomically published. Optional
loudnorm runs only after this merge and therefore measures the complete book.

The runtime image is based on a digest-pinned supported Debian Trixie/Python
image. Debian slim avoids the Alpine apk 3 package-script incompatibility seen
on NAS hosts with older Docker/seccomp stacks.
Production Python dependencies are hash-locked, pip/build tooling is absent from
the final virtual environment, application code is root-owned read-only, and the
service runs as UID/GID 1000 with separate `/config`, `/audiobooks`, and `/work`
writable roots. Existing SQLite is backed up with SQLite's online backup API
before startup migration. Sensitive files/directories are normalized to
`0600`/`0700`; optional retention prunes only old dismissed jobs after detaching
their path-claim references. Runtime logs are JSON objects by default, and the
authenticated metrics endpoint exposes aggregate book/job/scheduler state.
