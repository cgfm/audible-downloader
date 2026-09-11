"""Compatibility CLI for one automatic Audible synchronization cycle."""

from audible_service import (
    AudibleService,
    multipart_destination,
    safe_path_component,
)

service = AudibleService()


def update_titles():
    return service.refresh_library()


def reconcile_downloaded_state():
    return service.reconcile_existing_outputs()


def download_new_titles():
    return service.process_pending()


def process_book(book, target_path=None, force=False, overwrite=False):
    return service.process_book(book, target_path, force, overwrite)


def _render_book_path(asin):
    return str(service.render_book_path(asin))


def _multipart_destination(base_destination, part_number):
    return str(multipart_destination(base_destination, part_number))


def _safe_path_component(value):
    return safe_path_component(value)


def main():
    update_titles()
    reconcile_downloaded_state()
    download_new_titles()


if __name__ == "__main__":
    main()
