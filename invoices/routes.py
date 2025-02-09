from flask import Blueprint, render_template, redirect, url_for, flash, request, make_response, jsonify
from flask_login import login_required, current_user
from app import db
from models import Invoice, InvoiceItem, Client, Project
from invoices.forms import InvoiceForm, InvoiceItemForm
from reportlab.pdfgen import canvas
from io import BytesIO
import uuid

invoices_bp = Blueprint('invoices', __name__, url_prefix='/invoices')

@invoices_bp.route('/')
@login_required
def list_invoices():
    invoices = Invoice.query.join(Client).filter(Client.user_id == current_user.id).all()
    return render_template('invoices/list.html', invoices=invoices)

@invoices_bp.route('/new', methods=['GET', 'POST'])
@login_required
def create_invoice():
    form = InvoiceForm()
    # Get all clients for the current user
    form.client_id.choices = [(c.id, c.name) for c in Client.query.filter_by(user_id=current_user.id)]

    # Get client_id from either form data (POST) or query parameters (GET)
    client_id = request.form.get('client_id', type=int) or request.args.get('client_id', type=int)

    # If we have a client_id, populate the projects dropdown
    if client_id:
        projects = Project.query.filter_by(client_id=client_id).all()
        form.project_id.choices = [(p.id, p.name) for p in projects]
    else:
        form.project_id.choices = []

    if form.validate_on_submit():
        invoice = Invoice(
            invoice_number=f"INV-{uuid.uuid4().hex[:8].upper()}",
            amount=form.amount.data,
            due_date=form.due_date.data,
            notes=form.notes.data,
            client_id=form.client_id.data,
            project_id=form.project_id.data,
            status='draft'
        )
        db.session.add(invoice)
        db.session.commit()
        flash('Invoice created successfully')
        return redirect(url_for('invoices.view_invoice', id=invoice.id))

    return render_template('invoices/create.html', form=form)

@invoices_bp.route('/get-projects/<int:client_id>')
@login_required
def get_projects(client_id):
    projects = Project.query.filter_by(client_id=client_id).all()
    return jsonify([(p.id, p.name) for p in projects])

@invoices_bp.route('/<int:id>')
@login_required
def view_invoice(id):
    invoice = Invoice.query.join(Client).filter(
        Invoice.id == id,
        Client.user_id == current_user.id
    ).first_or_404()
    return render_template('invoices/detail.html', invoice=invoice)

@invoices_bp.route('/<int:id>/pdf')
@login_required
def generate_pdf(id):
    invoice = Invoice.query.join(Client).filter(
        Invoice.id == id,
        Client.user_id == current_user.id
    ).first_or_404()

    # Create PDF using ReportLab
    buffer = BytesIO()
    p = canvas.Canvas(buffer)

    # Add invoice details
    p.drawString(50, 800, f"Invoice #{invoice.invoice_number}")
    p.drawString(50, 780, f"Due Date: {invoice.due_date.strftime('%Y-%m-%d')}")
    p.drawString(50, 760, f"Amount: ${invoice.amount:.2f}")

    # Add client details
    p.drawString(50, 740, f"Client: {invoice.client.name}")
    p.drawString(50, 720, f"Email: {invoice.client.email}")

    # Save PDF
    p.showPage()
    p.save()

    buffer.seek(0)
    response = make_response(buffer.getvalue())
    response.mimetype = 'application/pdf'
    response.headers['Content-Disposition'] = f'attachment; filename=invoice_{invoice.invoice_number}.pdf'

    return response