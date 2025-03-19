"""
Email functionality for the Freelancer Suite application.
"""
import os
import logging
from threading import Thread
from flask import current_app, render_template
from flask_mail import Mail, Message

# Initialize mail extension
mail = Mail()

# Setup logger
logger = logging.getLogger('mail')

def init_app(app):
    """Initialize the mail extension with the Flask app."""
    mail_server = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
    mail_port = int(os.environ.get('MAIL_PORT', 587))
    mail_use_tls = os.environ.get('MAIL_USE_TLS', 'true').lower() in ['true', 'yes', '1']
    mail_username = os.environ.get('MAIL_USERNAME', None)
    mail_password = os.environ.get('MAIL_PASSWORD', None)
    mail_default_sender = os.environ.get('MAIL_DEFAULT_SENDER', mail_username)
    
    app.config.update(
        MAIL_SERVER=mail_server,
        MAIL_PORT=mail_port,
        MAIL_USE_TLS=mail_use_tls,
        MAIL_USERNAME=mail_username,
        MAIL_PASSWORD=mail_password,
        MAIL_DEFAULT_SENDER=mail_default_sender
    )
    
    # Initialize mail extension
    mail.init_app(app)
    
    logger.info(f"Mail service initialized. Server: {mail_server}, Port: {mail_port}")
    
    # Check if we have credentials
    if not mail_username or not mail_password:
        logger.warning("Mail credentials not set. Email functionality will not work.")

def send_email_async(app, msg):
    """Send an email asynchronously."""
    with app.app_context():
        try:
            mail.send(msg)
            logger.info(f"Email sent to {msg.recipients}")
        except Exception as e:
            logger.error(f"Failed to send email: {str(e)}")

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
        
        # Send the email asynchronously to avoid blocking
        app = current_app._get_current_object()
        Thread(target=send_email_async, args=(app, msg)).start()
        
        return True
    except Exception as e:
        logger.error(f"Error creating email: {str(e)}")
        return False

def send_welcome_email(user):
    """Send a welcome email to a newly registered user."""
    from datetime import datetime
    
    subject = "Welcome to Freelancer Suite!"
    
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