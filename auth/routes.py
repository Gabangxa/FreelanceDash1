from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from urllib.parse import urlparse
from sqlalchemy.exc import SQLAlchemyError
from app import db, logger
from models import User
from auth.forms import LoginForm, RegistrationForm
from werkzeug.security import generate_password_hash
from errors import handle_db_errors, UserFriendlyError
import re

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

            # Security: Safe redirect handling
            next_page = request.args.get('next')
            if not next_page or urlparse(next_page).netloc != '':
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

            logger.info(f"New user registered: {user.username} (ID: {user.id})")
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