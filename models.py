import warnings
from datetime import datetime, timedelta
from typing import Optional
from app import db, login_manager
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import Index
from sqlalchemy.exc import SQLAlchemyError
import secrets
import time

@login_manager.user_loader
def load_user(id):
    return User.query.get(int(id))

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256))
    is_admin = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    reset_token = db.Column(db.String(100), nullable=True, index=True)
    reset_token_expiry = db.Column(db.DateTime, nullable=True)
    magic_link_token_hash = db.Column(db.String(256), nullable=True)
    magic_link_token_expiry = db.Column(db.DateTime, nullable=True)
    # OAuth provider linkage (Task #17). NULL when the account was created
    # purely with email/password. ``oauth_provider`` is the provider key
    # (e.g. ``"google"``); ``oauth_provider_id`` is the provider's stable
    # subject identifier (Google's ``sub`` claim) -- never the email,
    # which the user can change inside the provider account.
    oauth_provider = db.Column(db.String(32), nullable=True)
    oauth_provider_id = db.Column(db.String(255), nullable=True)
    __table_args__ = (
        db.UniqueConstraint(
            'oauth_provider', 'oauth_provider_id',
            name='uq_user_oauth_provider_subject',
        ),
        db.Index(
            'ix_user_oauth_provider_subject',
            'oauth_provider', 'oauth_provider_id',
        ),
    )

    # Relationships
    projects = db.relationship('Project', backref='user', lazy=True, cascade='all, delete-orphan')
    clients = db.relationship('Client', backref='user', lazy=True, cascade='all, delete-orphan')
    notifications = db.relationship('Notification', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        # OAuth-only accounts (created via Google sign-in, Task #17) have
        # ``password_hash`` set to NULL because the user has never set a
        # local password. ``check_password_hash`` would raise
        # ``AttributeError`` on a None hash, which would surface as a 500
        # error if any caller tried email/password login against an
        # OAuth-only row. Treat a missing hash as "no password set" and
        # fall through to the normal "invalid credentials" path -- this
        # also avoids leaking the existence of OAuth-only accounts via
        # a different error response.
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)
        
    def generate_reset_token(self, expires_in=3600):
        """Generate a secure password reset token valid for 'expires_in' seconds."""
        self.reset_token = secrets.token_urlsafe(32)
        self.reset_token_expiry = datetime.utcnow() + timedelta(seconds=expires_in)
        return self.reset_token
        
    def verify_reset_token(self, token):
        """Check if the reset token is valid and not expired."""
        if self.reset_token is None or self.reset_token_expiry is None:
            return False
        if self.reset_token != token:
            return False
        if datetime.utcnow() > self.reset_token_expiry:
            return False
        return True
        
    def clear_reset_token(self):
        """Clear the reset token after it's been used."""
        self.reset_token = None
        self.reset_token_expiry = None

    def generate_magic_link_token(self, expires_in=900):
        """Issue a single-use magic-link sign-in token.

        Returns the raw token (to embed in the email URL). Only the hash
        is stored at rest -- like a password -- so a database leak alone
        does not allow attackers to forge logins. Default lifetime is 15
        minutes; calling this again rotates and invalidates any prior
        outstanding token for this user.
        """
        raw_token = secrets.token_urlsafe(32)
        self.magic_link_token_hash = generate_password_hash(raw_token)
        self.magic_link_token_expiry = datetime.utcnow() + timedelta(seconds=expires_in)
        return raw_token

    def verify_magic_link_token(self, token):
        """Constant-time check that ``token`` matches the stored hash and
        is not expired. Returns True/False; does NOT clear the token --
        callers must call ``clear_magic_link_token`` after a successful
        login to enforce single-use semantics.
        """
        if not token:
            return False
        if self.magic_link_token_hash is None or self.magic_link_token_expiry is None:
            return False
        if datetime.utcnow() > self.magic_link_token_expiry:
            return False
        # check_password_hash performs a constant-time comparison.
        return check_password_hash(self.magic_link_token_hash, token)

    def clear_magic_link_token(self):
        """Invalidate the outstanding magic-link token (single-use)."""
        self.magic_link_token_hash = None
        self.magic_link_token_expiry = None

    @classmethod
    def consume_magic_link_token(cls, user_id, token):
        """Atomically verify and burn a magic-link token.

        Single-use enforcement requires that verification and invalidation
        happen as one indivisible step -- otherwise two near-simultaneous
        clicks (e.g. user click + mailbox-side prefetch) could both pass
        the verify step and both succeed. We hold a row lock with
        ``with_for_update`` for the duration of verify+clear, so a
        concurrent caller blocks until our transaction commits and then
        sees the cleared columns.

        Returns the User on success, or None if the token was missing,
        wrong, expired, or already consumed.
        """
        if not token or user_id is None:
            return None
        try:
            user = cls.query.filter_by(id=user_id).with_for_update().first()
        except SQLAlchemyError:
            db.session.rollback()
            raise
        if user is None or not user.verify_magic_link_token(token):
            # Release the row lock without mutating anything.
            db.session.rollback()
            return None
        user.clear_magic_link_token()
        db.session.commit()
        return user
        
    def get_subscription(self):
        """Get the user's active subscription or None if no active subscription exists."""
        # Try to import Subscription model - this will fail if Polar is disabled
        try:
            # Import here to avoid circular imports
            from polar.models import Subscription
            
            subscription = Subscription.query.filter_by(user_id=self.id).first()
            if subscription and subscription.is_active():
                return subscription
            return None
        except (ImportError, ModuleNotFoundError):
            # Polar integration is disabled, return None
            return None
        except (SQLAlchemyError, AttributeError) as e:
            # Log any other errors but don't crash the application
            import logging
            logger = logging.getLogger(__name__)
            logger.exception("Error getting subscription")
            return None
        
    def _resolve_features(self):
        """Internal: return the active feature dict for this user.

        Pulls from the user's active subscription if one exists, otherwise
        falls back to the shared free-tier defaults defined in
        ``polar.features``. Both code paths return the same schema (same
        keys, same value types) so callers don't have to special-case
        anonymous/free users.
        """
        from polar.features import free_tier_features
        subscription = self.get_subscription()
        if subscription is None:
            return free_tier_features()
        return subscription.get_features()

    def has_feature(self, name: str) -> bool:
        """Return True iff the user has the boolean feature ``name``.

        Only valid for ``KIND_BOOL`` features (e.g. ``custom_branding``,
        ``api_access``). Calling this for a numeric limit or list feature
        returns ``False`` -- use ``get_feature_limit`` for limits.
        """
        from polar.features import feature_kind, KIND_BOOL
        if feature_kind(name) != KIND_BOOL:
            # Defensive: silently returning False for the wrong kind would
            # let bugs sneak by. Be explicit about the contract violation.
            return False
        value = self._resolve_features().get(name, False)
        return bool(value)

    def get_feature_limit(self, name: str) -> Optional[int]:
        """Return the numeric limit for feature ``name``.

        ``None`` means *unlimited* (the caller should skip any cap check).
        Returns ``0`` for features that legitimately allow zero (e.g.
        ``team_members`` on free tier). Only valid for ``KIND_LIMIT``
        features; other kinds raise ValueError so the wrong-method bug is
        caught early instead of silently misbehaving.
        """
        from polar.features import feature_kind, KIND_LIMIT
        if feature_kind(name) != KIND_LIMIT:
            raise ValueError(
                f"get_feature_limit('{name}') called on a non-limit feature. "
                f"Use has_feature() for boolean flags."
            )
        value = self._resolve_features().get(name)
        # The shared schema already returns None for unlimited, so no
        # legacy 0->None translation is needed here. We still defensively
        # coerce 0 through unchanged so a real cap of 0 is preserved.
        if value is None:
            return None
        return int(value)

    def has_subscription_feature(self, feature_name):
        """DEPRECATED: split into ``has_feature`` and ``get_feature_limit``.

        The original method returned a polymorphic ``bool | int`` value,
        which is a footgun: ``if user.has_subscription_feature('clients_limit'):``
        is ``True`` for a cap of 1 but ``False`` for the legacy ``0``-means-
        unlimited sentinel. Use:

            * ``has_feature(name)``       for boolean flags.
            * ``get_feature_limit(name)`` for numeric caps (None = unlimited).

        This shim is intentionally bug-compatible with the old method
        (returns ``0`` for unlimited limits) so any unmigrated caller keeps
        working until the deprecation warnings have flushed them out.
        """
        warnings.warn(
            "User.has_subscription_feature() is deprecated; use has_feature() "
            "for boolean flags and get_feature_limit() for numeric limits "
            "(None = unlimited).",
            DeprecationWarning,
            stacklevel=2,
        )
        from polar.features import feature_kind, KIND_BOOL, KIND_LIMIT, KIND_LIST
        kind = feature_kind(feature_name)
        if kind == KIND_BOOL:
            return self.has_feature(feature_name)
        if kind == KIND_LIMIT:
            value = self.get_feature_limit(feature_name)
            # Preserve the old "0 == unlimited" contract for legacy callers.
            return 0 if value is None else value
        if kind == KIND_LIST:
            return self._resolve_features().get(feature_name, [])
        # Unknown feature: preserve the original method's behavior so a
        # typoed ``*_limit`` lookup still returns the legacy 0 sentinel
        # (instead of False, which would compare oddly with `>=`).
        if feature_name.endswith("_limit"):
            return 0
        return False
        
    def get_sign_in_methods(self):
        """Return the list of authentication methods linked to this account.

        Used by the account-settings page so the user can see at a glance
        whether they can sign in with their password, with a magic link
        (any account with an email can request one), and/or with each
        configured OAuth provider. Order is stable and intentionally
        starts with the most-common method.
        """
        methods = []
        if self.password_hash:
            methods.append('password')
        # Every account with a verified email can use the magic-link
        # flow, regardless of whether they currently have an outstanding
        # token. This is a *capability* list, not a session list.
        if self.email:
            methods.append('magic_link')
        if self.oauth_provider:
            methods.append(f'oauth:{self.oauth_provider}')
        return methods

    def get_or_create_settings(self):
        """Get the user settings or create default settings if none exist."""
        settings = UserSettings.query.filter_by(user_id=self.id).first()
        if settings is None:
            settings = UserSettings()
            settings.user_id = self.id
            db.session.add(settings)
            db.session.commit()
        return settings

class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, index=True)
    email = db.Column(db.String(120), index=True)
    company = db.Column(db.String(100), index=True)
    address = db.Column(db.Text)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # Relationships
    projects = db.relationship('Project', backref='client', lazy=True, cascade='all, delete-orphan')
    invoices = db.relationship('Invoice', backref='client', lazy=True, cascade='all, delete-orphan')

    # Create a composite index for user_id and name for faster client lookup
    __table_args__ = (
        Index('idx_client_user_name', 'user_id', 'name'),
    )

class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, index=True)
    description = db.Column(db.Text)
    start_date = db.Column(db.DateTime, nullable=False, index=True)
    end_date = db.Column(db.DateTime, index=True)
    status = db.Column(db.String(20), default='active', index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # Relationships
    tasks = db.relationship('Task', backref='project', lazy=True, cascade='all, delete-orphan')
    time_entries = db.relationship('TimeEntry', backref='project', lazy=True, cascade='all, delete-orphan')
    invoices = db.relationship('Invoice', backref='project', lazy=True, cascade='all, delete-orphan')

    # Composite index for common queries
    __table_args__ = (
        Index('idx_project_user_status', 'user_id', 'status'),
    )

class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False, index=True)
    description = db.Column(db.Text)
    status = db.Column(db.String(20), default='pending', index=True)
    due_date = db.Column(db.DateTime, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # Relationships
    time_entries = db.relationship('TimeEntry', backref='task', lazy=True, cascade='all, delete-orphan')

    # Composite index for task filtering
    __table_args__ = (
        Index('idx_task_project_status', 'project_id', 'status'),
    )

class TimeEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    start_time = db.Column(db.DateTime, nullable=False, index=True)
    end_time = db.Column(db.DateTime, index=True)
    duration = db.Column(db.Integer)  # Duration in minutes
    description = db.Column(db.Text)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False, index=True)
    task_id = db.Column(db.Integer, db.ForeignKey('task.id'), index=True)
    billable = db.Column(db.Boolean, default=True, index=True)  # Flag for billable time
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # Composite indexes for reporting queries
    __table_args__ = (
        # Index for project-based time tracking reports by date
        Index('idx_time_entry_project_date', 'project_id', 'start_time'),
        # Index for filtering billable entries
        Index('idx_time_entry_billable', 'billable'),
        # Index for date range reports
        Index('idx_time_entry_date_range', 'start_time', 'end_time'),
        # Index for task-based reporting
        Index('idx_time_entry_task_date', 'task_id', 'start_time'),
        # Index for billable project time reports
        Index('idx_time_entry_project_billable', 'project_id', 'billable')
    )

class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(20), unique=True, nullable=False, index=True)
    # Money is Numeric, not Float, so totals like 0.10 + 0.20 don't drift
    # by 1e-17 cents. Precision 12 / scale 2 covers up to 9,999,999,999.99
    # in any single-currency unit which is more than enough for invoice
    # amounts.
    amount = db.Column(db.Numeric(precision=12, scale=2), nullable=False)
    currency = db.Column(db.String(3), nullable=False, default='USD')
    status = db.Column(db.String(20), default='draft', index=True)
    due_date = db.Column(db.DateTime, index=True)
    notes = db.Column(db.Text)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False, index=True)  
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # Updated relationship with cascade delete
    items = db.relationship('InvoiceItem', backref='invoice', lazy=True, cascade='all, delete-orphan')

    # Composite indexes for common queries
    __table_args__ = (
        Index('idx_invoice_client_status', 'client_id', 'status'),
        Index('idx_invoice_project_date', 'project_id', 'created_at'),
    )

class InvoiceItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.Text, nullable=False)
    # Quantity gets 4 decimal places so fractional hours (1.25h, 0.5h)
    # round-trip without precision loss. Rate / amount are 2dp money.
    quantity = db.Column(db.Numeric(precision=12, scale=4), nullable=False)
    rate = db.Column(db.Numeric(precision=12, scale=2), nullable=False)
    amount = db.Column(db.Numeric(precision=12, scale=2), nullable=False)
    invoice_id = db.Column(db.Integer, db.ForeignKey('invoice.id'), nullable=False, index=True)


class UserSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    company_name = db.Column(db.String(100))
    company_address = db.Column(db.Text)
    company_phone = db.Column(db.String(20))
    company_email = db.Column(db.String(120))
    company_website = db.Column(db.String(120))
    invoice_logo = db.Column(db.LargeBinary)
    invoice_logo_mimetype = db.Column(db.String(30))
    invoice_template = db.Column(db.String(20), default='default')
    invoice_color_primary = db.Column(db.String(10), default='#3498db')
    invoice_color_secondary = db.Column(db.String(10), default='#f8f9fa')
    invoice_footer_text = db.Column(db.Text)
    
    deadline_alert_enabled = db.Column(db.Boolean, default=True)
    deadline_alert_7_days = db.Column(db.Boolean, default=True)
    deadline_alert_3_days = db.Column(db.Boolean, default=True)
    deadline_alert_1_day = db.Column(db.Boolean, default=True)
    deadline_alert_custom_days = db.Column(db.Integer, nullable=True)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    user = db.relationship('User', backref=db.backref('settings', uselist=False, cascade='all, delete-orphan'))
    
    def get_active_alert_days(self):
        """Returns a list of days before deadline when alerts should be shown"""
        alert_days = []
        if self.deadline_alert_enabled:
            if self.deadline_alert_7_days:
                alert_days.append(7)
            if self.deadline_alert_3_days:
                alert_days.append(3)
            if self.deadline_alert_1_day:
                alert_days.append(1)
            if self.deadline_alert_custom_days and self.deadline_alert_custom_days > 0:
                alert_days.append(self.deadline_alert_custom_days)
        return sorted(set(alert_days), reverse=True)
    
    def get_logo_data_uri(self):
        """Return the logo as a data URI for embedding in HTML/PDF"""
        if not self.invoice_logo:
            return None
        
        import base64
        encoded = base64.b64encode(self.invoice_logo).decode('utf-8')
        return f"data:{self.invoice_logo_mimetype};base64,{encoded}"


class EmailDeliveryLog(db.Model):
    """Persistent record of every outbound email send attempt.

    This gives operations a single place to see which messages succeeded,
    which failed, and how many retries each took. It is written from the
    background email thread in ``mail.py`` and is the foundation for the
    queue-based delivery system planned for the next phase.
    """
    id = db.Column(db.Integer, primary_key=True)
    recipient = db.Column(db.String(254), nullable=False, index=True)
    subject = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    # 'pending' | 'sent' | 'failed'
    attempts = db.Column(db.Integer, default=0, nullable=False)
    last_error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    sent_at = db.Column(db.DateTime)


class WebhookEvent(db.Model):
    """Store incoming webhook events for processing"""
    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(50), nullable=False, index=True)  # e.g., 'github', 'stripe', 'custom'
    event_type = db.Column(db.String(100), nullable=False, index=True)  # e.g., 'push', 'payment.failed'
    payload = db.Column(db.Text, nullable=False)  # JSON payload
    processed = db.Column(db.Boolean, default=False, index=True)
    processed_at = db.Column(db.DateTime, index=True)
    error_message = db.Column(db.Text)
    signature = db.Column(db.String(256))  # Webhook signature for verification
    headers = db.Column(db.Text)  # Store important headers as JSON
    # NOTE: do not name this `metadata` -- that attribute is reserved on
    # SQLAlchemy DeclarativeBase and assigning to it never persists.
    event_metadata = db.Column(db.Text)  # Security/audit metadata as JSON
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    
    # Relationships
    notifications = db.relationship('Notification', backref='webhook_event', lazy=True, cascade='all, delete-orphan')
    
    # Composite indexes for common queries
    __table_args__ = (
        Index('idx_webhook_source_type', 'source', 'event_type'),
        Index('idx_webhook_processed', 'processed', 'created_at'),
    )


class WebhookRateLimitEvent(db.Model):
    """Per-request marker used to compute sliding-window rate limits.

    Each row is one webhook request from a given (source, client_ip)
    combination. ``WebhookSecurity.check_rate_limit`` inserts a row, then
    counts rows whose ``created_at`` falls inside the configured window.
    Rows older than the window are pruned eagerly on the next insert for
    the same key, keeping the table bounded without a background sweeper.

    This table is only used when ``REDIS_URL`` is unset and the DB
    fallback storage backend is active.
    """
    __tablename__ = 'webhook_rate_limit_event'
    id = db.Column(db.Integer, primary_key=True)
    rate_key = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index('idx_webhook_rl_key_ts', 'rate_key', 'created_at'),
    )


class WebhookFailedAttempt(db.Model):
    """Per-failed-request marker for security-monitoring and alerting.

    Same shape as ``WebhookRateLimitEvent``, but tracks failed validation
    attempts (bad signatures, IP-list rejects, oversize payloads, etc.)
    so we can spot brute-forcing across workers.
    """
    __tablename__ = 'webhook_failed_attempt'
    id = db.Column(db.Integer, primary_key=True)
    attempt_key = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index('idx_webhook_fa_key_ts', 'attempt_key', 'created_at'),
    )


class WebhookCacheEntry(db.Model):
    """Tiny key/value cache used by the DB storage backend.

    Currently only stores the cached upstream IP allowlists for GitHub and
    Stripe so the dynamic refresh in ``webhooks/ip_ranges.py`` doesn't
    have to hit the upstream HTTP endpoint on every request.
    """
    __tablename__ = 'webhook_cache_entry'
    cache_key = db.Column(db.String(200), primary_key=True)
    value = db.Column(db.Text, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=True, index=True)


class Notification(db.Model):
    """Store notifications for users"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text, nullable=False)
    notification_type = db.Column(db.String(50), nullable=False, index=True)  # e.g., 'webhook', 'system', 'reminder'
    priority = db.Column(db.String(20), default='normal', index=True)  # 'low', 'normal', 'high', 'urgent'
    read = db.Column(db.Boolean, default=False, index=True)
    read_at = db.Column(db.DateTime, index=True)
    action_url = db.Column(db.String(500))  # Optional URL for notification action
    webhook_event_id = db.Column(db.Integer, db.ForeignKey('webhook_event.id'), index=True)
    extra_data = db.Column(db.Text)  # JSON data for additional context
    
    # Delivery tracking fields
    delivered = db.Column(db.Boolean, default=False, index=True)
    delivery_attempts = db.Column(db.Integer, default=0)
    last_delivery_attempt = db.Column(db.DateTime, index=True)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    
    # Composite indexes for common queries
    __table_args__ = (
        Index('idx_notification_user_read', 'user_id', 'read'),
        Index('idx_notification_user_type', 'user_id', 'notification_type'),
        Index('idx_notification_priority', 'priority', 'created_at'),
    )


class NotificationSettings(db.Model):
    """User preferences for notifications"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, unique=True, index=True)
    
    # Email notification preferences
    email_enabled = db.Column(db.Boolean, default=True)
    email_webhook_events = db.Column(db.Boolean, default=True)
    email_project_updates = db.Column(db.Boolean, default=True)
    email_invoice_updates = db.Column(db.Boolean, default=True)
    email_payment_notifications = db.Column(db.Boolean, default=True)
    email_system_notifications = db.Column(db.Boolean, default=True)
    
    # In-app notification preferences
    inapp_enabled = db.Column(db.Boolean, default=True)
    inapp_webhook_events = db.Column(db.Boolean, default=True)
    inapp_project_updates = db.Column(db.Boolean, default=True)
    inapp_invoice_updates = db.Column(db.Boolean, default=True)
    inapp_payment_notifications = db.Column(db.Boolean, default=True)
    inapp_system_notifications = db.Column(db.Boolean, default=True)
    
    # Frequency and delivery preferences
    digest_frequency = db.Column(db.String(20), default='daily')  # 'immediate', 'hourly', 'daily', 'weekly'
    quiet_hours_enabled = db.Column(db.Boolean, default=False)
    quiet_hours_start = db.Column(db.Time)
    quiet_hours_end = db.Column(db.Time)
    timezone = db.Column(db.String(50), default='UTC')
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    user = db.relationship('User', backref=db.backref('notification_settings', uselist=False, cascade='all, delete-orphan'))
    
    @staticmethod
    def get_or_create_for_user(user_id):
        """Get or create notification settings for a user"""
        settings = NotificationSettings.query.filter_by(user_id=user_id).first()
        if settings is None:
            settings = NotificationSettings()
            settings.user_id = user_id
            db.session.add(settings)
            db.session.commit()
        return settings