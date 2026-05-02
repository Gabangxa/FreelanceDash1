from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from flask_login import login_required, current_user, logout_user
from sqlalchemy.exc import SQLAlchemyError
import io
import logging
from PIL import Image
from app import db
from models import UserSettings, User, Client, Project, Task, TimeEntry, Invoice, InvoiceItem, NotificationSettings
from settings.forms import CompanySettingsForm, InvoiceTemplateForm, DeleteAccountForm, NotificationSettingsForm, DeadlineAlertSettingsForm
from errors import handle_db_errors

# Get the module logger
logger = logging.getLogger(__name__)

settings_bp = Blueprint('settings', __name__, url_prefix='/settings', template_folder='../templates/settings')


@settings_bp.route('/sign-in-methods')
@login_required
def sign_in_methods():
    """Show the user which authentication methods are linked to their
    account (password, magic link, Google, etc).

    The page is intentionally read-only for now -- adding/removing
    OAuth providers is a follow-up. The goal here is to give the user
    transparency into how their account can be accessed.
    """
    methods = current_user.get_sign_in_methods()
    return render_template(
        'sign_in_methods.html',
        methods=methods,
    )


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
            logger.exception("Database error updating company settings")
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
                        img = img.resize((max_width, height), Image.Resampling.LANCZOS)
                    
                    # Save to bytes
                    output_buffer = io.BytesIO()
                    img_format = img.format if img.format else 'PNG'
                    img.save(output_buffer, format=img_format)
                    logo_data = output_buffer.getvalue()
                    
                    # Store in database
                    settings.invoice_logo = logo_data
                    settings.invoice_logo_mimetype = f'image/{img_format.lower()}'
                except (OSError, ValueError) as e:
                    logger.exception("Error processing logo image")
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
            logger.exception("Database error updating invoice template settings")
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

@settings_bp.route('/notifications', methods=['GET', 'POST'])
@login_required
@handle_db_errors
def notification_settings():
    """Manage notification preferences."""
    from datetime import time
    
    # Get or create notification settings
    notification_settings = NotificationSettings.get_or_create_for_user(current_user.id)
    
    form = NotificationSettingsForm()
    
    if form.validate_on_submit():
        try:
            # Update email notification settings
            notification_settings.email_enabled = form.email_enabled.data
            notification_settings.email_webhook_events = form.email_webhook_events.data
            notification_settings.email_project_updates = form.email_project_updates.data
            notification_settings.email_invoice_updates = form.email_invoice_updates.data
            notification_settings.email_payment_notifications = form.email_payment_notifications.data
            notification_settings.email_system_notifications = form.email_system_notifications.data
            
            # Update in-app notification settings
            notification_settings.inapp_enabled = form.inapp_enabled.data
            notification_settings.inapp_webhook_events = form.inapp_webhook_events.data
            notification_settings.inapp_project_updates = form.inapp_project_updates.data
            notification_settings.inapp_invoice_updates = form.inapp_invoice_updates.data
            notification_settings.inapp_payment_notifications = form.inapp_payment_notifications.data
            notification_settings.inapp_system_notifications = form.inapp_system_notifications.data
            
            # Update delivery preferences
            notification_settings.digest_frequency = form.digest_frequency.data
            notification_settings.timezone = form.timezone.data
            
            # Update quiet hours
            notification_settings.quiet_hours_enabled = form.quiet_hours_enabled.data
            
            # Parse quiet hours times
            if form.quiet_hours_start.data:
                try:
                    hour, minute = map(int, form.quiet_hours_start.data.split(':'))
                    notification_settings.quiet_hours_start = time(hour, minute)
                except (ValueError, TypeError):
                    notification_settings.quiet_hours_start = None
            else:
                notification_settings.quiet_hours_start = None
                
            if form.quiet_hours_end.data:
                try:
                    hour, minute = map(int, form.quiet_hours_end.data.split(':'))
                    notification_settings.quiet_hours_end = time(hour, minute)
                except (ValueError, TypeError):
                    notification_settings.quiet_hours_end = None
            else:
                notification_settings.quiet_hours_end = None
            
            # Update timestamp
            from datetime import datetime
            notification_settings.updated_at = datetime.utcnow()
            
            db.session.commit()
            flash('Notification settings updated successfully', 'success')
            
            # Redirect to the same page to prevent form resubmission
            return redirect(url_for('settings.notification_settings'))
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.exception("Database error updating notification settings")
            flash('Error updating notification settings. Please try again.', 'danger')
    
    # Populate form with existing data
    if request.method == 'GET':
        # Email settings
        form.email_enabled.data = notification_settings.email_enabled
        form.email_webhook_events.data = notification_settings.email_webhook_events
        form.email_project_updates.data = notification_settings.email_project_updates
        form.email_invoice_updates.data = notification_settings.email_invoice_updates
        form.email_payment_notifications.data = notification_settings.email_payment_notifications
        form.email_system_notifications.data = notification_settings.email_system_notifications
        
        # In-app settings
        form.inapp_enabled.data = notification_settings.inapp_enabled
        form.inapp_webhook_events.data = notification_settings.inapp_webhook_events
        form.inapp_project_updates.data = notification_settings.inapp_project_updates
        form.inapp_invoice_updates.data = notification_settings.inapp_invoice_updates
        form.inapp_payment_notifications.data = notification_settings.inapp_payment_notifications
        form.inapp_system_notifications.data = notification_settings.inapp_system_notifications
        
        # Delivery preferences
        form.digest_frequency.data = notification_settings.digest_frequency
        form.timezone.data = notification_settings.timezone
        
        # Quiet hours
        form.quiet_hours_enabled.data = notification_settings.quiet_hours_enabled
        if notification_settings.quiet_hours_start:
            form.quiet_hours_start.data = notification_settings.quiet_hours_start.strftime('%H:%M')
        if notification_settings.quiet_hours_end:
            form.quiet_hours_end.data = notification_settings.quiet_hours_end.strftime('%H:%M')
    
    return render_template('notification_settings.html', form=form, settings=notification_settings)

@settings_bp.route('/deadline-alerts', methods=['GET', 'POST'])
@login_required
@handle_db_errors
def deadline_alert_settings():
    """Manage project deadline alert preferences."""
    settings = current_user.get_or_create_settings()
    
    form = DeadlineAlertSettingsForm()
    
    if form.validate_on_submit():
        try:
            settings.deadline_alert_enabled = form.deadline_alert_enabled.data
            settings.deadline_alert_7_days = form.deadline_alert_7_days.data
            settings.deadline_alert_3_days = form.deadline_alert_3_days.data
            settings.deadline_alert_1_day = form.deadline_alert_1_day.data
            
            custom_days = form.deadline_alert_custom_days.data
            if custom_days and custom_days.strip():
                try:
                    custom_int = int(custom_days.strip())
                    if 1 <= custom_int <= 365:
                        settings.deadline_alert_custom_days = custom_int
                    else:
                        settings.deadline_alert_custom_days = None
                except ValueError:
                    settings.deadline_alert_custom_days = None
            else:
                settings.deadline_alert_custom_days = None
            
            db.session.commit()
            flash('Deadline alert settings updated successfully', 'success')
            return redirect(url_for('settings.deadline_alert_settings'))
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.exception("Database error updating deadline alert settings")
            flash('Error updating deadline alert settings. Please try again.', 'danger')
    
    if request.method == 'GET':
        form.deadline_alert_enabled.data = settings.deadline_alert_enabled if settings.deadline_alert_enabled is not None else True
        form.deadline_alert_7_days.data = settings.deadline_alert_7_days if settings.deadline_alert_7_days is not None else True
        form.deadline_alert_3_days.data = settings.deadline_alert_3_days if settings.deadline_alert_3_days is not None else True
        form.deadline_alert_1_day.data = settings.deadline_alert_1_day if settings.deadline_alert_1_day is not None else True
        form.deadline_alert_custom_days.data = str(settings.deadline_alert_custom_days) if settings.deadline_alert_custom_days else ''
    
    return render_template('deadline_alert_settings.html', form=form, settings=settings)

@settings_bp.route('/export-data', methods=['GET'])
@login_required
def export_data():
    """Show data export options page."""
    return render_template('export_data.html')

@settings_bp.route('/export-data/json', methods=['GET'])
@login_required
def export_data_json():
    """Export user data in JSON format."""
    try:
        import json
        from datetime import datetime
        from collections import OrderedDict
        from decimal import Decimal

        def _json_default(obj):
            # ``Decimal`` (used for invoice money / quantity columns) is
            # not JSON-serializable by default. Render as a plain float
            # so consumers see a number rather than a string -- precision
            # is preserved at 2dp / 4dp by the column type itself.
            if isinstance(obj, Decimal):
                return float(obj)
            raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
        
        # Get user data
        user_data = OrderedDict()
        
        # Basic user info (excluding sensitive data)
        user_data['user'] = {
            'username': current_user.username,
            'email': current_user.email,
            'created_at': current_user.created_at.isoformat() if current_user.created_at else None
        }
        
        # User settings
        settings = current_user.get_or_create_settings()
        user_data['settings'] = {
            'company_name': settings.company_name,
            'company_address': settings.company_address,
            'company_phone': settings.company_phone,
            'company_email': settings.company_email,
            'company_website': settings.company_website,
            'invoice_template': settings.invoice_template,
            'invoice_color_primary': settings.invoice_color_primary,
            'invoice_color_secondary': settings.invoice_color_secondary,
            'invoice_footer_text': settings.invoice_footer_text,
            # Logo is binary so we don't include it
        }
        
        # Clients
        clients = Client.query.filter_by(user_id=current_user.id).all()
        user_data['clients'] = []
        client_map = {}  # To map client IDs to indices in the exported array
        
        for i, client in enumerate(clients):
            client_data = {
                'id': client.id,
                'name': client.name,
                'email': client.email,
                'company': client.company,
                'address': client.address,
                'created_at': client.created_at.isoformat() if client.created_at else None
            }
            user_data['clients'].append(client_data)
            client_map[client.id] = i
        
        # Projects
        projects = Project.query.filter_by(user_id=current_user.id).all()
        user_data['projects'] = []
        project_map = {}  # To map project IDs to indices
        
        for i, project in enumerate(projects):
            project_data = {
                'id': project.id,
                'name': project.name,
                'description': project.description,
                'start_date': project.start_date.isoformat() if project.start_date else None,
                'end_date': project.end_date.isoformat() if project.end_date else None,
                'status': project.status,
                'client_id': project.client_id,
                'client_name': clients[client_map[project.client_id]].name if project.client_id in client_map else None,
                'created_at': project.created_at.isoformat() if project.created_at else None
            }
            user_data['projects'].append(project_data)
            project_map[project.id] = i
        
        # Tasks
        tasks = Task.query.join(Project).filter(Project.user_id == current_user.id).all()
        user_data['tasks'] = []
        task_map = {}  # To map task IDs to indices
        
        for i, task in enumerate(tasks):
            task_data = {
                'id': task.id,
                'title': task.title,
                'description': task.description,
                'status': task.status,
                'due_date': task.due_date.isoformat() if task.due_date else None,
                'project_id': task.project_id,
                'project_name': projects[project_map[task.project_id]].name if task.project_id in project_map else None,
                'created_at': task.created_at.isoformat() if task.created_at else None
            }
            user_data['tasks'].append(task_data)
            task_map[task.id] = i
        
        # Time entries
        time_entries = TimeEntry.query.join(Project).filter(Project.user_id == current_user.id).all()
        user_data['time_entries'] = []
        
        for entry in time_entries:
            entry_data = {
                'id': entry.id,
                'start_time': entry.start_time.isoformat() if entry.start_time else None,
                'end_time': entry.end_time.isoformat() if entry.end_time else None,
                'duration': entry.duration,
                'description': entry.description,
                'project_id': entry.project_id,
                'project_name': projects[project_map[entry.project_id]].name if entry.project_id in project_map else None,
                'task_id': entry.task_id,
                'task_name': tasks[task_map[entry.task_id]].title if entry.task_id and entry.task_id in task_map else None,
                'billable': entry.billable,
                'created_at': entry.created_at.isoformat() if entry.created_at else None
            }
            user_data['time_entries'].append(entry_data)
        
        # Invoices and items
        invoices = Invoice.query.join(Project).filter(Project.user_id == current_user.id).all()
        user_data['invoices'] = []
        
        for invoice in invoices:
            invoice_data = {
                'id': invoice.id,
                'invoice_number': invoice.invoice_number,
                'amount': invoice.amount,
                'currency': invoice.currency,
                'status': invoice.status,
                'due_date': invoice.due_date.isoformat() if invoice.due_date else None,
                'notes': invoice.notes,
                'client_id': invoice.client_id,
                'client_name': clients[client_map[invoice.client_id]].name if invoice.client_id in client_map else None,
                'project_id': invoice.project_id,
                'project_name': projects[project_map[invoice.project_id]].name if invoice.project_id in project_map else None,
                'created_at': invoice.created_at.isoformat() if invoice.created_at else None,
                'items': []
            }
            
            # Add invoice items
            for item in invoice.items:
                item_data = {
                    'id': item.id,
                    'description': item.description,
                    'quantity': item.quantity,
                    'rate': item.rate,
                    'amount': item.amount
                }
                invoice_data['items'].append(item_data)
            
            user_data['invoices'].append(invoice_data)
        
        # Generate timestamp for filename
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"workvista_export_{timestamp}.json"
        
        # Convert to JSON with pretty formatting. ``default=_json_default``
        # handles Decimal columns (Invoice.amount, InvoiceItem.{quantity,
        # rate, amount}) which json's stdlib encoder otherwise rejects.
        json_data = json.dumps(user_data, indent=2, default=_json_default)
        
        # Create response
        from flask import Response
        response = Response(
            json_data,
            mimetype='application/json',
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )
        
        # Log export
        logger.info(f"User {current_user.username} exported data in JSON format")
        
        return response
        
    except (SQLAlchemyError, OSError, ValueError, TypeError) as e:
        logger.exception("Error exporting JSON data")
        flash('An error occurred while exporting your data. Please try again.', 'danger')
        return redirect(url_for('settings.export_data'))

@settings_bp.route('/export-data/csv', methods=['GET'])
@login_required
def export_data_csv():
    """Export user data in CSV format (as a ZIP file with multiple CSV files)."""
    try:
        import csv
        import io
        import zipfile
        from datetime import datetime
        
        # Create a in-memory file-like object for ZIP file
        memory_file = io.BytesIO()
        
        # Create a ZIP file in the memory file
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            
            # Get user data and create CSVs
            
            # ----- Clients CSV -----
            clients_output = io.StringIO()
            clients_writer = csv.writer(clients_output)
            
            # Write header
            clients_writer.writerow(['ID', 'Name', 'Email', 'Company', 'Address', 'Created At'])
            
            # Write data
            clients = Client.query.filter_by(user_id=current_user.id).all()
            for client in clients:
                clients_writer.writerow([
                    client.id,
                    client.name,
                    client.email,
                    client.company,
                    client.address,
                    client.created_at.strftime('%Y-%m-%d %H:%M:%S') if client.created_at else ''
                ])
            
            # Add to ZIP
            zf.writestr('clients.csv', clients_output.getvalue())
            
            # ----- Projects CSV -----
            projects_output = io.StringIO()
            projects_writer = csv.writer(projects_output)
            
            # Write header
            projects_writer.writerow(['ID', 'Name', 'Description', 'Start Date', 'End Date', 
                                     'Status', 'Client ID', 'Client Name', 'Created At'])
            
            # Write data
            projects = Project.query.filter_by(user_id=current_user.id).all()
            for project in projects:
                client_name = client.name if (client := Client.query.get(project.client_id)) else ''
                projects_writer.writerow([
                    project.id,
                    project.name,
                    project.description,
                    project.start_date.strftime('%Y-%m-%d') if project.start_date else '',
                    project.end_date.strftime('%Y-%m-%d') if project.end_date else '',
                    project.status,
                    project.client_id,
                    client_name,
                    project.created_at.strftime('%Y-%m-%d %H:%M:%S') if project.created_at else ''
                ])
            
            # Add to ZIP
            zf.writestr('projects.csv', projects_output.getvalue())
            
            # ----- Tasks CSV -----
            tasks_output = io.StringIO()
            tasks_writer = csv.writer(tasks_output)
            
            # Write header
            tasks_writer.writerow(['ID', 'Title', 'Description', 'Status', 'Due Date',
                                  'Project ID', 'Project Name', 'Created At'])
            
            # Write data
            tasks = Task.query.join(Project).filter(Project.user_id == current_user.id).all()
            for task in tasks:
                project_name = project.name if (project := Project.query.get(task.project_id)) else ''
                tasks_writer.writerow([
                    task.id,
                    task.title,
                    task.description,
                    task.status,
                    task.due_date.strftime('%Y-%m-%d') if task.due_date else '',
                    task.project_id,
                    project_name,
                    task.created_at.strftime('%Y-%m-%d %H:%M:%S') if task.created_at else ''
                ])
            
            # Add to ZIP
            zf.writestr('tasks.csv', tasks_output.getvalue())
            
            # ----- Time Entries CSV -----
            time_entries_output = io.StringIO()
            time_entries_writer = csv.writer(time_entries_output)
            
            # Write header
            time_entries_writer.writerow(['ID', 'Start Time', 'End Time', 'Duration (minutes)',
                                         'Description', 'Project ID', 'Project Name', 
                                         'Task ID', 'Task Name', 'Billable', 'Created At'])
            
            # Write data
            time_entries = TimeEntry.query.join(Project).filter(Project.user_id == current_user.id).all()
            for entry in time_entries:
                project_name = project.name if (project := Project.query.get(entry.project_id)) else ''
                task_name = task.title if entry.task_id and (task := Task.query.get(entry.task_id)) else ''
                
                time_entries_writer.writerow([
                    entry.id,
                    entry.start_time.strftime('%Y-%m-%d %H:%M:%S') if entry.start_time else '',
                    entry.end_time.strftime('%Y-%m-%d %H:%M:%S') if entry.end_time else '',
                    entry.duration,
                    entry.description,
                    entry.project_id,
                    project_name,
                    entry.task_id,
                    task_name,
                    'Yes' if entry.billable else 'No',
                    entry.created_at.strftime('%Y-%m-%d %H:%M:%S') if entry.created_at else ''
                ])
            
            # Add to ZIP
            zf.writestr('time_entries.csv', time_entries_output.getvalue())
            
            # ----- Invoices CSV -----
            invoices_output = io.StringIO()
            invoices_writer = csv.writer(invoices_output)
            
            # Write header
            invoices_writer.writerow(['ID', 'Invoice Number', 'Amount', 'Currency', 'Status',
                                     'Due Date', 'Notes', 'Client ID', 'Client Name',
                                     'Project ID', 'Project Name', 'Created At'])
            
            # Write data
            invoices = Invoice.query.join(Project).filter(Project.user_id == current_user.id).all()
            for invoice in invoices:
                client_name = client.name if (client := Client.query.get(invoice.client_id)) else ''
                project_name = project.name if (project := Project.query.get(invoice.project_id)) else ''
                
                invoices_writer.writerow([
                    invoice.id,
                    invoice.invoice_number,
                    invoice.amount,
                    invoice.currency,
                    invoice.status,
                    invoice.due_date.strftime('%Y-%m-%d') if invoice.due_date else '',
                    invoice.notes,
                    invoice.client_id,
                    client_name,
                    invoice.project_id,
                    project_name,
                    invoice.created_at.strftime('%Y-%m-%d %H:%M:%S') if invoice.created_at else ''
                ])
            
            # Add to ZIP
            zf.writestr('invoices.csv', invoices_output.getvalue())
            
            # ----- Invoice Items CSV -----
            invoice_items_output = io.StringIO()
            invoice_items_writer = csv.writer(invoice_items_output)
            
            # Write header
            invoice_items_writer.writerow(['ID', 'Invoice ID', 'Invoice Number', 'Description',
                                          'Quantity', 'Rate', 'Amount'])
            
            # Write data
            invoice_items = InvoiceItem.query.join(Invoice).join(Project).filter(Project.user_id == current_user.id).all()
            for item in invoice_items:
                invoice_number = invoice.invoice_number if (invoice := Invoice.query.get(item.invoice_id)) else ''
                
                invoice_items_writer.writerow([
                    item.id,
                    item.invoice_id,
                    invoice_number,
                    item.description,
                    item.quantity,
                    item.rate,
                    item.amount
                ])
            
            # Add to ZIP
            zf.writestr('invoice_items.csv', invoice_items_output.getvalue())
            
            # ----- User Settings CSV -----
            settings_output = io.StringIO()
            settings_writer = csv.writer(settings_output)
            
            # Write header
            settings_writer.writerow(['Setting', 'Value'])
            
            # Write data
            settings = current_user.get_or_create_settings()
            settings_writer.writerow(['Company Name', settings.company_name or ''])
            settings_writer.writerow(['Company Address', settings.company_address or ''])
            settings_writer.writerow(['Company Phone', settings.company_phone or ''])
            settings_writer.writerow(['Company Email', settings.company_email or ''])
            settings_writer.writerow(['Company Website', settings.company_website or ''])
            settings_writer.writerow(['Invoice Template', settings.invoice_template or ''])
            settings_writer.writerow(['Invoice Primary Color', settings.invoice_color_primary or ''])
            settings_writer.writerow(['Invoice Secondary Color', settings.invoice_color_secondary or ''])
            settings_writer.writerow(['Invoice Footer Text', settings.invoice_footer_text or ''])
            
            # Add to ZIP
            zf.writestr('settings.csv', settings_output.getvalue())
            
            # ----- README file -----
            readme_content = f"""WorkVista Data Export
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
User: {current_user.username}

This ZIP archive contains the following CSV files:
- clients.csv: Your client records
- projects.csv: Your project records
- tasks.csv: Task records for all your projects
- time_entries.csv: Time tracking entries for all your projects
- invoices.csv: Your invoice records
- invoice_items.csv: Line items for all your invoices
- settings.csv: Your account settings and preferences

For support, please contact support@workvista.example.com
"""
            zf.writestr('README.txt', readme_content)
        
        # Rewind the file pointer to the beginning of the file
        memory_file.seek(0)
        
        # Generate timestamp for filename
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"workvista_export_{timestamp}.zip"
        
        # Create response
        from flask import send_file
        response = send_file(
            memory_file,
            mimetype='application/zip',
            as_attachment=True,
            download_name=filename
        )
        
        # Log export
        logger.info(f"User {current_user.username} exported data in CSV format")
        
        return response
        
    except (SQLAlchemyError, OSError, ValueError, TypeError) as e:
        logger.exception("Error exporting CSV data")
        flash('An error occurred while exporting your data. Please try again.', 'danger')
        return redirect(url_for('settings.export_data'))

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
            logger.exception("Error deleting account")
            flash('An error occurred while trying to delete your account. Please try again.', 'danger')
    
    return render_template('delete_account.html', form=form)