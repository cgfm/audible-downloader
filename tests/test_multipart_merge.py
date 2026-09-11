import json
import shutil
import subprocess
from pathlib import Path

import pytest


def multipart_probe(duration, chapters, tags=None, cover=False):
    streams = [
        {
            "codec_type": "audio",
            "codec_name": "aac",
            "sample_rate": "44100",
            "channels": 1,
            "duration": str(duration),
        }
    ]
    if cover:
        streams.append(
            {"codec_type": "video", "disposition": {"attached_pic": 1}}
        )
    return {
        "stream": streams[0],
        "streams": streams,
        "chapters": chapters,
        "format": {"duration": str(duration), "tags": tags or {}},
    }


def test_multipart_metadata_offsets_chapters_and_unifies_tags(service):
    probes = [
        multipart_probe(
            10,
            [
                {"start_time": "0", "end_time": "4", "tags": {"title": "A"}},
                {"start_time": "4", "end_time": "10", "tags": {"title": "B"}},
            ],
            {"title": "Part 1", "artist": "Original Author"},
        ),
        multipart_probe(
            20,
            [{"start_time": "0", "end_time": "20", "tags": {"title": "C"}}],
            {"comment": "From second part"},
        ),
    ]

    tags, chapters, duration = service._multipart_metadata(
        probes, service.get_book("B012345678")
    )

    assert duration == 30
    assert [(chapter["start"], chapter["end"]) for chapter in chapters] == [
        (0, 4),
        (4, 10),
        (10, 30),
    ]
    assert tags["title"] == "Der Titel"
    assert tags["artist"] == "Original Author"
    assert tags["comment"] == "From second part"
    assert tags["narrator"] == "Nora Erzählerin"


def test_merge_uses_stream_copy_combined_chapters_and_available_cover(
    monkeypatch, service
):
    work = service.config.work_root / "merge"
    work.mkdir()
    parts = [work / "part-0001.m4b", work / "part-0002.m4b"]
    for part in parts:
        part.write_bytes(b"part")
    first_probe = multipart_probe(
        10,
        [{"start_time": "0", "end_time": "10", "tags": {"title": "First"}}],
        {"artist": "Author"},
    )
    second_probe = multipart_probe(
        20,
        [{"start_time": "0", "end_time": "20", "tags": {"title": "Second"}}],
        {"comment": "Second metadata"},
        cover=True,
    )
    captured = {}

    def probe(path, input_options=None):
        if path == parts[0]:
            return first_probe
        if path == parts[1]:
            return second_probe
        return multipart_probe(30, [], {"title": "Der Titel"}, cover=True)

    def run(command, duration, progress_callback):
        if "ffmetadata" in command:
            captured["command"] = command
            metadata_path = Path(command[command.index("ffmetadata") + 2])
            captured["metadata"] = metadata_path.read_text(encoding="utf-8")
        Path(command[-1]).write_bytes(b"merged")
        if progress_callback:
            progress_callback(1.0)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(service, "_probe_audio", probe)
    monkeypatch.setattr(service, "_run_ffmpeg_with_progress", run)
    monkeypatch.setattr(service, "_validate_media_output", lambda path: None)
    destination = service.config.audiobook_root / "merged.m4b"

    service._merge_m4b_parts(
        parts, destination, service.get_book("B012345678")
    )

    command = captured["command"]
    assert destination.read_bytes() == b"merged"
    assert command[command.index("-c") + 1] == "copy"
    assert "loudnorm" not in " ".join(command)
    input_positions = [index for index, value in enumerate(command) if value == "-i"]
    assert command[input_positions[1] + 1] == str(parts[1])
    assert "title=First" in captured["metadata"]
    assert "START=10000" in captured["metadata"]
    assert "title=Second" in captured["metadata"]


def test_merge_failure_leaves_existing_destination_unchanged(monkeypatch, service):
    work = service.config.work_root / "failed-merge"
    work.mkdir()
    parts = [work / "part-0001.m4b", work / "part-0002.m4b"]
    for part in parts:
        part.write_bytes(b"part")
    probe = multipart_probe(10, [])
    monkeypatch.setattr(service, "_probe_audio", lambda *args: probe)

    def fail(*args):
        raise subprocess.CalledProcessError(1, "ffmpeg")

    monkeypatch.setattr(service, "_run_ffmpeg_with_progress", fail)
    destination = service.config.audiobook_root / "existing.m4b"
    destination.write_bytes(b"original")

    with pytest.raises(subprocess.CalledProcessError):
        service._merge_m4b_parts(
            parts, destination, service.get_book("B012345678")
        )

    assert destination.read_bytes() == b"original"
    assert list(destination.parent.glob(".*.audible-merge-*.tmp.m4b")) == []


def test_incompatible_part_layouts_are_aligned_with_aac(monkeypatch, service):
    work = service.config.work_root / "different-layouts"
    work.mkdir()
    parts = [work / "part-0001.m4b", work / "part-0002.m4b"]
    for part in parts:
        part.write_bytes(b"part")
    probes = [multipart_probe(10, []), multipart_probe(10, [])]
    probes[1]["stream"]["sample_rate"] = "48000"
    commands = []

    def probe(path, input_options=None):
        if path == parts[0]:
            return probes[0]
        if path == parts[1]:
            return probes[1]
        return multipart_probe(20, [])

    def run(command, duration, progress_callback):
        commands.append(command)
        Path(command[-1]).write_bytes(b"output")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(service, "_probe_audio", probe)
    monkeypatch.setattr(service, "_run_ffmpeg_with_progress", run)
    monkeypatch.setattr(service, "_validate_media_output", lambda path: None)

    service._merge_m4b_parts(
        parts,
        service.config.audiobook_root / "aligned.m4b",
        service.get_book("B012345678"),
    )

    for remux_command in commands[:2]:
        assert remux_command[remux_command.index("-c:a") + 1] == "aac"
    assert commands[-1][commands[-1].index("-c") + 1] == "copy"


def test_startup_cleanup_removes_only_named_multipart_staging(service):
    interrupted = service.config.work_root / "B012345678" / (
        ".audible-multipart-interrupted"
    )
    unrelated = service.config.work_root / "B012345678" / "keep"
    interrupted.mkdir(parents=True)
    unrelated.mkdir()
    (interrupted / "part-0001.m4b").write_bytes(b"partial")

    assert service.cleanup_interrupted_work_directories() == 1
    assert not interrupted.exists()
    assert unrelated.exists()


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg smoke test requires ffmpeg and ffprobe",
)
def test_real_multipart_merge_preserves_cover_metadata_and_all_chapters(
    service, tmp_path
):
    cover = tmp_path / "cover.jpg"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=16x16",
            "-frames:v",
            "1",
            str(cover),
        ],
        check=True,
        capture_output=True,
    )
    parts = []
    for number, frequency in ((1, 440), (2, 660)):
        metadata = tmp_path / f"part-{number}.ffmeta"
        extra_tag = "artist=Smoke Author" if number == 1 else "comment=Part Two"
        metadata.write_text(
            ";FFMETADATA1\n"
            f"title=Original Part {number}\n{extra_tag}\n"
            "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=500\n"
            f"title=Part {number} Chapter 1\n"
            "[CHAPTER]\nTIMEBASE=1/1000\nSTART=500\nEND=1000\n"
            f"title=Part {number} Chapter 2\n",
            encoding="utf-8",
        )
        part = tmp_path / f"part-{number:04d}.m4b"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:duration=1",
                "-i",
                str(cover),
                "-f",
                "ffmetadata",
                "-i",
                str(metadata),
                "-map",
                "0:a",
                "-map",
                "1:v",
                "-map_metadata",
                "2",
                "-map_chapters",
                "2",
                "-c:a",
                "aac",
                "-b:a",
                "64000",
                "-c:v",
                "copy",
                "-disposition:v",
                "attached_pic",
                str(part),
            ],
            check=True,
            capture_output=True,
        )
        parts.append(part)

    destination = service.config.audiobook_root / "complete.m4b"
    service._merge_m4b_parts(
        parts, destination, service.get_book("B012345678")
    )
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_chapters",
            "-show_format",
            "-of",
            "json",
            str(destination),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    probe = json.loads(result.stdout)

    assert probe["format"]["tags"]["title"] == "Der Titel"
    assert probe["format"]["tags"]["artist"] == "Smoke Author"
    assert probe["format"]["tags"]["comment"] == "Part Two"
    assert len(probe["chapters"]) == 4
    assert float(probe["chapters"][2]["start_time"]) >= 0.99
    assert any(
        stream.get("disposition", {}).get("attached_pic")
        for stream in probe["streams"]
    )
