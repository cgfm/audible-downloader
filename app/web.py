"""Small server-rendered web UI and automatic scheduler."""

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from audible_service import (
    ACTIVE_JOB_STATUSES,
    AUDIBLE_MARKETPLACES,
    SORT_FIELDS,
    STATUS_LABELS,
    ApplicationConfigurationError,
    AudibleService,
    AuthenticationError,
    utc_now,
)
from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from logging_config import configure_logging

LOGGER = logging.getLogger(__name__)

LOCAL_WEB_HOSTS = {"127.0.0.1", "::1", "localhost"}


class WebConfigurationError(RuntimeError):
    """Raised when the web server would start with an unsafe configuration."""


class RequestRateLimiter:
    """Small per-process fixed-window limiter for abuse-prone endpoints."""

    def __init__(self) -> None:
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts[key]
            while attempts and attempts[0] <= now - window_seconds:
                attempts.popleft()
            if len(attempts) >= limit:
                return False
            attempts.append(now)
            return True


def _secret_from_environment(name: str, file_name: str) -> str:
    secret_file = os.getenv(file_name, "").strip()
    if secret_file:
        try:
            value = Path(secret_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise WebConfigurationError(
                f"Ungültige Webkonfiguration: {file_name} kann nicht gelesen werden."
            ) from exc
        return value.rstrip("\r\n")
    return os.getenv(name, "")


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).digest()


def _validate_web_configuration(app: Flask) -> None:
    host = os.getenv("WEB_HOST", "127.0.0.1").strip().lower()
    mode = app.config["AUTH_MODE"]
    if mode not in {"basic", "form", "proxy", "disabled"}:
        raise WebConfigurationError(
            "Ungültige Webkonfiguration: WEB_AUTH_MODE muss basic, form, proxy "
            "oder disabled sein."
        )
    if mode == "form" and not app.config["ADMIN_PASSWORD"]:
        raise WebConfigurationError(
            "Unsicherer Webstart verweigert: WEB_AUTH_MODE=form ist aktiv, aber "
            "kein Passwort gesetzt. Setze WEB_ADMIN_PASSWORD oder "
            "WEB_ADMIN_PASSWORD_FILE."
        )
    if mode == "basic" and not app.config["ADMIN_PASSWORD"] and host not in LOCAL_WEB_HOSTS:
        raise WebConfigurationError(
            f"Unsicherer Webstart verweigert: WEB_HOST={host or '<leer>'} ist nicht "
            "lokal, WEB_AUTH_MODE=basic ist aktiv, aber kein Passwort gesetzt. "
            "Setze WEB_ADMIN_PASSWORD oder WEB_ADMIN_PASSWORD_FILE. Wenn ein "
            "vorgeschalteter Proxy die Benutzer bereits authentifiziert, verwende "
            "stattdessen WEB_AUTH_MODE=proxy zusammen mit WEB_PROXY_AUTH_HEADER."
        )
    if mode == "disabled" and host not in LOCAL_WEB_HOSTS:
        raise WebConfigurationError(
            f"Unsicherer Webstart verweigert: WEB_AUTH_MODE=disabled ist mit "
            f"WEB_HOST={host or '<leer>'} nicht erlaubt. Verwende Formular- oder "
            "Basic-Authentifizierung oder einen explizit konfigurierten "
            "authentifizierenden Proxy."
        )
    if mode == "proxy" and not app.config["PROXY_AUTH_HEADER"]:
        raise WebConfigurationError(
            "Ungültige Webkonfiguration: WEB_AUTH_MODE=proxy erfordert "
            "WEB_PROXY_AUTH_HEADER mit dem Namen des vertrauenswürdigen "
            "Authentifizierungs-Headers."
        )


def create_app(
    service: AudibleService | None = None, *, enforce_startup_security: bool = False
) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("WEB_SECRET_KEY") or secrets.token_hex(32)
    app.config["AUDIBLE_SERVICE"] = service or AudibleService()
    app.config["ADMIN_USERNAME"] = os.getenv("WEB_ADMIN_USERNAME", "admin")
    app.config["ADMIN_PASSWORD"] = _secret_from_environment(
        "WEB_ADMIN_PASSWORD", "WEB_ADMIN_PASSWORD_FILE"
    )
    app.config["AUTH_MODE"] = os.getenv("WEB_AUTH_MODE", "basic").strip().lower()
    app.config["PROXY_AUTH_HEADER"] = os.getenv(
        "WEB_PROXY_AUTH_HEADER", ""
    ).strip()
    app.config["MAX_CONTENT_LENGTH"] = max(
        1024, int(os.getenv("WEB_MAX_REQUEST_BYTES", "1048576"))
    )
    app.config.update(
        PERMANENT_SESSION_LIFETIME=timedelta(
            hours=max(1, int(os.getenv("WEB_SESSION_HOURS", "12")))
        ),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=os.getenv("WEB_SECURE_COOKIES", "True") == "True",
    )
    if enforce_startup_security:
        _validate_web_configuration(app)
    rate_limiter = RequestRateLimiter()

    def current_service() -> AudibleService:
        return app.config["AUDIBLE_SERVICE"]

    def csrf_token() -> str:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    app.jinja_env.globals["csrf_token"] = csrf_token

    def form_session_token() -> str:
        secret_key = app.config["SECRET_KEY"]
        key = (
            secret_key.encode("utf-8", errors="surrogatepass")
            if isinstance(secret_key, str)
            else bytes(secret_key)
        )
        identity = (
            f"audible-web-auth\0{app.config['ADMIN_USERNAME']}\0"
            f"{app.config['ADMIN_PASSWORD']}"
        ).encode("utf-8", errors="surrogatepass")
        return hmac.new(key, identity, hashlib.sha256).hexdigest()

    def form_session_valid() -> bool:
        saved_token = session.get("web_auth_token", "")
        return bool(saved_token) and hmac.compare_digest(
            saved_token, form_session_token()
        )

    def safe_next_url(value: str | None) -> str:
        candidate = (value or "").strip()
        parsed = urlsplit(candidate)
        if (
            not candidate.startswith("/")
            or candidate.startswith("//")
            or "\\" in candidate
            or parsed.scheme
            or parsed.netloc
        ):
            return url_for("library")
        return candidate

    authentication_endpoints = {
        "settings",
        "start_audible_authentication",
        "audible_authentication",
        "submit_audible_authentication",
        "cancel_audible_authentication",
        "test_audible_authentication",
        "set_automatic_processing",
        "update_application_settings",
        "backup_database",
        "static",
        "health",
        "readiness",
        "web_login",
        "web_logout",
    }

    @app.before_request
    def protect_web_interface():
        if request.endpoint in {"health", "readiness"}:
            return None
        client = request.remote_addr or "unknown"
        auth_mode = app.config["AUTH_MODE"]
        admin_password = app.config["ADMIN_PASSWORD"]
        if auth_mode == "form":
            if request.endpoint not in {"web_login", "static"} and not form_session_valid():
                session.pop("web_auth_token", None)
                next_url = request.full_path if request.query_string else request.path
                return redirect(url_for("web_login", next=next_url))
        elif auth_mode == "basic" and admin_password:
            authorization = request.authorization
            valid_username = bool(authorization) and hmac.compare_digest(
                _digest(authorization.username or ""),
                _digest(app.config["ADMIN_USERNAME"]),
            )
            valid_password = bool(authorization) and hmac.compare_digest(
                _digest(authorization.password or ""), _digest(admin_password)
            )
            if not (valid_username and valid_password):
                if not rate_limiter.allow(f"auth:{client}", 10, 60):
                    return Response("Zu viele Anmeldeversuche.", 429)
                return Response(
                    "Anmeldung erforderlich.",
                    401,
                    {"WWW-Authenticate": 'Basic realm="Audible Downloader", charset="UTF-8"'},
                )
        elif auth_mode == "proxy":
            header = app.config["PROXY_AUTH_HEADER"]
            if not header or not request.headers.get(header, "").strip():
                return Response("Proxy-Anmeldung erforderlich.", 401)

        if request.endpoint in {
            "refresh_library",
            "test_audible_authentication",
        } and not rate_limiter.allow(f"expensive:{request.endpoint}:{client}", 5, 300):
            return Response("Zu viele Anfragen.", 429)

        if (
            not app.config.get("TESTING")
            and request.endpoint not in authentication_endpoints
            and not current_service().audible_authentication_status()["connected"]
        ):
            return redirect(url_for("settings"))

        if request.method == "POST" and not app.config.get("TESTING"):
            expected = session.get("csrf_token", "")
            supplied = request.form.get("_csrf_token", "")
            if not expected or not hmac.compare_digest(expected, supplied):
                abort(400, description="Ungültiges oder abgelaufenes Formular.")
        return None

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' https: data:; "
            "style-src 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        hsts_seconds = max(0, int(os.getenv("WEB_HSTS_SECONDS", "0")))
        if hsts_seconds and request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", f"max-age={hsts_seconds}"
            )
        return response

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/readyz")
    def readiness():
        try:
            with current_service()._connect() as connection:
                connection.execute("SELECT 1").fetchone()
        except Exception:
            return {"status": "not-ready"}, 503
        return {"status": "ready"}

    @app.route("/login", methods=["GET", "POST"])
    def web_login():
        if app.config["AUTH_MODE"] != "form":
            abort(404)
        next_url = safe_next_url(request.values.get("next"))
        if form_session_valid():
            return redirect(next_url)

        form_error = None
        submitted_username = ""
        if request.method == "POST":
            client = request.remote_addr or "unknown"
            submitted_username = request.form.get("username", "")
            submitted_password = request.form.get("password", "")
            if not rate_limiter.allow(f"form-auth:{client}", 10, 60):
                form_error = "Zu viele Anmeldeversuche. Bitte warte eine Minute."
                status = 429
            else:
                valid_username = hmac.compare_digest(
                    _digest(submitted_username), _digest(app.config["ADMIN_USERNAME"])
                )
                valid_password = hmac.compare_digest(
                    _digest(submitted_password), _digest(app.config["ADMIN_PASSWORD"])
                )
                if valid_username and valid_password:
                    session.clear()
                    session["web_auth_token"] = form_session_token()
                    session.permanent = True
                    csrf_token()
                    return redirect(next_url)
                form_error = "Benutzername oder Passwort ist falsch."
                status = 401
            return (
                render_template(
                    "login.html",
                    form_error=form_error,
                    next_url=next_url,
                    submitted_username=submitted_username,
                ),
                status,
            )

        return render_template(
            "login.html",
            form_error=form_error,
            next_url=next_url,
            submitted_username=submitted_username,
        )

    @app.post("/logout")
    def web_logout():
        if app.config["AUTH_MODE"] != "form":
            abort(404)
        session.clear()
        return redirect(url_for("web_login"))

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
        library_filter = request.args.get("library", "active").strip()
        if library_filter not in ("active", "removed", "all"):
            library_filter = "active"
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1
        requested_sort = request.args.get("sort", "purchase_date").strip()
        sort_key = requested_sort if requested_sort in SORT_FIELDS else "purchase_date"
        requested_direction = request.args.get("direction", "").strip()
        sort_direction = (
            requested_direction
            if requested_direction in ("asc", "desc")
            else ("desc" if sort_key == "purchase_date" else "asc")
        )
        all_books = current_service().list_books(
            query, status_filter, sort_key, sort_direction, library_filter
        )
        page_size = 100
        total = len(all_books)
        page_count = max(1, (total + page_size - 1) // page_size)
        page = min(page, page_count)
        books = all_books[(page - 1) * page_size : page * page_size]
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
                    library=library_filter,
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
            library_filter=library_filter,
            status_labels=STATUS_LABELS,
            sort_key=sort_key,
            sort_direction=sort_direction,
            sort_headers=sort_headers,
            page=page,
            page_count=page_count,
            total=total,
        )

    @app.get("/settings")
    def settings():
        auth_status = current_service().audible_authentication_status()
        return render_template(
            "settings.html",
            auth_status=auth_status,
            active_flow=current_service().active_authentication_flow(),
            marketplaces=AUDIBLE_MARKETPLACES,
            selected_country=auth_status["country_code"] or "de",
            web_auth_enabled=(
                bool(app.config["ADMIN_PASSWORD"])
                or app.config["AUTH_MODE"] == "proxy"
            ),
            operational=current_service().operational_status(),
            application_settings=current_service().application_settings(),
        )

    @app.post("/settings/application")
    def update_application_settings():
        values = {
            "auto_download_from_date": request.form.get(
                "auto_download_from_date", ""
            ),
            "audiobook_folders": request.form.get("audiobook_folders", "False"),
            "single_book_path_template": request.form.get(
                "single_book_path_template", ""
            ),
            "series_book_path_template": request.form.get(
                "series_book_path_template", ""
            ),
            "auto_process_enabled": request.form.get(
                "auto_process_enabled", "False"
            ),
            "auto_process_interval": request.form.get("auto_process_interval", ""),
            "log_level": request.form.get("log_level", ""),
        }
        try:
            current_service().update_application_settings(values)
        except ApplicationConfigurationError as exc:
            flash(str(exc), "error")
        else:
            flash("Anwendungseinstellungen gespeichert.", "success")
        return redirect(url_for("settings"))

    @app.post("/settings/automatic-processing")
    def set_automatic_processing():
        paused = request.form.get("paused") == "1"
        current_service().set_automatic_processing_paused(paused)
        flash(
            "Automatische Verarbeitung pausiert."
            if paused
            else "Automatische Verarbeitung fortgesetzt.",
            "success",
        )
        return redirect(url_for("settings"))

    @app.post("/settings/database-backup")
    def backup_database():
        try:
            backup = current_service().create_database_backup()
            flash(f"Datenbanksicherung erstellt: {backup.name}", "success")
        except Exception as exc:
            message = current_service().public_error_message(exc)
            LOGGER.error("Database backup failed: %s", message)
            flash(f"Datenbanksicherung fehlgeschlagen: {message}", "error")
        return redirect(url_for("settings"))

    @app.get("/metrics")
    def metrics():
        return current_service().operational_metrics()

    @app.post("/settings/audible/authenticate")
    def start_audible_authentication():
        auth_status = current_service().audible_authentication_status()
        country_code = (
            auth_status["country_code"]
            if auth_status["connected"]
            else request.form.get("country_code", "de")
        )
        with_username = (
            auth_status["with_username"]
            if auth_status["connected"]
            else request.form.get("with_username") == "1"
        )
        try:
            flow = current_service().start_authentication(
                country_code,
                with_username=with_username,
                queue_pending=(
                    current_service().config.automatic_processing_enabled
                    and not current_service().automatic_processing_paused()
                ),
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("settings"))
        return redirect(
            url_for("audible_authentication", flow_id=flow["flow_id"])
        )

    @app.get("/settings/audible/authentication/<flow_id>")
    def audible_authentication(flow_id: str):
        flow = current_service().authentication_flow(flow_id)
        if flow is None:
            abort(404)
        return render_template("authentication.html", flow=flow)

    @app.post("/settings/audible/authentication/<flow_id>/complete")
    def submit_audible_authentication(flow_id: str):
        try:
            current_service().submit_authentication_callback(
                flow_id, request.form.get("callback_url", "")
            )
        except KeyError:
            abort(404)
        except ValueError as exc:
            flow = current_service().authentication_flow(flow_id)
            if flow is None:
                abort(404)
            return render_template(
                "authentication.html", flow=flow, form_error=str(exc)
            ), 400
        return redirect(url_for("audible_authentication", flow_id=flow_id))

    @app.post("/settings/audible/authentication/<flow_id>/cancel")
    def cancel_audible_authentication(flow_id: str):
        if current_service().cancel_authentication(flow_id):
            flash("Die Audible-Anmeldung wurde abgebrochen.", "warning")
        return redirect(url_for("settings"))

    @app.post("/settings/audible/test")
    def test_audible_authentication():
        try:
            count = current_service().test_authentication()
            flash(
                f"Audible-Verbindung erfolgreich geprüft ({count} Bibliothekseinträge).",
                "success",
            )
        except AuthenticationError as exc:
            flash(str(exc), "error")
        except Exception as exc:
            LOGGER.error(
                "Audible authentication test failed (%s)", exc.__class__.__name__
            )
            flash("Die Audible-Verbindung konnte nicht geprüft werden.", "error")
        return redirect(url_for("settings"))

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
            message = current_service().public_error_message(exc)
            LOGGER.error("Bibliothek konnte nicht aktualisiert werden: %s", message)
            flash(f"Bibliothek konnte nicht aktualisiert werden: {message}", "error")
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
            flash(f"Abbruch für Job {job_id} wurde angefordert.", "success")
        else:
            flash("Nur aktive Jobs können abgebrochen werden.", "error")
        return redirect(url_for("jobs"))

    @app.post("/jobs/<int:job_id>/retry")
    def retry_job(job_id: int):
        queued = current_service().retry_job(job_id)
        if queued.created:
            flash(f"Job {queued.job_id} wurde erneut eingereiht.", "success")
        else:
            flash(queued.error or "Der Job konnte nicht wiederholt werden.", "error")
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
    started_at = utc_now()
    if not service.audible_authentication_status()["connected"]:
        LOGGER.info("Automatischer Durchlauf wartet auf die Audible-Anmeldung")
        service.record_automatic_cycle(
            "waiting_for_authentication",
            started_at=started_at,
            detail="Audible ist noch nicht verbunden.",
        )
        return
    try:
        service.refresh_library()
        service.reconcile_existing_outputs()
        if service.automatic_processing_paused():
            detail = "Bibliothek aktualisiert; automatische Verarbeitung pausiert."
            status = "paused"
        else:
            queued = service.process_pending()
            detail = f"Bibliothek aktualisiert; {len(queued)} Job(s) geprüft."
            status = "success"
        service.record_automatic_cycle(status, started_at=started_at, detail=detail)
    except Exception as exc:
        message = service.public_error_message(exc)
        service.record_automatic_cycle("error", started_at=started_at, detail=message)
        LOGGER.error("Automatischer Audible-Durchlauf fehlgeschlagen: %s", message)


def automatic_scheduler(service: AudibleService) -> None:
    next_run_at: float | None = None
    while True:
        enabled = service.config.automatic_processing_enabled
        interval = service.config.automatic_process_interval
        now = time.monotonic()
        if enabled and next_run_at is None:
            next_run_at = now
        if enabled and next_run_at is not None and now >= next_run_at:
            run_automatic_cycle(service)
            next_run_at = time.monotonic() + interval
        if not enabled:
            next_run_at = None

        wait_seconds = (
            max(0.1, next_run_at - time.monotonic())
            if enabled and next_run_at is not None
            else 3600
        )
        previous_enabled = enabled
        previous_interval = interval
        changed = service.wait_for_application_settings_change(wait_seconds)
        if changed:
            new_enabled = service.config.automatic_processing_enabled
            new_interval = service.config.automatic_process_interval
            if not new_enabled:
                next_run_at = None
            elif not previous_enabled:
                next_run_at = time.monotonic()
            elif new_interval != previous_interval:
                next_run_at = time.monotonic() + new_interval


def main() -> None:
    os.umask(0o077)
    configure_logging()
    try:
        service = AudibleService()
        app = create_app(service, enforce_startup_security=True)
    except (ValueError, WebConfigurationError) as exc:
        LOGGER.critical("Start abgebrochen: %s", exc)
        raise SystemExit(78) from None
    service.reconcile_existing_outputs()
    service.start_job_workers()
    threading.Thread(
        target=automatic_scheduler,
        args=(service,),
        name="automatic-scheduler",
        daemon=True,
    ).start()
    app.run(
        host=os.getenv("WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("WEB_PORT", "8080")),
        threaded=True,
    )


if __name__ == "__main__":
    main()
