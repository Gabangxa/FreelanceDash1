"""
Email functionality for the WorkVista application.

Outbound email is sent on a background thread so it doesn't block the
request handler. Every send attempt is recorded in the ``EmailDeliveryLog``
table with status, attempt count, and last error so failures are visible to
operations even before a real job queue is wired up.

Important transactional design:
    The EmailDeliveryLog rows are written through an *isolated* SQLAlchemy
    session bound to the same engine, NOT through ``db.session``. This is
    deliberate: ``db.session`` is the request-scoped session, and calling
    ``db.session.commit()`` from here would commit any other pending
    changes the request handler had staged. The dedicated session keeps
    delivery-log writes atomic and side-effect-free.

NOTE: Threading is a transitional implementation. Under multi-worker gunicorn
this is fragile: a worker reload mid-send loses the in-flight thread. The
``EmailDeliveryLog`` rows let a future queue worker (Celery/RQ/APScheduler)
pick up rows where ``status='failed'`` and re-attempt, which is the planned
next step.
"""
import os
import time
import logging
from contextlib import contextmanager
from datetime import datetime
from threading import Thread

from flask import current_app, render_template
from flask_mail import Mail, Message
from sqlalchemy.orm import Session as SASession

# Initialize mail extension
mail = Mail()

# Setup logger
logger = logging.getLogger('mail')

# Retry policy for transient SMTP failures.
MAX_SEND_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (1, 4, 10)  # used for attempts 1, 2, 3 (after-failure sleep)


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
            mail_default_sender = f"WorkVista <{mail_default_sender}>"

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


def _record_attempt(log_id, status, error=None, increment=True):
    """Update the EmailDeliveryLog row for the in-flight send.

    ``status`` is one of ``'pending'`` | ``'sent'`` | ``'failed'``. When
    ``increment`` is True (the default for each attempt) the ``attempts``
    counter is bumped so we get per-retry visibility, not just terminal
    success/failure.
    """
    if log_id is None:
        return
    try:
        from models import EmailDeliveryLog
        with _isolated_session() as session:
            log = session.get(EmailDeliveryLog, log_id)
            if not log:
                return
            if increment:
                log.attempts = (log.attempts or 0) + 1
            log.status = status
            if error:
                log.last_error = str(error)[:2000]
            if status == 'sent':
                log.sent_at = datetime.utcnow()
            session.commit()
    except Exception as e:
        # Never let logging failures crash the email worker.
        logger.error(f"Failed to record email delivery log {log_id}: {e}")


def send_email_async(app, msg, log_id):
    """Send an email asynchronously, with retries and persistent logging.

    Always wrapped in ``app.app_context()`` -- without one the Flask-Mail
    send call cannot resolve the configured SMTP settings.
    """
    try:
        with app.app_context():
            last_error = None
            for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
                try:
                    logger.info(
                        f"Email attempt {attempt}/{MAX_SEND_ATTEMPTS} to "
                        f"{msg.recipients} (log_id={log_id})"
                    )
                    mail.send(msg)
                    logger.info(f"Email successfully sent to {msg.recipients} (log_id={log_id})")
                    _record_attempt(log_id, 'sent')
                    return
                except Exception as e:
                    last_error = e
                    logger.warning(
                        f"Email attempt {attempt} failed for {msg.recipients} "
                        f"(log_id={log_id}): {e}"
                    )

                    # Hint at the most common misconfiguration without spamming logs.
                    err_text = str(e)
                    if "Username and Password not accepted" in err_text:
                        logger.error(
                            "SMTP authentication failed. For Gmail use an App "
                            "Password (Google account → Security → 2-Step "
                            "Verification → App passwords)."
                        )
                    elif "SMTP AUTH extension not supported" in err_text:
                        logger.error("SMTP server does not support AUTH; check TLS/SSL settings.")

                    if attempt < MAX_SEND_ATTEMPTS:
                        # Record this failed attempt so attempts/last_error
                        # reflect every try, not just the terminal outcome.
                        _record_attempt(log_id, 'pending', error=e)
                        backoff = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
                        time.sleep(backoff)

            # All attempts exhausted.
            logger.error(
                f"Email permanently failed after {MAX_SEND_ATTEMPTS} attempts "
                f"to {msg.recipients} (log_id={log_id}): {last_error}"
            )
            _record_attempt(log_id, 'failed', error=last_error)
    except Exception as e:
        # This branch only runs if app_context() itself blew up.
        logger.error(f"Failed to set up app context for email (log_id={log_id}): {e}")
        try:
            with app.app_context():
                _record_attempt(log_id, 'failed', error=e, increment=False)
        except Exception:
            pass


def _create_delivery_log(recipients, subject):
    """Create the EmailDeliveryLog row before dispatching the worker thread.

    Uses an isolated session so this never accidentally commits unrelated
    pending changes from the request handler. Returns the new log id, or
    ``None`` if persistence fails -- in which case we still attempt to send
    (logs go to stderr) but cannot track outcome.
    """
    try:
        from models import EmailDeliveryLog
        with _isolated_session() as session:
            log = EmailDeliveryLog(
                recipient=', '.join(recipients)[:254],
                subject=subject[:500],
                status='pending',
                attempts=0,
            )
            session.add(log)
            session.commit()
            return log.id
    except Exception as e:
        logger.error(f"Failed to create EmailDeliveryLog row: {e}")
        return None


def send_email(subject, recipients, text_body, html_body=None, sender=None):
    """
    Send an email with the given parameters.

    Args:
        subject: The subject of the email
        recipients: List of recipient email addresses
        text_body: The plain text version of the email
        html_body: The HTML version of the email (optional)
        sender: The sender email address (optional, uses default if not provided)
    """
    try:
        msg = Message(subject, recipients=recipients, sender=sender)
        msg.body = text_body
        if html_body:
            msg.html = html_body

        # Get the underlying Flask app object (not the proxy) to pass into
        # the worker thread. The proxy is request-bound and won't survive.
        app = current_app._get_current_object() if hasattr(current_app, '_get_current_object') else current_app

        log_id = _create_delivery_log(recipients, subject)
        Thread(target=send_email_async, args=(app, msg, log_id), daemon=True).start()

        return True
    except Exception as e:
        logger.error(f"Error creating email: {str(e)}")
        return False


def send_welcome_email(user):
    """Send a welcome email to a newly registered user."""
    from datetime import datetime

    subject = "Welcome to WorkVista!"

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


def send_notification_email(user, notification):
    """Send a notification email to a user."""
    from datetime import datetime

    subject = f"[Freelancer Suite] {notification.title}"

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
