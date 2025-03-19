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
        
    def get_or_create_settings(self):
        """Get the user settings or create default settings if none exist."""
        if not hasattr(self, 'settings') or self.settings is None:
            settings = UserSettings(user_id=self.id)
            db.session.add(settings)
            db.session.commit()
        return self.settings

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
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # Composite index for reporting queries
    __table_args__ = (
        Index('idx_time_entry_project_date', 'project_id', 'start_time'),
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