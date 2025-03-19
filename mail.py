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
    
    # Use the mail_username as the default sender if MAIL_DEFAULT_SENDER is not provided
    mail_default_sender = os.environ.get('MAIL_DEFAULT_SENDER', mail_username)
    
    # If mail_default_sender contains a variable placeholder, replace with actual value
    if mail_default_sender and '${' in mail_default_sender:
        mail_default_sender = mail_default_sender.replace('${MAIL_USERNAME}', mail_username or '')
    
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

def send_email_async(app, msg):
    """Send an email asynchronously."""
    with app.app_context():
        try:
            # Log detailed debugging information
            logger.info(f"Attempting to send email to {msg.recipients}")
            logger.info(f"Email server: {app.config.get('MAIL_SERVER')}:{app.config.get('MAIL_PORT')}")
            logger.info(f"TLS enabled: {app.config.get('MAIL_USE_TLS')}")
            logger.info(f"SSL enabled: {app.config.get('MAIL_USE_SSL')}")
            logger.info(f"Sender: {msg.sender}")
            
            # Send the email
            mail.send(msg)
            logger.info(f"Email successfully sent to {msg.recipients}")
            
        except Exception as e:
            logger.error(f"Failed to send email: {str(e)}")
            
            # Provide more detailed error information
            if "Username and Password not accepted" in str(e):
                logger.error("Email authentication failed. If using Gmail, make sure you're using an App Password, not your regular password.")
                logger.error("For Gmail, go to your Google account → Security → 2-Step Verification → App passwords")
            elif "SMTP connection failed" in str(e):
                logger.error(f"Could not connect to SMTP server {app.config.get('MAIL_SERVER')}:{app.config.get('MAIL_PORT')}")
            elif "SMTP AUTH extension not supported" in str(e):
                logger.error("SMTP server doesn't support authentication or TLS/SSL settings are incorrect")
                
            # Re-raise exception if we're in debug mode
            if app.debug:
                raise

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