from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy.exc import SQLAlchemyError
from app import db, logger
from models import User
from auth.forms import LoginForm, RegistrationForm, ResetPasswordRequestForm, ResetPasswordForm, MagicLinkRequestForm
from flask_wtf import FlaskForm
from werkzeug.security import generate_password_hash
from errors import handle_db_errors, UserFriendlyError
from mail import send_welcome_email, send_password_reset_email, send_magic_link_email
from utils.security import is_safe_url
from utils.rate_limit import hit as _rate_limit_hit, client_ip
import re
import smtplib

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')

# Sliding-window rate-limit policy as (limit, window_seconds). Endpoints are
# keyed per-IP and, where an email is known, also per-email -- so a botnet
# spread across many IPs still can't brute-force or email-bomb one specific
# account past the per-email cap.
# Per-IP login cap is deliberately loose so a NAT'd office (many legit users
# behind one egress IP) can't collectively trip it; the tight per-email cap is
# the real brute-force control against any single account.
_LOGIN_IP_LIMIT = (20, 300)          # 20 attempts / 5 min per client IP
_LOGIN_EMAIL_LIMIT = (5, 300)        # 5 attempts / 5 min per targeted account
_REGISTER_IP_LIMIT = (5, 3600)       # 5 sign-ups / hour per client IP
_EMAIL_SEND_IP_LIMIT = (5, 3600)     # 5 reset/magic-link requests / hour per IP
_EMAIL_SEND_EMAIL_LIMIT = (3, 3600)  # 3 emails / hour to a given address

_RATE_LIMIT_MSG = 'Too many attempts. Please wait a few minutes and try again.'


def _rate_limited(scope, identifier, policy):
    """Return True when this attempt should be BLOCKED (limit exceeded).

    ``policy`` is a ``(limit, window_seconds)`` tuple. Keys are namespaced by
    ``scope`` so login, register, and email-send counters never collide.
    """
    limit, window = policy
    return not _rate_limit_hit(f"{scope}:{identifier}", limit, window)

@auth_bp.route('/login', methods=['GET', 'POST'])
@handle_db_errors
def login():
    if current_user.is_authenticated:
        return redirect(url_for('projects.dashboard'))

    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.lower().strip()
        # Count the attempt BEFORE checking the password so a brute-forcer
        # can't get unlimited tries. Keyed per-IP and per-target-email.
        if (_rate_limited('login:ip', client_ip(), _LOGIN_IP_LIMIT)
                or _rate_limited('login:email', email, _LOGIN_EMAIL_LIMIT)):
            logger.warning("Login rate limit hit (ip=%s)", client_ip())
            flash(_RATE_LIMIT_MSG, 'warning')
            return redirect(url_for('auth.login'))
        try:
            user = User.query.filter_by(email=email).first()
            if user is None or not user.check_password(form.password.data):
                flash('Invalid email or password', 'danger')
                # Log failed login attempts but avoid exposing which field was wrong
                logger.warning(f"Failed login attempt for email: {form.email.data}")
                return redirect(url_for('auth.login'))

            # Successfully authenticated
            login_user(user, remember=form.remember_me.data)
            logger.info(f"User logged in: {user.username} (ID: {user.id})")

            # Security: Safe redirect handling. is_safe_url enforces a
            # same-origin http(s) allowlist and rejects javascript:/data:/
            # protocol-relative payloads that the previous netloc-only
            # check would let through.
            next_page = request.args.get('next')
            if not is_safe_url(next_page):
                next_page = url_for('projects.dashboard')
            return redirect(next_page)

        except SQLAlchemyError as e:
            logger.exception("Database error during login")
            db.session.rollback()
            flash('A system error occurred. Please try again later.', 'danger')

    return render_template('auth/login.html', title='Sign In', form=form)

@auth_bp.route('/register', methods=['GET', 'POST'])
@handle_db_errors
def register():
    if current_user.is_authenticated:
        return redirect(url_for('projects.dashboard'))

    form = RegistrationForm()
    if form.validate_on_submit():
        # Throttle sign-ups per IP to blunt automated account creation.
        if _rate_limited('register:ip', client_ip(), _REGISTER_IP_LIMIT):
            logger.warning("Registration rate limit hit (ip=%s)", client_ip())
            flash(_RATE_LIMIT_MSG, 'warning')
            return redirect(url_for('auth.register'))
        try:
            # Additional validation for username and password strength
            if not form.username.data or not re.match(r'^[a-zA-Z0-9_]+$', form.username.data):
                flash('Username must contain only letters, numbers, and underscores', 'danger')
                return render_template('auth/register.html', title='Register', form=form)

            if not form.password.data or len(form.password.data) < 8:
                flash('Password must be at least 8 characters long', 'danger')
                return render_template('auth/register.html', title='Register', form=form)

            # Create new user with sanitized inputs
            user = User(
                username=form.username.data.strip(),
                email=form.email.data.lower().strip()
            )
            user.set_password(form.password.data)

            db.session.add(user)
            db.session.commit()

            # Send welcome email
            email_sent = False
            try:
                email_sent = send_welcome_email(user)
                if email_sent:
                    logger.info(f"Welcome email sent to {user.email}")
                else:
                    logger.warning(f"Failed to send welcome email to {user.email}")
            except (smtplib.SMTPException, OSError, ConnectionError) as e:
                logger.exception("Error sending welcome email")

            logger.info(f"New user registered: {user.username} (ID: {user.id})")
            
            # Update the flash message based on email status
            if email_sent:
                flash('Registration successful! A welcome email has been sent to your email address. You can now log in.', 'success')
            else:
                flash('Registration successful! You can now log in.', 'success')
                
            return redirect(url_for('auth.login'))

        except SQLAlchemyError as e:
            db.session.rollback()
            logger.exception("Database error during registration")
            flash('An error occurred during registration. Please try again.', 'danger')

    return render_template('auth/register.html', title='Register', form=form)

@auth_bp.route('/logout')
@login_required
def logout():
    if current_user.is_authenticated:
        user_id = current_user.id
        username = current_user.username
        logout_user()
        logger.info(f"User logged out: {username} (ID: {user_id})")
        flash('You have been logged out successfully', 'info')
    return redirect(url_for('auth.login'))

@auth_bp.route('/magic_link_request', methods=['GET', 'POST'])
@handle_db_errors
def magic_link_request():
    """Step 1 of magic-link sign-in: accept an email and email a one-click
    sign-in URL. Always renders the same neutral confirmation regardless of
    whether the email exists, to avoid leaking account existence -- the
    same posture as the password-reset flow.
    """
    if current_user.is_authenticated:
        return redirect(url_for('projects.dashboard'))

    form = MagicLinkRequestForm()
    if form.validate_on_submit():
        email = form.email.data.lower().strip()
        if (_rate_limited('magic:ip', client_ip(), _EMAIL_SEND_IP_LIMIT)
                or _rate_limited('magic:email', email, _EMAIL_SEND_EMAIL_LIMIT)):
            logger.warning("Magic-link request rate limit hit (ip=%s)", client_ip())
            flash(_RATE_LIMIT_MSG, 'warning')
            return redirect(url_for('auth.login'))
        try:
            user = User.query.filter_by(email=email).first()

            if user:
                # Issue a fresh single-use token (rotates any prior outstanding one).
                raw_token = user.generate_magic_link_token()
                db.session.commit()

                magic_link_url = url_for(
                    'auth.magic_link_login',
                    user_id=user.id,
                    token=raw_token,
                    _external=True,
                )

                email_sent = send_magic_link_email(user, magic_link_url)
                if email_sent:
                    logger.info(f"Magic-link email sent to {user.email}")
                else:
                    logger.warning(f"Failed to send magic-link email to {user.email}")
            else:
                # Don't disclose whether the address is registered. Still log
                # for ops visibility into request volume.
                logger.info(f"Magic-link requested for unknown email: {form.email.data}")

            flash(
                'If your email address exists in our database, you will receive a sign-in link shortly.',
                'info',
            )
            return redirect(url_for('auth.login'))

        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Database error during magic-link request")
            flash('A system error occurred. Please try again later.', 'danger')

    return render_template('auth/magic_link_request.html', title='Email me a sign-in link', form=form)


@auth_bp.route('/magic_link/<int:user_id>/<token>', methods=['GET', 'POST'])
@handle_db_errors
def magic_link_login(user_id, token):
    """Step 2 of magic-link sign-in.

    GET shows a confirm page with a CSRF-protected form. POST atomically
    verifies + burns the token, logs the user in, and redirects.

    We deliberately do NOT log the user in on the GET. Many corporate
    mail gateways and email clients prefetch links to scan them for
    malware, which would otherwise burn the single-use token before the
    human ever clicked it. The interstitial requires a real form
    submission (with CSRF), which scanners do not perform.

    Single-use semantics are enforced by ``User.consume_magic_link_token``,
    which holds a row lock across verify+clear so two near-simultaneous
    POSTs can't both succeed.
    """
    if current_user.is_authenticated:
        return redirect(url_for('projects.dashboard'))

    next_page = request.args.get('next') or ''
    # Bare FlaskForm just to get CSRF protection on the confirm POST --
    # we don't need any user-supplied fields.
    form = FlaskForm()

    if request.method == 'POST':
        if not form.validate_on_submit():
            logger.warning(f"Magic-link POST failed CSRF validation for user_id={user_id}")
            flash('Your sign-in attempt could not be verified. Please try again.', 'danger')
            return redirect(url_for('auth.magic_link_request'))
        try:
            user = User.consume_magic_link_token(user_id, token)
        except SQLAlchemyError:
            logger.exception("Database error during magic-link login")
            flash('A system error occurred. Please try again later.', 'danger')
            return redirect(url_for('auth.login'))

        if user is None:
            logger.warning(f"Invalid, expired, or already-used magic-link for user_id={user_id}")
            flash('That sign-in link is invalid or has expired. Please request a new one.', 'danger')
            return redirect(url_for('auth.magic_link_request'))

        login_user(user, remember=False)
        logger.info(f"User logged in via magic link: {user.username} (ID: {user.id})")

        # next can come from either the query string (carried through from
        # the email URL) or the hidden form field on the confirm page.
        target = request.form.get('next') or next_page
        if not is_safe_url(target):
            target = url_for('projects.dashboard')
        return redirect(target)

    # GET -- render the confirm page. Show a generic "expired" message if
    # the token is already invalid so users know to request a new one
    # without us actually consuming it. We use ``verify_magic_link_token``
    # here for the read-only check; no row lock and no clear.
    user = User.query.get(user_id)
    valid = user is not None and user.verify_magic_link_token(token)

    return render_template(
        'auth/magic_link_confirm.html',
        title='Confirm sign in',
        valid=valid,
        next_page=next_page,
        form=form,
    )


@auth_bp.route('/reset_password_request', methods=['GET', 'POST'])
@handle_db_errors
def reset_password_request():
    # Redirect if user is already logged in
    if current_user.is_authenticated:
        return redirect(url_for('projects.dashboard'))
        
    form = ResetPasswordRequestForm()
    if form.validate_on_submit():
        email = form.email.data.lower().strip()
        if (_rate_limited('reset:ip', client_ip(), _EMAIL_SEND_IP_LIMIT)
                or _rate_limited('reset:email', email, _EMAIL_SEND_EMAIL_LIMIT)):
            logger.warning("Password-reset request rate limit hit (ip=%s)", client_ip())
            flash(_RATE_LIMIT_MSG, 'warning')
            return redirect(url_for('auth.login'))
        try:
            user = User.query.filter_by(email=email).first()

            # Always show success message even if email not found for security
            if user:
                # Generate a secure reset token
                token = user.generate_reset_token()
                db.session.commit()
                
                # Send password reset email
                email_sent = send_password_reset_email(user, token)
                
                if email_sent:
                    logger.info(f"Password reset email sent to {user.email}")
                else:
                    logger.warning(f"Failed to send password reset email to {user.email}")
                    
            flash('If your email address exists in our database, you will receive a password reset link shortly.', 'info')
            return redirect(url_for('auth.login'))
            
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.exception("Database error during password reset request")
            flash('A system error occurred. Please try again later.', 'danger')
            
    return render_template('auth/reset_password_request.html', title='Reset Password', form=form)
    
@auth_bp.route('/reset_password/<token>', methods=['GET', 'POST'])
@handle_db_errors
def reset_password(token):
    # Redirect if user is already logged in
    if current_user.is_authenticated:
        return redirect(url_for('projects.dashboard'))
        
    # Read-only lookup (by token digest) to decide whether to render the
    # form. The token is only *burned* on a successful POST, via
    # consume_reset_token, which locks the row for an atomic verify+set+clear.
    try:
        user = User.find_by_reset_token(token)
    except SQLAlchemyError:
        logger.exception("Database error during password reset token verification")
        flash('A system error occurred. Please try again later.', 'danger')
        return redirect(url_for('auth.reset_password_request'))

    if user is None:
        # Deliberately does not log the token value.
        logger.warning("Invalid or expired password reset token presented")
        flash('The password reset link is invalid or has expired.', 'danger')
        return redirect(url_for('auth.reset_password_request'))

    # Token is valid, proceed with password reset
    form = ResetPasswordForm()
    if form.validate_on_submit():
        try:
            consumed = User.consume_reset_token(token, form.password.data)
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Database error during password reset")
            flash('A system error occurred. Please try again later.', 'danger')
            return render_template('auth/reset_password.html', title='Reset Password', form=form, token=token)

        if consumed is None:
            # Token was valid on GET but was consumed or expired before this
            # POST completed (double-submit / prefetch race).
            flash('The password reset link is invalid or has expired.', 'danger')
            return redirect(url_for('auth.reset_password_request'))

        logger.info(f"Password reset successful for user: {consumed.username} (ID: {consumed.id})")
        flash('Your password has been reset successfully. You can now log in with your new password.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('auth/reset_password.html', title='Reset Password', form=form, token=token)
