"""
Email functionality for the SoloDolo application.

Outbound email is a durable outbox backed by the ``EmailDeliveryLog`` table:
``send_email`` renders the message and ENQUEUES it (status ``pending``); a
separate drainer, ``drain_email_outbox`` -- run on a schedule via the
``drain-emails`` Flask CLI command (Railway cron) -- sends the queued rows.

This replaces the old daemon-thread sender, which gunicorn's worker recycle
could kill mid-send, silently losing password resets and magic links with
only an orphaned 'pending' row as evidence. Because the full rendered message
is now persisted, a fresh drainer process can always send what a request
enqueued, and every attempt's status/attempts/last_error stay visible.

Important transactional design:
    The enqueue writes its EmailDeliveryLog row through an *isolated*
    SQLAlchemy session bound to the same engine, NOT through ``db.session``.
    This is deliberate: ``db.session`` is the request-scoped session, and
    committing it from here would commit any other pending changes the
    request handler had staged. The dedicated session keeps the enqueue
    atomic and side-effect-free. The drainer, by contrast, runs in its own
    request-less context and uses ``db.session`` directly.
"""
import os
import smtplib
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

import click
from flask import render_template
from flask_mail import Mail, Message
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as SASession

# Initialize mail extension
mail = Mail()

# Set by start_email_drainer; guards against starting a second drainer thread.
_drainer_thread = None

# Setup logger
logger = logging.getLogger('mail')

# A message is retried up to this many times across drain runs before it is
# marked permanently 'failed'.
MAX_SEND_ATTEMPTS = 3

# Drainer tuning.
DRAIN_BATCH_LIMIT = 50          # max messages processed per drain run
STALE_SENDING_MINUTES = 15      # reclaim rows a crashed drainer left 'sending'


def init_app(app):
    """Initialize the mail extension with the Flask app."""
    mail_server = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
    mail_port = int(os.environ.get('MAIL_PORT', 587))
    mail_use_tls = os.environ.get('MAIL_USE_TLS', 'true').lower() in ['true', 'yes', '1']
    mail_username = os.environ.get('MAIL_USERNAME', None)
    mail_password = os.environ.get('MAIL_PASSWORD', None)

    # Use the mail_username as the default sender if MAIL_DEFAULT_SENDER is not provided
    mail_default_sender = os.environ.get('MAIL_DEFAULT_SENDER', mail_username)

    # Parse and format the mail_default_sender appropriately
    if mail_default_sender:
        # Replace any environment variable placeholders
        if '${' in mail_default_sender:
            mail_default_sender = mail_default_sender.replace('${MAIL_USERNAME}', mail_username or '')
        if '$MAIL_USERNAME' in mail_default_sender:
            mail_default_sender = mail_default_sender.replace('$MAIL_USERNAME', mail_username or '')

        # Format the sender display name if it's just an email
        if mail_default_sender and '@' in mail_default_sender and '<' not in mail_default_sender:
            mail_default_sender = f"SoloDolo <{mail_default_sender}>"

        # Log the configured sender for debugging
        logger.info(f"Configured mail sender: {mail_default_sender}")

    app.config.update(
        MAIL_SERVER=mail_server,
        MAIL_PORT=mail_port,
        MAIL_USE_TLS=mail_use_tls,
        MAIL_USERNAME=mail_username,
        MAIL_PASSWORD=mail_password,
        MAIL_DEFAULT_SENDER=mail_default_sender,
        MAIL_DEBUG=app.debug,
        MAIL_USE_SSL=False,  # Force TLS over SSL for Gmail
        MAIL_MAX_EMAILS=None,  # No limit
        MAIL_ASCII_ATTACHMENTS=False
    )

    # Initialize mail extension
    mail.init_app(app)

    logger.info(f"Mail service initialized. Server: {mail_server}, Port: {mail_port}")

    # Check if we have credentials
    if not mail_username or not mail_password:
        logger.warning("Mail credentials not set. Email functionality will not work.")

    # Register the outbox drainer as a CLI command so a scheduler (Railway
    # cron: `flask --app main drain-emails`) can send queued mail out of band.
    @app.cli.command('drain-emails')
    def _drain_emails_command():
        """Send queued emails from the EmailDeliveryLog outbox."""
        summary = drain_email_outbox()
        logger.info("Email outbox drain complete: %s", summary)
        click.echo(f"drain-emails: {summary}")


@contextmanager
def _isolated_session():
    """Yield a SQLAlchemy session bound to the same engine as ``db.session``
    but completely independent of the request-scoped session.

    Imported lazily so this module can be imported before models are
    registered, and to avoid circular imports between ``app`` and ``models``.
    """
    from app import db
    session = SASession(bind=db.engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()


def send_email(subject, recipients, text_body, html_body=None, sender=None):
    """Enqueue an email into the durable outbox (EmailDeliveryLog).

    Does NOT send synchronously -- the scheduled drainer
    (``drain_email_outbox`` / ``flask --app main drain-emails``) delivers
    queued rows. Returns True if the message was enqueued, False if even the
    enqueue failed (in which case there is nothing to retry, so the caller
    should surface the failure). Args mirror the previous signature so the
    ``send_*`` wrappers below are unchanged.
    """
    try:
        from models import EmailDeliveryLog
        with _isolated_session() as session:
            session.add(EmailDeliveryLog(
                recipient=', '.join(recipients)[:254],
                subject=(subject or '')[:500],
                sender=(sender or None),
                text_body=text_body,
                html_body=html_body,
                status='pending',
                attempts=0,
            ))
            session.commit()
        return True
    except (SQLAlchemyError, OSError, TypeError, ValueError):
        logger.exception("Failed to enqueue email to the outbox")
        return False


def _deliver_outbox_row(log_id):
    """Send one claimed outbox row and record the outcome.

    Runs inside the drainer's app context and uses ``db.session`` directly.
    Returns 'sent', 'retried' (transient failure, still under the attempt
    cap and re-queued as pending), or 'failed' (attempt cap reached).
    """
    from app import db
    from models import EmailDeliveryLog

    log = db.session.get(EmailDeliveryLog, log_id)
    if log is None:
        return 'failed'

    recipients = [r.strip() for r in (log.recipient or '').split(',') if r.strip()]
    try:
        msg = Message(log.subject, recipients=recipients, sender=log.sender or None)
        msg.body = log.text_body or ''
        if log.html_body:
            msg.html = log.html_body
        mail.send(msg)
    except (smtplib.SMTPException, OSError, ConnectionError, RuntimeError, ValueError) as e:
        log.attempts = (log.attempts or 0) + 1
        log.last_error = str(e)[:2000]
        # Under the cap -> back to 'pending' so the next drain retries it;
        # at the cap -> terminal 'failed'.
        log.status = 'failed' if log.attempts >= MAX_SEND_ATTEMPTS else 'pending'
        db.session.commit()
        outcome = 'failed' if log.status == 'failed' else 'retried'
        logger.warning(
            "Outbox send %s for log_id=%s (attempt %s/%s): %s",
            outcome, log_id, log.attempts, MAX_SEND_ATTEMPTS, e,
        )
        return outcome

    log.attempts = (log.attempts or 0) + 1
    log.status = 'sent'
    log.sent_at = datetime.utcnow()
    db.session.commit()
    logger.info("Outbox send ok for log_id=%s to %s", log_id, recipients)
    return 'sent'


def drain_email_outbox(batch_limit=DRAIN_BATCH_LIMIT, stale_minutes=STALE_SENDING_MINUTES):
    """Send queued emails from the EmailDeliveryLog outbox.

    Meant to run on a schedule (Railway cron: ``flask --app main
    drain-emails``). It (1) reclaims rows a crashed drainer left in
    'sending' past ``stale_minutes``, then (2) claims 'pending' rows one at
    a time with an atomic pending->sending flip -- so two overlapping
    drainers can never send the same message twice -- and delivers each.
    'sent' rows are never re-selected, making redelivery idempotent.
    Returns a summary dict.
    """
    from app import db
    from models import EmailDeliveryLog

    summary = {'sent': 0, 'retried': 0, 'failed': 0, 'reclaimed': 0}

    # 1. Reclaim stale claims from a crashed drainer.
    stale_cutoff = datetime.utcnow() - timedelta(minutes=stale_minutes)
    reclaimed = (
        EmailDeliveryLog.query
        .filter(EmailDeliveryLog.status == 'sending',
                EmailDeliveryLog.updated_at.isnot(None),
                EmailDeliveryLog.updated_at < stale_cutoff)
        .update({'status': 'pending'}, synchronize_session=False)
    )
    if reclaimed:
        db.session.commit()
        summary['reclaimed'] = reclaimed

    # 2. Snapshot the batch of pending ids up front and process each once.
    # Snapshotting (rather than re-querying for 'pending' in a loop) means a
    # row that fails transiently and reverts to 'pending' waits for the NEXT
    # drain run to retry -- that inter-run gap is the backoff -- instead of
    # being hammered through its whole attempt budget in a single run.
    pending_ids = [
        row.id for row in (
            EmailDeliveryLog.query
            .filter_by(status='pending')
            .order_by(EmailDeliveryLog.created_at)
            .limit(batch_limit)
            .all()
        )
    ]

    for log_id in pending_ids:
        # Atomic claim: only the drainer that flips pending->sending sends it.
        claimed = (
            EmailDeliveryLog.query
            .filter_by(id=log_id, status='pending')
            .update({'status': 'sending', 'updated_at': datetime.utcnow()},
                    synchronize_session=False)
        )
        db.session.commit()
        if not claimed:
            continue  # another drainer beat us to it; skip

        outcome = _deliver_outbox_row(log_id)
        summary[outcome] = summary.get(outcome, 0) + 1

    return summary


def start_email_drainer(app, interval_seconds=None):
    """Start a daemon thread that periodically drains the email outbox.

    This is the in-process consumer for the durable outbox: ``send_email``
    only enqueues, so without a drainer no mail is ever sent. Unlike the old
    per-send daemon thread, this is safe: the outbox is durable, so a drainer
    that dies mid-send loses nothing -- the row stays ``pending``/``sending``
    and is reclaimed on the next poll or after a restart. Under multiple
    gunicorn workers each runs one; the atomic ``pending``->``sending`` claim
    makes concurrent drainers safe (at the cost of some idle polling).

    Mirrors ``webhooks.storage.start_background_sweeper``. Idempotent.
    Disable knobs (either short-circuits and returns ``None``):
      * ``EMAIL_DRAINER_ENABLED`` = ``0``/``false``/``no`` -- opt out, e.g.
        when running ``flask --app main drain-emails`` from an external
        Railway cron service instead.
      * ``FLASK_ENV=test`` -- tests drive ``drain_email_outbox`` directly.
    Interval is ``EMAIL_DRAIN_INTERVAL_SECONDS`` (default 60, floor 15).
    """
    global _drainer_thread

    if _drainer_thread is not None and _drainer_thread.is_alive():
        return None
    if os.environ.get("FLASK_ENV", "").lower() == "test":
        return None
    if os.environ.get("EMAIL_DRAINER_ENABLED", "1").lower() in ("0", "false", "no"):
        logger.info("In-process email drainer disabled via EMAIL_DRAINER_ENABLED")
        return None

    if interval_seconds is None:
        try:
            interval_seconds = int(os.environ.get("EMAIL_DRAIN_INTERVAL_SECONDS", "60"))
        except (TypeError, ValueError):
            interval_seconds = 60
    interval_seconds = max(15, interval_seconds)

    stop = threading.Event()

    def _run():
        # Sleep first so a fast restart loop doesn't hammer the DB.
        while not stop.wait(interval_seconds):
            try:
                with app.app_context():
                    summary = drain_email_outbox()
                if summary.get("sent") or summary.get("failed") or summary.get("reclaimed"):
                    logger.info("Email drainer: %s", summary)
            except Exception:  # noqa: BLE001 - a daemon loop must never die on one bad cycle
                logger.exception("Email drainer cycle failed")

    thread = threading.Thread(target=_run, name="email-drainer", daemon=True)
    thread.start()
    _drainer_thread = thread
    logger.info("In-process email drainer started (interval=%ss)", interval_seconds)
    return thread


def send_welcome_email(user):
    """Send a welcome email to a newly registered user."""
    from datetime import datetime

    subject = "Welcome to SoloDolo!"

    # Prepare context data for templates
    context = {
        'user': user,
        'login_url': f"{os.environ.get('APP_URL', 'http://localhost:5000')}/auth/login",
        'current_year': datetime.utcnow().year
    }

    # Render templates with context
    text_body = render_template('email/welcome.txt', **context)
    html_body = render_template('email/welcome.html', **context)

    return send_email(
        subject=subject,
        recipients=[user.email],
        text_body=text_body,
        html_body=html_body
    )


def send_password_reset_email(user, token):
    """Send a password reset email to a user."""
    from datetime import datetime

    subject = "Password Reset Request"

    # Build the password reset URL
    reset_url = f"{os.environ.get('APP_URL', 'http://localhost:5000')}/auth/reset_password/{token}"

    # Prepare context data for templates
    context = {
        'user': user,
        'reset_url': reset_url,
        'current_year': datetime.utcnow().year,
        'expiry_hours': 1  # Token expiry in hours
    }

    # Render templates with context
    text_body = render_template('email/reset_password.txt', **context)
    html_body = render_template('email/reset_password.html', **context)

    return send_email(
        subject=subject,
        recipients=[user.email],
        text_body=text_body,
        html_body=html_body
    )


def send_magic_link_email(user, magic_link_url, expiry_minutes=15):
    """Send a one-click sign-in link to the user.

    The URL embeds a single-use, short-lived token (default 15 minutes)
    issued by ``User.generate_magic_link_token``. Mirrors the password-reset
    sender so it inherits the same retry + delivery-log machinery.
    """
    from datetime import datetime

    subject = "Your SoloDolo sign-in link"

    context = {
        'user': user,
        'magic_link_url': magic_link_url,
        'current_year': datetime.utcnow().year,
        'expiry_minutes': expiry_minutes,
    }

    text_body = render_template('email/magic_link.txt', **context)
    html_body = render_template('email/magic_link.html', **context)

    return send_email(
        subject=subject,
        recipients=[user.email],
        text_body=text_body,
        html_body=html_body,
    )


def send_notification_email(user, notification):
    """Send a notification email to a user."""
    from datetime import datetime

    subject = f"[SoloDolo] {notification.title}"

    # Build the app URL
    app_url = os.environ.get('APP_URL', 'http://localhost:5000')

    # Prepare context data for templates
    context = {
        'user': user,
        'notification': notification,
        'app_url': app_url,
        'current_year': datetime.utcnow().year
    }

    # Render templates with context
    text_body = render_template('email/notification.txt', **context)
    html_body = render_template('email/notification.html', **context)

    return send_email(
        subject=subject,
        recipients=[user.email],
        text_body=text_body,
        html_body=html_body
    )
