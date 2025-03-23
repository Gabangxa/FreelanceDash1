from flask import Blueprint, render_template, redirect, url_for, flash, request, make_response, jsonify, abort
from flask_login import login_required, current_user
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from app import db, logger
from models import Invoice, InvoiceItem, Client, Project
from invoices.forms import InvoiceForm, InvoiceItemForm
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from io import BytesIO
import uuid
from datetime import datetime
from errors import handle_db_errors, UserFriendlyError

invoices_bp = Blueprint('invoices', __name__, url_prefix='/invoices', template_folder='../templates/invoices')

@invoices_bp.route('/')
@login_required
@handle_db_errors
def list_invoices():
    try:
        # Optimized query with eager loading
        invoices = (Invoice.query
                    .join(Client)
                    .options(db.joinedload(Invoice.client))
                    .filter(Client.user_id == current_user.id)
                    .order_by(Invoice.created_at.desc())
                    .all())
        return render_template('list.html', invoices=invoices)
    except SQLAlchemyError as e:
        logger.error(f"Database error in list_invoices: {str(e)}")
        flash('An error occurred while loading invoices. Please try again.', 'danger')
        return render_template('list.html', invoices=[])

@invoices_bp.route('/new', methods=['GET', 'POST'])
@login_required
@handle_db_errors
def create_invoice():
    form = InvoiceForm()

    try:
        # Get all clients for the current user
        clients = Client.query.filter_by(user_id=current_user.id).all()
        form.client_id.choices = [(c.id, c.name) for c in clients]

        # If client_id is provided, populate projects
        if request.method == 'POST' and form.client_id.data:
            projects = Project.query.filter_by(client_id=form.client_id.data).all()
            form.project_id.choices = [(p.id, p.name) for p in projects]

        if form.validate_on_submit():
            # Verify project belongs to selected client
            project = Project.query.get(form.project_id.data)
            if not project or project.client_id != form.client_id.data:
                flash('Invalid project selection', 'danger')
                return redirect(url_for('invoices.create_invoice'))

            # Start a database transaction
            try:
                # Create invoice with UUID-based invoice number
                invoice_number = f"INV-{uuid.uuid4().hex[:8].upper()}"

                # Check for duplicate invoice numbers just to be safe
                existing = Invoice.query.filter_by(invoice_number=invoice_number).first()
                if existing:
                    invoice_number = f"INV-{uuid.uuid4().hex[:8].upper()}"

                # Create invoice
                invoice = Invoice(
                    invoice_number=invoice_number,
                    amount=0,  # Will be calculated from items
                    currency=form.currency.data,
                    due_date=form.due_date.data,
                    notes=form.notes.data,
                    client_id=form.client_id.data,
                    project_id=form.project_id.data,
                    status=form.status.data
                )
                db.session.add(invoice)
                db.session.flush()  # Get the invoice.id without committing

                # Add invoice items
                total_amount = 0
                items_added = False

                for item_data in form.items.data:
                    # Validate item data
                    if not item_data['description'] or item_data['quantity'] <= 0 or item_data['rate'] <= 0:
                        continue

                    quantity = float(item_data['quantity'])
                    rate = float(item_data['rate'])
                    item_amount = quantity * rate

                    item = InvoiceItem(
                        description=item_data['description'],
                        quantity=quantity,
                        rate=rate,
                        amount=item_amount,
                        invoice_id=invoice.id  # Ensure invoice_id is set
                    )
                    db.session.add(item)
                    total_amount += item_amount
                    items_added = True

                # Validate we have at least one item
                if not items_added:
                    db.session.rollback()
                    flash('Invoice must have at least one valid item', 'danger')
                    return render_template('create.html', form=form)

                # Update invoice total
                invoice.amount = total_amount
                db.session.commit()
                logger.info(f"Invoice #{invoice.invoice_number} created by user {current_user.id}")
                flash('Invoice created successfully', 'success')
                return redirect(url_for('invoices.view_invoice', id=invoice.id))

            except IntegrityError as e:
                db.session.rollback()
                logger.error(f"Integrity error creating invoice: {str(e)}")
                flash('Error creating invoice: Duplicate invoice number. Please try again.', 'danger')
            except SQLAlchemyError as e:
                db.session.rollback()
                logger.error(f"Database error creating invoice: {str(e)}")
                flash('Error creating invoice. Please try again.', 'danger')

        return render_template('create.html', form=form)

    except Exception as e:
        logger.error(f"Unexpected error in create_invoice: {str(e)}")
        flash('An unexpected error occurred. Please try again.', 'danger')
        return redirect(url_for('invoices.list_invoices'))

@invoices_bp.route('/<int:id>', methods=['GET', 'POST'])
@login_required
@handle_db_errors
def view_invoice(id):
    try:
        # Secured query with joins and eager loading
        invoice = (Invoice.query
                   .join(Client)
                   .options(db.joinedload(Invoice.client), db.joinedload(Invoice.project), db.joinedload(Invoice.items))
                   .filter(Invoice.id == id, Client.user_id == current_user.id)
                   .first_or_404())

        if request.method == 'POST':
            new_status = request.form.get('status')
            if new_status in ['draft', 'pending', 'paid', 'cancelled']:
                try:
                    invoice.status = new_status
                    db.session.commit()
                    logger.info(f"Invoice #{invoice.invoice_number} status updated to {new_status} by user {current_user.id}")
                    flash('Invoice status updated successfully', 'success')
                except SQLAlchemyError as e:
                    db.session.rollback()
                    logger.error(f"Error updating invoice status: {str(e)}")
                    flash('Error updating status. Please try again.', 'danger')

            return redirect(url_for('invoices.view_invoice', id=id))

        return render_template('detail.html', invoice=invoice)

    except SQLAlchemyError as e:
        logger.error(f"Database error viewing invoice {id}: {str(e)}")
        flash('Error loading invoice data. Please try again.', 'danger')
        return redirect(url_for('invoices.list_invoices'))

@invoices_bp.route('/get-projects/<int:client_id>')
@login_required
@handle_db_errors
def get_projects(client_id):
    try:
        # Verify client belongs to current user
        client = Client.query.filter_by(id=client_id, user_id=current_user.id).first()
        if not client:
            logger.warning(f"Unauthorized project list request for client {client_id} by user {current_user.id}")
            return jsonify([]), 403

        projects = Project.query.filter_by(client_id=client_id).all()
        return jsonify([(p.id, p.name) for p in projects])
    except SQLAlchemyError as e:
        logger.error(f"Error fetching projects for client {client_id}: {str(e)}")
        return jsonify({"error": "Could not fetch projects"}), 500

@invoices_bp.route('/<int:id>/pdf')
@login_required
@handle_db_errors
def generate_pdf(id):
    try:
        # Secured query with joins
        invoice = (Invoice.query
                  .join(Client)
                  .options(db.joinedload(Invoice.client), db.joinedload(Invoice.items), db.joinedload(Invoice.project))
                  .filter(Invoice.id == id, Client.user_id == current_user.id)
                  .first_or_404())
        
        # Get user settings to apply the correct template
        settings = current_user.get_or_create_settings()
        
        buffer = BytesIO()
        p = canvas.Canvas(buffer, pagesize=letter)
        width, height = letter  # For easier positioning calculations
        
        # Define colors based on user settings
        primary_color = settings.invoice_color_primary or '#3498db'
        secondary_color = settings.invoice_color_secondary or '#f8f9fa'
        
        # We'll use the built-in ReportLab fonts instead of custom fonts
        # Built-in fonts include: Helvetica, Helvetica-Bold, Times-Roman, Times-Bold, Courier, Courier-Bold
        
        # Select the template based on user preferences
        template_name = settings.invoice_template or 'default'
        
        if template_name == 'modern':
            # Modern template - clean and minimal
            # Header with logo and invoice number
            p.setFillColorRGB(0.2, 0.2, 0.2)  # Dark gray
            p.rect(0, height - 100, width, 100, fill=1)
            
            # Add logo if available
            if settings.invoice_logo:
                try:
                    logo = ImageReader(BytesIO(settings.invoice_logo))
                    p.drawImage(logo, 50, height - 80, width=100, height=60, mask='auto')
                except Exception as logo_error:
                    logger.error(f"Error adding logo to PDF: {str(logo_error)}")
            
            # Invoice number in white on dark header
            p.setFillColorRGB(1, 1, 1)  # White
            p.setFont('Helvetica-Bold', 16)
            p.drawString(width - 200, height - 50, f"INVOICE #{invoice.invoice_number}")
            
            # Date and status
            p.setFillColorRGB(0.2, 0.2, 0.2)  # Dark gray
            p.setFont('Helvetica', 10)
            p.drawString(50, height - 120, f"Date: {invoice.created_at.strftime('%Y-%m-%d')}")
            p.drawString(50, height - 135, f"Due Date: {invoice.due_date.strftime('%Y-%m-%d')}")
            
            # Status with colored indicator
            status_colors = {
                'draft': (0.5, 0.5, 0.5),      # Gray
                'pending': (0.2, 0.6, 0.9),    # Blue
                'paid': (0.2, 0.8, 0.2),       # Green
                'cancelled': (0.9, 0.2, 0.2),  # Red
            }
            p.setFillColorRGB(*status_colors.get(invoice.status, (0, 0, 0)))
            p.drawString(width - 200, height - 120, f"Status: {invoice.status.upper()}")
            
        elif template_name == 'classic':
            # Classic template - traditional business style
            # Title
            p.setFont('Times-Bold', 24)
            p.drawString(50, height - 50, "INVOICE")
            
            # Add logo if available
            if settings.invoice_logo:
                try:
                    logo = ImageReader(BytesIO(settings.invoice_logo))
                    p.drawImage(logo, width - 150, height - 70, width=100, height=60, mask='auto')
                except Exception as logo_error:
                    logger.error(f"Error adding logo to PDF: {str(logo_error)}")
            
            # Double border lines
            p.setStrokeColorRGB(0.2, 0.2, 0.2)
            p.line(50, height - 80, width - 50, height - 80)
            p.line(50, height - 82, width - 50, height - 82)
            
            # Invoice details in classic format
            p.setFont('Times-Roman', 12)
            p.drawString(50, height - 100, f"Invoice Number: {invoice.invoice_number}")
            p.drawString(50, height - 115, f"Date: {invoice.created_at.strftime('%B %d, %Y')}")
            p.drawString(50, height - 130, f"Due Date: {invoice.due_date.strftime('%B %d, %Y')}")
            p.drawString(50, height - 145, f"Status: {invoice.status.capitalize()}")
            
        elif template_name == 'creative':
            # Creative template - bold and colorful
            # Convert hex to RGB
            p_color = primary_color.lstrip('#')
            rgb = tuple(int(p_color[i:i+2], 16)/255 for i in (0, 2, 4))
            
            # Colorful header
            p.setFillColorRGB(*rgb)
            p.rect(0, height - 120, width, 120, fill=1)
            
            # Add logo if available
            if settings.invoice_logo:
                try:
                    logo = ImageReader(BytesIO(settings.invoice_logo))
                    p.drawImage(logo, 50, height - 100, width=100, height=80, mask='auto')
                except Exception as logo_error:
                    logger.error(f"Error adding logo to PDF: {str(logo_error)}")
            
            # Invoice title with creative styling
            p.setFillColorRGB(1, 1, 1)  # White
            p.setFont('Helvetica-Bold', 28)
            p.drawString(width/2 - 80, height - 70, "INVOICE")
            
            # Diagonal status banner
            p.saveState()
            p.translate(width - 80, height - 40)
            p.rotate(45)
            p.setFillColorRGB(0.1, 0.1, 0.1)
            p.rect(-20, -10, 100, 20, fill=1)
            p.setFillColorRGB(1, 1, 1)
            p.setFont('Helvetica-Bold', 10)
            p.drawCentredString(30, 0, invoice.status.upper())
            p.restoreState()
            
            # Invoice number with creative styling
            p.setFillColorRGB(0.2, 0.2, 0.2)
            p.setFont('Helvetica-Bold', 14)
            p.drawString(width/2 - 80, height - 100, f"#{invoice.invoice_number}")
            
        else:
            # Default template - professional
            # Header section
            p.setFont('Helvetica-Bold', 18)
            p.drawString(50, height - 50, "INVOICE")
            
            # Add logo if available
            if settings.invoice_logo:
                try:
                    logo = ImageReader(BytesIO(settings.invoice_logo))
                    p.drawImage(logo, width - 150, height - 70, width=100, height=60, mask='auto')
                except Exception as logo_error:
                    logger.error(f"Error adding logo to PDF: {str(logo_error)}")
            
            # Basic info
            p.setFont('Helvetica', 10)
            p.drawString(50, height - 70, f"Invoice Number: {invoice.invoice_number}")
            p.drawString(50, height - 85, f"Date: {invoice.created_at.strftime('%Y-%m-%d')}")
            p.drawString(50, height - 100, f"Due Date: {invoice.due_date.strftime('%Y-%m-%d')}")
            
            # Status with color coding
            if invoice.status == 'paid':
                p.setFillColorRGB(0, 0.7, 0)  # Green
            elif invoice.status == 'pending':
                p.setFillColorRGB(0.9, 0.6, 0)  # Orange
            elif invoice.status == 'cancelled':
                p.setFillColorRGB(0.8, 0, 0)  # Red
            else:
                p.setFillColorRGB(0.5, 0.5, 0.5)  # Gray for draft
            
            p.drawString(50, height - 115, f"Status: {invoice.status.upper()}")
            p.setFillColorRGB(0, 0, 0)  # Reset to black
        
        # Common elements across all templates
        y = height - 200  # Starting position
        
        # From (Company) and To (Client) sections
        p.setFont('Helvetica-Bold', 12)
        p.drawString(50, y, "FROM:")
        p.drawString(300, y, "TO:")
        
        p.setFont('Helvetica', 10)
        y -= 15
        
        # Company details
        if settings.company_name:
            p.drawString(50, y, settings.company_name)
            y -= 15
        
        if settings.company_address:
            address_lines = settings.company_address.split('\n')
            for line in address_lines[:3]:  # Limit to 3 lines
                p.drawString(50, y, line.strip())
                y -= 15
        
        if settings.company_email:
            p.drawString(50, y, settings.company_email)
            y -= 15
            
        if settings.company_phone:
            p.drawString(50, y, settings.company_phone)
        
        # Reset y for client details
        y = height - 215
        
        # Client details
        p.drawString(300, y, invoice.client.name)
        y -= 15
        
        if invoice.client.company:
            p.drawString(300, y, invoice.client.company)
            y -= 15
            
        if invoice.client.email:
            p.drawString(300, y, invoice.client.email)
            y -= 15
            
        if invoice.client.address:
            address_lines = invoice.client.address.split('\n')
            for line in address_lines[:3]:  # Limit to 3 lines
                p.drawString(300, y, line.strip())
                y -= 15
        
        # Project info if available
        y = height - 310
        if invoice.project:
            p.setFont('Helvetica-Bold', 11)
            p.drawString(50, y, f"Project: {invoice.project.name}")
            y -= 20
        
        # Line items table header
        p.setFont('Helvetica-Bold', 11)
        p.setFillColorRGB(0.2, 0.2, 0.2)
        
        # Draw table header with background
        p.setFillColorRGB(0.95, 0.95, 0.95)  # Light gray background
        p.rect(50, y - 5, width - 100, 20, fill=1)
        p.setFillColorRGB(0, 0, 0)  # Back to black text
        
        p.drawString(60, y, "DESCRIPTION")
        p.drawString(350, y, "QUANTITY")
        p.drawString(420, y, "RATE")
        p.drawString(500, y, "AMOUNT")
        y -= 25
        
        # Item rows
        p.setFont('Helvetica', 10)
        
        # Alternating row colors
        row_counter = 0
        for item in invoice.items:
            # Alternate row background
            if row_counter % 2 == 0 and template_name in ['modern', 'creative']:
                p.setFillColorRGB(0.97, 0.97, 0.97)  # Very light gray
                p.rect(50, y - 5, width - 100, 20, fill=1)
                p.setFillColorRGB(0, 0, 0)  # Back to black text
            
            # Truncate description if too long and add ellipsis
            description = item.description
            if len(description) > 45:
                description = description[:42] + "..."
            
            p.drawString(60, y, description)
            p.drawString(350, y, f"{item.quantity}")
            p.drawString(420, y, f"{invoice.currency} {item.rate:.2f}")
            p.drawString(500, y, f"{invoice.currency} {item.amount:.2f}")
            
            y -= 20
            row_counter += 1
            
            # Check if we need a new page
            if y < 100:
                p.showPage()
                p.setFont('Helvetica', 10)
                y = height - 50
                p.drawString(50, y, "INVOICE CONTINUED")
                y -= 30
        
        # Total section
        y -= 10
        p.setStrokeColorRGB(0.8, 0.8, 0.8)
        p.line(350, y + 5, width - 50, y + 5)
        
        p.setFont('Helvetica-Bold', 12)
        p.drawString(420, y - 15, "TOTAL:")
        p.drawString(500, y - 15, f"{invoice.currency} {invoice.amount:.2f}")
        
        # Notes section
        if invoice.notes:
            y -= 50
            p.setFont('Helvetica-Bold', 11)
            p.drawString(50, y, "NOTES:")
            p.setFont('Helvetica', 10)
            
            # Split notes into lines
            notes_lines = []
            current_line = ""
            for word in invoice.notes.split():
                if len(current_line + " " + word) > 80:
                    notes_lines.append(current_line)
                    current_line = word
                else:
                    current_line += " " + word if current_line else word
            if current_line:
                notes_lines.append(current_line)
            
            y -= 15
            for line in notes_lines[:5]:  # Limit to 5 lines
                p.drawString(50, y, line)
                y -= 15
        
        # Footer with custom text from settings
        if settings.invoice_footer_text:
            p.setFont('Helvetica', 9)
            p.setFillColorRGB(0.5, 0.5, 0.5)  # Gray text
            
            footer_y = 40  # Bottom of page
            footer_lines = settings.invoice_footer_text.split('\n')
            for line in footer_lines[:3]:  # Limit to 3 lines
                p.drawCentredString(width/2, footer_y, line.strip())
                footer_y -= 12
        
        p.showPage()
        p.save()

        buffer.seek(0)
        response = make_response(buffer.getvalue())
        response.mimetype = 'application/pdf'
        response.headers['Content-Disposition'] = f'attachment; filename=invoice_{invoice.invoice_number}.pdf'

        logger.info(f"PDF generated for invoice #{invoice.invoice_number} by user {current_user.id}")
        return response

    except SQLAlchemyError as e:
        logger.error(f"Error generating PDF for invoice {id}: {str(e)}")
        flash('Error generating PDF. Please try again.', 'danger')
        return redirect(url_for('invoices.view_invoice', id=id))
    except Exception as e:
        logger.error(f"Unexpected error generating PDF: {str(e)}")
        flash('An unexpected error occurred generating the PDF. Please try again.', 'danger')
        return redirect(url_for('invoices.view_invoice', id=id))

@invoices_bp.route('/<int:id>/delete', methods=['POST'])
@login_required
@handle_db_errors
def delete_invoice(id):
    try:
        # Secured query with joins
        invoice = (Invoice.query
                  .join(Client)
                  .filter(Invoice.id == id, Client.user_id == current_user.id)
                  .first_or_404())

        invoice_number = invoice.invoice_number  # Store for logging

        db.session.delete(invoice)
        db.session.commit()
        logger.info(f"Invoice #{invoice_number} deleted by user {current_user.id}")
        flash('Invoice deleted successfully', 'success')
    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Error deleting invoice {id}: {str(e)}")
        flash('Error deleting invoice. Please try again.', 'danger')

    return redirect(url_for('invoices.list_invoices'))