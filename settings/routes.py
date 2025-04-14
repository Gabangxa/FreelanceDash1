from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from flask_login import login_required, current_user, logout_user
from sqlalchemy.exc import SQLAlchemyError
import io
import logging
from PIL import Image
from app import db
from models import UserSettings, User, Client, Project, Task, TimeEntry, Invoice, InvoiceItem
from settings.forms import CompanySettingsForm, InvoiceTemplateForm, DeleteAccountForm
from errors import handle_db_errors

# Get the module logger
logger = logging.getLogger(__name__)

settings_bp = Blueprint('settings', __name__, url_prefix='/settings', template_folder='../templates/settings')

@settings_bp.route('/company', methods=['GET', 'POST'])
@login_required
@handle_db_errors
def company_settings():
    # Get or create user settings
    settings = current_user.get_or_create_settings()
    
    form = CompanySettingsForm()
    
    if form.validate_on_submit():
        try:
            # Update settings with form data
            settings.company_name = form.company_name.data
            settings.company_address = form.company_address.data
            settings.company_phone = form.company_phone.data
            settings.company_email = form.company_email.data
            settings.company_website = form.company_website.data
            
            db.session.commit()
            flash('Company settings updated successfully', 'success')
            
            # Redirect to the same page to prevent form resubmission
            return redirect(url_for('settings.company_settings'))
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.error(f"Database error updating company settings: {str(e)}")
            flash('Error updating company settings. Please try again.', 'danger')
    
    # Populate form with existing data
    if request.method == 'GET':
        form.company_name.data = settings.company_name
        form.company_address.data = settings.company_address
        form.company_phone.data = settings.company_phone
        form.company_email.data = settings.company_email
        form.company_website.data = settings.company_website
    
    return render_template('company_settings.html', form=form, settings=settings)

@settings_bp.route('/invoice-template', methods=['GET', 'POST'])
@login_required
@handle_db_errors
def invoice_template():
    # Get or create user settings
    settings = current_user.get_or_create_settings()
    
    form = InvoiceTemplateForm()
    
    if form.validate_on_submit():
        try:
            # Process logo upload if provided
            if form.invoice_logo.data:
                # Read and process the image
                logo_data = form.invoice_logo.data.read()
                
                try:
                    # Resize if needed to prevent huge file sizes
                    img = Image.open(io.BytesIO(logo_data))
                    
                    # Maintain aspect ratio, but ensure reasonable size
                    max_width = 400
                    if img.width > max_width:
                        ratio = max_width / float(img.width)
                        height = int(float(img.height) * ratio)
                        img = img.resize((max_width, height), Image.LANCZOS)
                    
                    # Save to bytes
                    output_buffer = io.BytesIO()
                    img_format = img.format if img.format else 'PNG'
                    img.save(output_buffer, format=img_format)
                    logo_data = output_buffer.getvalue()
                    
                    # Store in database
                    settings.invoice_logo = logo_data
                    settings.invoice_logo_mimetype = f'image/{img_format.lower()}'
                except Exception as e:
                    logger.error(f"Error processing logo image: {str(e)}")
                    flash('Error processing logo image. Please try a different image.', 'warning')
            
            # Check if logo should be removed
            if form.remove_logo.data == '1':
                settings.invoice_logo = None
                settings.invoice_logo_mimetype = None
            
            # Update other template settings
            settings.invoice_template = form.invoice_template.data
            settings.invoice_color_primary = form.invoice_color_primary.data
            settings.invoice_color_secondary = form.invoice_color_secondary.data
            settings.invoice_footer_text = form.invoice_footer_text.data
            
            db.session.commit()
            flash('Invoice template settings updated successfully', 'success')
            
            # Redirect to the same page to prevent form resubmission
            return redirect(url_for('settings.invoice_template'))
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.error(f"Database error updating invoice template settings: {str(e)}")
            flash('Error updating invoice template settings. Please try again.', 'danger')
    
    # Populate form with existing data
    if request.method == 'GET':
        form.invoice_template.data = settings.invoice_template
        form.invoice_color_primary.data = settings.invoice_color_primary
        form.invoice_color_secondary.data = settings.invoice_color_secondary
        form.invoice_footer_text.data = settings.invoice_footer_text
    
    # Pass logo data to template
    logo_data_uri = settings.get_logo_data_uri() if settings.invoice_logo else None
    
    return render_template('invoice_template.html', form=form, settings=settings, logo_data_uri=logo_data_uri)

@settings_bp.route('/delete-account', methods=['GET', 'POST'])
@login_required
def delete_account():
    """Handle account deletion with confirmation steps."""
    form = DeleteAccountForm()
    
    if form.validate_on_submit():
        try:
            # Start database transaction
            user_id = current_user.id
            username = current_user.username
            
            # Delete all user data in the proper order to respect foreign key constraints
            # First, delete all invoice items
            invoice_ids = [invoice.id for invoice in Invoice.query.join(Project).filter(Project.user_id == user_id)]
            if invoice_ids:
                InvoiceItem.query.filter(InvoiceItem.invoice_id.in_(invoice_ids)).delete(synchronize_session=False)
                db.session.flush()
            
            # Delete invoices
            Invoice.query.join(Project).filter(Project.user_id == user_id).delete(synchronize_session=False)
            db.session.flush()
            
            # Delete time entries
            TimeEntry.query.join(Project).filter(Project.user_id == user_id).delete(synchronize_session=False)
            db.session.flush()
            
            # Delete tasks
            Task.query.join(Project).filter(Project.user_id == user_id).delete(synchronize_session=False)
            db.session.flush()
            
            # Delete projects
            Project.query.filter_by(user_id=user_id).delete(synchronize_session=False)
            db.session.flush()
            
            # Delete clients
            Client.query.filter_by(user_id=user_id).delete(synchronize_session=False)
            db.session.flush()
            
            # Delete user settings (should cascade with user deletion, but being explicit)
            UserSettings.query.filter_by(user_id=user_id).delete(synchronize_session=False)
            db.session.flush()
            
            # Finally, delete the user
            User.query.filter_by(id=user_id).delete(synchronize_session=False)
            
            # Commit all changes
            db.session.commit()
            
            # Log the account deletion
            logger.info(f"User account deleted: {username} (ID: {user_id})")
            
            # Clear the session and log the user out
            logout_user()
            session.clear()
            
            # Show confirmation message
            flash('Your account has been permanently deleted. We\'re sorry to see you go.', 'info')
            return redirect(url_for('index'))
            
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.error(f"Error deleting account: {str(e)}")
            flash('An error occurred while trying to delete your account. Please try again.', 'danger')
    
    return render_template('delete_account.html', form=form)