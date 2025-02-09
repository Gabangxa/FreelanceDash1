from flask import Blueprint, render_template, redirect, url_for, flash, request, make_response, jsonify
from flask_login import login_required, current_user
from app import db
from models import Invoice, InvoiceItem, Client, Project
from invoices.forms import InvoiceForm, InvoiceItemForm
from reportlab.pdfgen import canvas
from io import BytesIO
import uuid

invoices_bp = Blueprint('invoices', __name__, url_prefix='/invoices', template_folder='../templates/invoices')

@invoices_bp.route('/')
@login_required
def list_invoices():
    invoices = Invoice.query.join(Client).filter(Client.user_id == current_user.id).all()
    return render_template('list.html', invoices=invoices)

@invoices_bp.route('/new', methods=['GET', 'POST'])
@login_required
def create_invoice():
    form = InvoiceForm()

    # Get all clients for the current user
    clients = Client.query.filter_by(user_id=current_user.id).all()
    form.client_id.choices = [(c.id, c.name) for c in clients]

    # If client_id is provided, populate projects
    if request.method == 'POST' and form.client_id.data:
        projects = Project.query.filter_by(client_id=form.client_id.data).all()
        form.project_id.choices = [(p.id, p.name) for p in projects]

    if form.validate_on_submit():
        project = Project.query.get(form.project_id.data)
        if not project or project.client_id != form.client_id.data:
            flash('Invalid project selection')
            return redirect(url_for('invoices.create_invoice'))

        # Create invoice
        invoice = Invoice(
            invoice_number=f"INV-{uuid.uuid4().hex[:8].upper()}",
            amount=0,  # Will be calculated from items
            currency=form.currency.data,  # Add currency field
            due_date=form.due_date.data,
            notes=form.notes.data,
            client_id=form.client_id.data,
            project_id=form.project_id.data,
            status=form.status.data
        )
        db.session.add(invoice)

        # Add invoice items
        total_amount = 0
        for item_data in form.items.data:
            item = InvoiceItem(
                description=item_data['description'],
                quantity=item_data['quantity'],
                rate=item_data['rate'],
                amount=item_data['quantity'] * item_data['rate'],
                invoice=invoice
            )
            db.session.add(item)
            total_amount += item.amount

        invoice.amount = total_amount
        db.session.commit()
        flash('Invoice created successfully')
        return redirect(url_for('invoices.view_invoice', id=invoice.id))

    return render_template('create.html', form=form)

@invoices_bp.route('/<int:id>', methods=['GET', 'POST'])
@login_required
def view_invoice(id):
    invoice = Invoice.query.join(Client).filter(
        Invoice.id == id,
        Client.user_id == current_user.id
    ).first_or_404()

    if request.method == 'POST':
        new_status = request.form.get('status')
        if new_status in ['draft', 'pending', 'paid', 'cancelled']:
            invoice.status = new_status
            db.session.commit()
            flash('Invoice status updated successfully')
            return redirect(url_for('invoices.view_invoice', id=id))

    return render_template('detail.html', invoice=invoice)

@invoices_bp.route('/get-projects/<int:client_id>')
@login_required
def get_projects(client_id):
    projects = Project.query.filter_by(client_id=client_id).all()
    return jsonify([(p.id, p.name) for p in projects])

@invoices_bp.route('/<int:id>/pdf')
@login_required
def generate_pdf(id):
    invoice = Invoice.query.join(Client).filter(
        Invoice.id == id,
        Client.user_id == current_user.id
    ).first_or_404()

    buffer = BytesIO()
    p = canvas.Canvas(buffer)

    # Add invoice details
    p.drawString(50, 800, f"Invoice #{invoice.invoice_number}")
    p.drawString(50, 780, f"Status: {invoice.status.upper()}")
    p.drawString(50, 760, f"Due Date: {invoice.due_date.strftime('%Y-%m-%d')}")
    p.drawString(50, 740, f"Total Amount: ${invoice.amount:.2f}")

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
        p.drawString(400, y, f"${item.rate:.2f}")
        p.drawString(500, y, f"${item.amount:.2f}")
        y -= 20

    p.showPage()
    p.save()

    buffer.seek(0)
    response = make_response(buffer.getvalue())
    response.mimetype = 'application/pdf'
    response.headers['Content-Disposition'] = f'attachment; filename=invoice_{invoice.invoice_number}.pdf'

    return response

@invoices_bp.route('/<int:id>/delete', methods=['POST'])
@login_required
def delete_invoice(id):
    invoice = Invoice.query.join(Client).filter(
        Invoice.id == id,
        Client.user_id == current_user.id
    ).first_or_404()

    db.session.delete(invoice)
    db.session.commit()
    flash('Invoice deleted successfully')
    return redirect(url_for('invoices.list_invoices'))