from flask import Blueprint, render_template, redirect, url_for, flash, request, make_response, jsonify, abort
from flask_login import login_required, current_user
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from app import db, logger
from models import Invoice, InvoiceItem, Client, Project
from invoices.forms import InvoiceForm, InvoiceItemForm
from reportlab.pdfgen import canvas
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
                  .options(db.joinedload(Invoice.client), db.joinedload(Invoice.items))
                  .filter(Invoice.id == id, Client.user_id == current_user.id)
                  .first_or_404())

        buffer = BytesIO()
        p = canvas.Canvas(buffer)

        # Add invoice details
        p.drawString(50, 800, f"Invoice #{invoice.invoice_number}")
        p.drawString(50, 780, f"Status: {invoice.status.upper()}")
        p.drawString(50, 760, f"Due Date: {invoice.due_date.strftime('%Y-%m-%d')}")
        p.drawString(50, 740, f"Total Amount: {invoice.currency} {invoice.amount:.2f}")

        # Add client details
        p.drawString(50, 700, f"Client: {invoice.client.name}")
        if invoice.client.email:
            p.drawString(50, 680, f"Email: {invoice.client.email}")

        # Add line items
        y = 620
        p.drawString(50, y, "Description")
        p.drawString(300, y, "Quantity")
        p.drawString(400, y, "Rate")
        p.drawString(500, y, "Amount")
        y -= 20

        for item in invoice.items:
            p.drawString(50, y, item.description[:40])
            p.drawString(300, y, f"{item.quantity}")
            p.drawString(400, y, f"{invoice.currency} {item.rate:.2f}")
            p.drawString(500, y, f"{invoice.currency} {item.amount:.2f}")
            y -= 20

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