"""Small server-rendered web UI and automatic scheduler."""

import logging
import os
import threading
from datetime import datetime

from audible_service import (
    ACTIVE_JOB_STATUSES,
    SORT_FIELDS,
    STATUS_LABELS,
    AudibleService,
)
from flask import Flask, abort, flash, redirect, render_template, request, url_for

LOGGER = logging.getLogger(__name__)


def create_app(service: AudibleService | None = None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("WEB_SECRET_KEY", "audible-downloader-local")
    app.config["AUDIBLE_SERVICE"] = service or AudibleService()

    def current_service() -> AudibleService:
        return app.config["AUDIBLE_SERVICE"]

    @app.template_filter("purchase_date")
    def format_purchase_date(value: str | None) -> str:
        if not value:
            return "–"
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.strftime("%d.%m.%Y")
        except ValueError:
            return value

    @app.get("/")
    def library():
        query = request.args.get("q", "").strip()
        status_filter = request.args.get("status", "").strip()
        requested_sort = request.args.get("sort", "purchase_date").strip()
        sort_key = requested_sort if requested_sort in SORT_FIELDS else "purchase_date"
        requested_direction = request.args.get("direction", "").strip()
        sort_direction = (
            requested_direction
            if requested_direction in ("asc", "desc")
            else ("desc" if sort_key == "purchase_date" else "asc")
        )
        books = current_service().list_books(
            query, status_filter, sort_key, sort_direction
        )
        sort_headers = {}
        for key in SORT_FIELDS:
            active = sort_key == key
            if active:
                next_direction = "asc" if sort_direction == "desc" else "desc"
            else:
                next_direction = "desc" if key == "purchase_date" else "asc"
            sort_headers[key] = {
                "url": url_for(
                    "library",
                    q=query or None,
                    status=status_filter or None,
                    sort=key,
                    direction=next_direction,
                ),
                "active": active,
                "indicator": "↓" if active and sort_direction == "desc" else "↑" if active else "",
                "aria_sort": (
                    "descending"
                    if active and sort_direction == "desc"
                    else "ascending"
                    if active
                    else "none"
                ),
            }
        return render_template(
            "library.html",
            books=books,
            query=query,
            status_filter=status_filter,
            status_labels=STATUS_LABELS,
            sort_key=sort_key,
            sort_direction=sort_direction,
            sort_headers=sort_headers,
        )

    @app.post("/library/refresh")
    def refresh_library():
        try:
            result = current_service().refresh_library()
            flash(
                f"Bibliothek aktualisiert: {result['added']} neu, "
                f"{result['updated']} aktualisiert.",
                "success",
            )
        except Exception as exc:
            LOGGER.exception("Bibliothek konnte nicht aktualisiert werden")
            flash(f"Bibliothek konnte nicht aktualisiert werden: {exc}", "error")
        return redirect(url_for("library"))

    @app.route("/books/<asin>/convert", methods=["GET", "POST"])
    def convert_book(asin: str):
        try:
            book = current_service().get_book(asin)
        except KeyError:
            abort(404)

        status = current_service().book_status(asin)
        default_target = current_service().default_target_path(book)
        target_value = request.form.get("target_path", str(default_target))
        form_error = None
        existing_targets = []

        try:
            normalized_target = current_service().normalize_target_path(target_value)
            target_value = str(normalized_target)
            existing_targets = current_service().existing_target_files(normalized_target)
        except ValueError as exc:
            normalized_target = None
            form_error = str(exc)

        if request.method == "POST" and form_error is None:
            force = request.form.get("force") == "1"
            overwrite = request.form.get("overwrite") == "1"
            normalize = request.form.get("normalize") == "1"
            if existing_targets and not overwrite:
                form_error = "Die Zieldatei existiert bereits. Überschreiben muss bestätigt werden."
            elif status["key"] == "running":
                form_error = "Dieses Hörbuch wird bereits verarbeitet."
            else:
                queued = current_service().enqueue_conversion(
                    asin,
                    target_path=normalized_target,
                    force=force,
                    overwrite=overwrite,
                    normalize=normalize,
                    trigger="web",
                )
                if queued.created:
                    flash(f"Job {queued.job_id} wurde eingereiht.", "success")
                    return redirect(url_for("jobs"))
                form_error = queued.error or "Der Job konnte nicht eingereiht werden."

        return render_template(
            "convert.html",
            book=book,
            status=status,
            default_target=str(default_target),
            target_value=target_value,
            existing_targets=existing_targets,
            form_error=form_error,
        )

    @app.post("/books/<asin>/normalize")
    def normalize_book(asin: str):
        try:
            queued = current_service().enqueue_normalize_existing(asin, trigger="web")
        except KeyError:
            abort(404)
        if queued.created:
            flash(f"Normalisierungsjob {queued.job_id} wurde eingereiht.", "success")
            return redirect(url_for("jobs"))
        flash(queued.error or "Der Job konnte nicht eingereiht werden.", "error")
        return redirect(url_for("convert_book", asin=asin))

    @app.route("/books/<asin>/link-existing", methods=["GET", "POST"])
    def link_existing_book(asin: str):
        try:
            book = current_service().get_book(asin)
        except KeyError:
            abort(404)
        query = request.values.get("q", "").strip()
        selected_paths = request.form.getlist("paths")
        manual_paths = request.form.get("manual_paths", "")
        form_error = None

        if request.method == "POST":
            requested_paths = selected_paths + [
                line.strip() for line in manual_paths.splitlines() if line.strip()
            ]
            result = current_service().link_existing_outputs(
                asin, requested_paths, trigger="web"
            )
            if result.successful:
                flash(
                    f"{len(result.outputs)} vorhandene M4B-Datei(en) wurden verknüpft.",
                    "success",
                )
                return redirect(url_for("convert_book", asin=asin))
            form_error = result.error

        files = current_service().discover_existing_m4b_files(asin, query=query)
        return render_template(
            "link_existing.html",
            book=book,
            files=files,
            audiobook_root=str(current_service().config.audiobook_root.resolve()),
            query=query,
            selected_paths=set(selected_paths),
            manual_paths=manual_paths,
            submitted=request.method == "POST",
            form_error=form_error,
        )

    @app.get("/jobs")
    def jobs():
        queued_jobs = current_service().list_jobs()
        return render_template(
            "jobs.html",
            jobs=queued_jobs,
            has_active=any(
                job["status"] in ACTIVE_JOB_STATUSES for job in queued_jobs
            ),
        )

    @app.post("/jobs/<int:job_id>/cancel")
    def cancel_job(job_id: int):
        if current_service().cancel_job(job_id):
            flash(f"Job {job_id} wurde abgebrochen.", "success")
        else:
            flash("Nur wartende Jobs können abgebrochen werden.", "error")
        return redirect(url_for("jobs"))

    @app.post("/jobs/<int:job_id>/dismiss")
    def dismiss_job(job_id: int):
        if current_service().dismiss_job(job_id):
            flash(f"Job {job_id} wurde aus der Liste entfernt.", "success")
        else:
            flash("Nur beendete Jobs können aus der Liste entfernt werden.", "error")
        return redirect(url_for("jobs"))

    return app


def run_automatic_cycle(service: AudibleService) -> None:
    try:
        service.refresh_library()
        service.reconcile_existing_outputs()
        service.process_pending()
    except Exception:
        LOGGER.exception("Automatischer Audible-Durchlauf fehlgeschlagen")


def automatic_scheduler(service: AudibleService, interval: int) -> None:
    while True:
        run_automatic_cycle(service)
        threading.Event().wait(interval)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    service = AudibleService()
    service.reconcile_existing_outputs()
    service.start_job_workers()
    if os.getenv("AUTO_PROCESS_ENABLED", "True") == "True":
        interval = max(60, int(os.getenv("AUTO_PROCESS_INTERVAL", "21600")))
        threading.Thread(
            target=automatic_scheduler,
            args=(service, interval),
            name="automatic-scheduler",
            daemon=True,
        ).start()
    app = create_app(service)
    app.run(
        host=os.getenv("WEB_HOST", "0.0.0.0"),
        port=int(os.getenv("WEB_PORT", "8080")),
        threaded=True,
    )


if __name__ == "__main__":
    main()
