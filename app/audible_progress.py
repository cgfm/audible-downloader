"""Run audible-cli and emit machine-readable byte progress events."""

from __future__ import annotations

from audible_cli import downloader

PROGRESS_PREFIX = "AUDIBLE_DOWNLOAD_PROGRESS="


class MachineProgressBar:
    """Minimal audible-cli progress bar that reports changed integer percents."""

    def __init__(self, total: int, start: int = 0) -> None:
        self.total = max(0, int(total))
        self.current = max(0, int(start))
        self.last_percent: int | None = None
        self._report()

    def __enter__(self) -> MachineProgressBar:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def update(self, amount: int = 1) -> None:
        self.current += amount
        self._report()

    def close(self) -> None:
        self._report()

    def _report(self) -> None:
        if self.total <= 0:
            return
        percent = max(0, min(100, self.current * 100 // self.total))
        if percent == self.last_percent:
            return
        self.last_percent = percent
        print(
            f"{PROGRESS_PREFIX}{self.current}/{self.total}",
            flush=True,
        )


def machine_progressbar(destination, total: int | None, start: int = 0):
    """Replace audible-cli's terminal-only bar with a line protocol."""
    del destination
    if total is None:
        return downloader.DummyProgressBar()
    return MachineProgressBar(total, start)


def main() -> None:
    downloader.get_progressbar = machine_progressbar
    from audible_cli import main as audible_main

    audible_main()


if __name__ == "__main__":
    main()
