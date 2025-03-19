from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from sqlalchemy.exc import SQLAlchemyError
import io
import logging
from PIL import Image
from app import db
from models import UserSettings
from settings.forms import CompanySettingsForm, InvoiceTemplateForm
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