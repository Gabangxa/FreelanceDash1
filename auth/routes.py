from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy.exc import SQLAlchemyError
from app import db, logger
from models import User
from auth.forms import LoginForm, RegistrationForm, ResetPasswordRequestForm, ResetPasswordForm
from werkzeug.security import generate_password_hash
from errors import handle_db_errors, UserFriendlyError
from mail import send_welcome_email, send_password_reset_email
from utils.security import is_safe_url
import re
import smtplib

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')

@auth_bp.route('/login', methods=['GET', 'POST'])
@handle_db_errors
def login():
    if current_user.is_authenticated:
        return redirect(url_for('projects.dashboard'))

    form = LoginForm()
    if form.validate_on_submit():
        try:
            user = User.query.filter_by(email=form.email.data.lower().strip()).first()
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
            logger.error(f"Database error during login: {str(e)}")
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
            logger.error(f"Database error during registration: {str(e)}")
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

@auth_bp.route('/reset_password_request', methods=['GET', 'POST'])
@handle_db_errors
def reset_password_request():
    # Redirect if user is already logged in
    if current_user.is_authenticated:
        return redirect(url_for('projects.dashboard'))
        
    form = ResetPasswordRequestForm()
    if form.validate_on_submit():
        try:
            user = User.query.filter_by(email=form.email.data.lower().strip()).first()
            
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
            logger.error(f"Database error during password reset request: {str(e)}")
            flash('A system error occurred. Please try again later.', 'danger')
            
    return render_template('auth/reset_password_request.html', title='Reset Password', form=form)
    
@auth_bp.route('/reset_password/<token>', methods=['GET', 'POST'])
@handle_db_errors
def reset_password(token):
    # Redirect if user is already logged in
    if current_user.is_authenticated:
        return redirect(url_for('projects.dashboard'))
        
    # Find user with this token
    user = None
    try:
        # Search for user with this reset token
        user = User.query.filter(User.reset_token == token).first()
        
        # Check if token is valid
        if not user or not user.verify_reset_token(token):
            logger.warning(f"Invalid or expired password reset token: {token}")
            flash('The password reset link is invalid or has expired.', 'danger')
            return redirect(url_for('auth.reset_password_request'))
            
    except SQLAlchemyError as e:
        logger.error(f"Database error during password reset token verification: {str(e)}")
        flash('A system error occurred. Please try again later.', 'danger')
        return redirect(url_for('auth.reset_password_request'))
    
    # Token is valid, proceed with password reset
    form = ResetPasswordForm()
    if form.validate_on_submit():
        try:
            # Update password
            user.set_password(form.password.data)
            # Clear the reset token
            user.clear_reset_token()
            db.session.commit()
            
            logger.info(f"Password reset successful for user: {user.username} (ID: {user.id})")
            flash('Your password has been reset successfully. You can now log in with your new password.', 'success')
            return redirect(url_for('auth.login'))
            
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.error(f"Database error during password reset: {str(e)}")
            flash('A system error occurred. Please try again later.', 'danger')
    
    return render_template('auth/reset_password.html', title='Reset Password', form=form, token=token)