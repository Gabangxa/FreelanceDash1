from datetime import datetime, timedelta
from app import db, login_manager
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import Index
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    reset_token = db.Column(db.String(100), nullable=True, index=True)
    reset_token_expiry = db.Column(db.DateTime, nullable=True)

    # Relationships
    projects = db.relationship('Project', backref='user', lazy=True, cascade='all, delete-orphan')
    clients = db.relationship('Client', backref='user', lazy=True, cascade='all, delete-orphan')
    notifications = db.relationship('Notification', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
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
        except Exception as e:
            # Log any other errors but don't crash the application
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Error getting subscription: {str(e)}")
            return None
        
    def has_subscription_feature(self, feature_name):
        """
        Check if the user has access to a specific feature based on subscription.
        
        Args:
            feature_name: Name of the feature to check (e.g., 'custom_branding', 'team_members')
            
        Returns:
            bool: True if user has access to the feature, False otherwise
        """
        subscription = self.get_subscription()
        
        # If no active subscription, use free tier defaults
        if subscription is None:
            # Default free tier features
            free_features = {
                'clients_limit': 3,
                'projects_limit': 5,
                'custom_branding': False,
                'advanced_reporting': False,
                'team_members': 0,
                'api_access': False,
                'priority_support': False
            }
            
            # For simple boolean features, return the free tier value
            if feature_name in free_features:
                return free_features[feature_name]
                
            # For numeric limits, return the limit value
            if feature_name.endswith('_limit'):
                return free_features.get(feature_name, 0)
                
            return False
            
        # Get features from the subscription
        features = subscription.get_features()
        
        # Check if the feature exists in the subscription
        if feature_name in features:
            return features[feature_name]
            
        # Feature not found
        return False
        
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
    amount = db.Column(db.Float, nullable=False)
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
    quantity = db.Column(db.Float, nullable=False)
    rate = db.Column(db.Float, nullable=False)
    amount = db.Column(db.Float, nullable=False)
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    user = db.relationship('User', backref=db.backref('settings', uselist=False, cascade='all, delete-orphan'))
    
    def get_logo_data_uri(self):
        """Return the logo as a data URI for embedding in HTML/PDF"""
        if not self.invoice_logo:
            return None
        
        import base64
        encoded = base64.b64encode(self.invoice_logo).decode('utf-8')
        return f"data:{self.invoice_logo_mimetype};base64,{encoded}"


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
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    
    # Relationships
    notifications = db.relationship('Notification', backref='webhook_event', lazy=True, cascade='all, delete-orphan')
    
    # Composite indexes for common queries
    __table_args__ = (
        Index('idx_webhook_source_type', 'source', 'event_type'),
        Index('idx_webhook_processed', 'processed', 'created_at'),
    )


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