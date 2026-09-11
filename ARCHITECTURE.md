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
  percentage, timestamps and errors. A partial unique index allows only one
  active job per ASIN. Dismissing a terminal job only sets `dismissed_at`, so
  the database audit trail and associated processing run remain intact.

`processing_runs` remains the processing history and is linked to a queue job;
it additionally records operation type and whether normalization was requested.
`output_files.normalization_status` is nullable so migrated files are not
incorrectly described as unnormalized.

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

The library page deliberately has no timed full-page refresh. The Jobs and
active book pages can refresh without disturbing library search, sort or scroll
state. Queue rows keep the weighted overall pipeline estimate separate from an
exact active-phase percentage. A small wrapper replaces audible-cli's
terminal-only progress bar and reports transferred bytes against its HTTP
content length. For multipart titles this represents the current downloaded
file. FFmpeg's `-progress pipe:1` timestamp is measured against the probed input
duration and weighted by media duration across loudnorm passes and multipart
operations.

Manual targets may be absolute or relative, but are normalized below the
audiobook root to prevent the web process from writing elsewhere. Existing
single-file or multipart destinations require the explicit `overwrite` flag.

The Flask process also owns a lightweight daemon scheduler. It performs the same
refresh-and-process cycle as the former shell loop every six hours by default.
The scheduler now enqueues work rather than running media inline.

Two daemon workers claim SQLite rows transactionally. The download worker moves
conversion jobs from `queued` to `waiting_for_processing`; the media worker
handles both conversion and existing-file normalization. Process-wide download
and media locks are also used by the compatibility `process_book()` call, which
keeps the resource limits intact. This permits one download and one media task
to overlap without permitting two of either class.

Loudnorm is implemented as a two-pass extension of `_convert_file()`. The normal
branch remains its original `-c copy` command. The normalized branch probes the
source layout/bitrate, measures EBU R128 values, maps all input streams, copies
non-audio streams/metadata/chapters, and encodes only audio as AAC. In-place
post-processing writes and validates a same-directory temporary file before
`os.replace()`; startup removes only the app's distinct temporary-name pattern.
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
