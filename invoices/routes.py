from flask import Blueprint, render_template, redirect, url_for, flash, request, make_response, jsonify, abort
from flask_login import login_required, current_user
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from app import db, logger
from models import Invoice, InvoiceItem, Client, Project, TimeEntry
from invoices.forms import InvoiceForm, InvoiceItemForm, TimeEntryToInvoiceForm
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from io import BytesIO
import uuid
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from errors import handle_db_errors, UserFriendlyError

# 2-decimal money quantum used to round invoice totals consistently.
_MONEY_QUANTUM = Decimal('0.01')


def _to_money(value) -> Decimal:
    """Coerce a Decimal-or-numeric ``value`` to 2dp money rounding half-up.

    The form already hands us ``Decimal`` (DecimalField), but we still
    quantize after multiplication so 1.005 * 1 doesn't render as 1.005
    in the templates / PDF / DB.
    """
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)

invoices_bp = Blueprint('invoices', __name__, url_prefix='/invoices', template_folder='../templates/invoices')


# ---------------------------------------------------------------------------
# PDF branding helpers
# ---------------------------------------------------------------------------
# ReportLab built-in font triples: (regular, bold, italic). Keyed by the
# value persisted in UserSettings.invoice_font. No font file shipping is
# required for any of these — they're embedded in ReportLab itself.
_FONT_MAP = {
    'helvetica': ('Helvetica',   'Helvetica-Bold', 'Helvetica-Oblique'),
    'times':     ('Times-Roman', 'Times-Bold',     'Times-Italic'),
    'courier':   ('Courier',     'Courier-Bold',   'Courier-Oblique'),
}


def _hex_to_rgb(hex_str, default):
    """Convert ``#RGB`` / ``#RRGGBB`` to a 0..1 RGB tuple. Falls back to
    ``default`` (also a 0..1 tuple) on any malformed input — matches the
    same defensive posture as the ``safe_color`` Jinja filter so a bad
    DB value can't crash the PDF generator."""
    try:
        h = (hex_str or '').lstrip('#').strip()
        if len(h) == 3:
            h = ''.join(c * 2 for c in h)
        if len(h) != 6:
            return default
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except (ValueError, TypeError):
        return default


def _draw_image_box(p, image_bytes, x, y, max_w, max_h):
    """Draw an image inside an (max_w x max_h) box anchored at the
    bottom-left corner ``(x, y)``, preserving aspect ratio so non-square
    logos / signatures don't get squished."""
    if not image_bytes:
        return
    try:
        img = ImageReader(BytesIO(image_bytes))
        iw, ih = img.getSize()
        if iw <= 0 or ih <= 0:
            return
        scale = min(max_w / float(iw), max_h / float(ih))
        w = iw * scale
        h = ih * scale
        p.drawImage(img, x, y, width=w, height=h, mask='auto', preserveAspectRatio=True)
    except (OSError, ValueError):
        logger.exception("Error adding image to PDF")

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
        logger.exception("Database error in list_invoices")
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

        # If client_id is provided, populate projects. Belt-and-suspenders:
        # filter by both client_id (chosen in the form) AND user_id, so a
        # tampered client_id can never surface another tenant's projects
        # in the dropdown.
        if request.method == 'POST' and form.client_id.data:
            projects = Project.query.filter_by(
                client_id=form.client_id.data,
                user_id=current_user.id,
            ).all()
            form.project_id.choices = [(p.id, p.name) for p in projects]

        if form.validate_on_submit():
            # Verify project belongs to selected client AND to the current
            # user. Going through ``user_id`` directly means we can't be
            # tricked by a forged ``client_id`` that points at our own
            # client but a project owned by a different tenant.
            project = Project.query.filter_by(
                id=form.project_id.data,
                user_id=current_user.id,
            ).first()
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
                    amount=Decimal('0'),  # Will be recalculated from items
                    currency=form.currency.data,
                    due_date=form.due_date.data,
                    notes=form.notes.data,
                    client_id=form.client_id.data,
                    project_id=form.project_id.data,
                    status=form.status.data
                )
                db.session.add(invoice)
                db.session.flush()  # Get the invoice.id without committing

                # Add invoice items. All math is in Decimal -- we never
                # cast to float, which would re-introduce the binary-
                # rounding drift the column type change is meant to fix.
                total_amount = Decimal('0')
                items_added = False

                for item_data in form.items.data:
                    # Validate item data (Decimal comparison, no coercion)
                    if (
                        not item_data['description']
                        or item_data['quantity'] is None
                        or item_data['rate'] is None
                        or item_data['quantity'] <= 0
                        or item_data['rate'] <= 0
                    ):
                        continue

                    quantity = item_data['quantity']
                    rate = item_data['rate']
                    item_amount = _to_money(quantity * rate)

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

                # Update invoice total (already-quantized item amounts so
                # the sum is exact; quantize once more just in case the
                # column adapter is strict about scale).
                invoice.amount = _to_money(total_amount)
                db.session.commit()
                logger.info(f"Invoice #{invoice.invoice_number} created by user {current_user.id}")
                flash('Invoice created successfully', 'success')
                return redirect(url_for('invoices.view_invoice', id=invoice.id))

            except IntegrityError as e:
                db.session.rollback()
                logger.exception("Integrity error creating invoice")
                flash('Error creating invoice: Duplicate invoice number. Please try again.', 'danger')
            except SQLAlchemyError as e:
                db.session.rollback()
                logger.exception("Database error creating invoice")
                flash('Error creating invoice. Please try again.', 'danger')

        return render_template('create.html', form=form)

    except (SQLAlchemyError, ValueError, KeyError) as e:
        db.session.rollback()
        logger.exception("Unexpected error in create_invoice")
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
                    logger.exception("Error updating invoice status")
                    flash('Error updating status. Please try again.', 'danger')

            return redirect(url_for('invoices.view_invoice', id=id))

        return render_template('detail.html', invoice=invoice)

    except SQLAlchemyError as e:
        logger.exception(f"Database error viewing invoice {id}")
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

        # Defense in depth: filter on user_id too even though the parent
        # ``client`` row was already proven to belong to ``current_user``.
        # That way any future refactor that drops the ``client`` lookup
        # above doesn't silently leak another tenant's project list.
        projects = Project.query.filter_by(
            client_id=client_id,
            user_id=current_user.id,
        ).all()
        return jsonify([(p.id, p.name) for p in projects])
    except SQLAlchemyError as e:
        logger.exception(f"Error fetching projects for client {client_id}")
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

        # ---- Resolve branding tokens (font, colors, template) -------------
        primary_rgb   = _hex_to_rgb(settings.invoice_color_primary,   (0.114, 0.114, 0.122))   # #1d1d1f
        secondary_rgb = _hex_to_rgb(settings.invoice_color_secondary, (0.97,  0.97,  0.97))    # #f7f7f7
        font_key = (settings.invoice_font or 'helvetica').lower()
        font_regular, font_bold, font_italic = _FONT_MAP.get(font_key, _FONT_MAP['helvetica'])
        template_name = settings.invoice_template or 'default'

        # ---- Header (template-specific) -----------------------------------
        if template_name == 'modern':
            # Solid primary header band, white text on top
            p.setFillColorRGB(*primary_rgb)
            p.rect(0, height - 100, width, 100, fill=1, stroke=0)
            _draw_image_box(p, settings.invoice_logo, 50, height - 90, 120, 70)
            p.setFillColorRGB(1, 1, 1)
            p.setFont(font_bold, 16)
            p.drawRightString(width - 50, height - 55, f"INVOICE #{invoice.invoice_number}")
            p.setFillColorRGB(0.114, 0.114, 0.122)
            p.setFont(font_regular, 10)
            p.drawString(50, height - 120, f"Date: {invoice.created_at.strftime('%Y-%m-%d')}")
            p.drawString(50, height - 135, f"Due Date: {invoice.due_date.strftime('%Y-%m-%d')}")
            p.setFillColorRGB(*primary_rgb)
            p.drawString(width - 200, height - 120, f"Status: {invoice.status.upper()}")
            p.setFillColorRGB(0, 0, 0)

        elif template_name == 'classic':
            p.setFillColorRGB(*primary_rgb)
            p.setFont(font_bold, 24)
            p.drawString(50, height - 50, "INVOICE")
            _draw_image_box(p, settings.invoice_logo, width - 170, height - 80, 120, 70)
            # Double rule under the title
            p.setStrokeColorRGB(*primary_rgb)
            p.line(50, height - 80, width - 50, height - 80)
            p.line(50, height - 82, width - 50, height - 82)
            p.setFillColorRGB(0, 0, 0)
            p.setFont(font_regular, 12)
            p.drawString(50, height - 100, f"Invoice Number: {invoice.invoice_number}")
            p.drawString(50, height - 115, f"Date: {invoice.created_at.strftime('%B %d, %Y')}")
            p.drawString(50, height - 130, f"Due Date: {invoice.due_date.strftime('%B %d, %Y')}")
            p.drawString(50, height - 145, f"Status: {invoice.status.capitalize()}")

        elif template_name == 'creative':
            # Bold full-bleed primary header
            p.setFillColorRGB(*primary_rgb)
            p.rect(0, height - 120, width, 120, fill=1, stroke=0)
            _draw_image_box(p, settings.invoice_logo, 50, height - 105, 120, 80)
            p.setFillColorRGB(1, 1, 1)
            p.setFont(font_bold, 28)
            p.drawCentredString(width / 2, height - 70, "INVOICE")
            p.setFont(font_bold, 14)
            p.drawCentredString(width / 2, height - 100, f"#{invoice.invoice_number}")
            # Diagonal status banner (always dark for legibility on any primary)
            p.saveState()
            p.translate(width - 80, height - 40)
            p.rotate(45)
            p.setFillColorRGB(0.1, 0.1, 0.1)
            p.rect(-20, -10, 100, 20, fill=1)
            p.setFillColorRGB(1, 1, 1)
            p.setFont(font_bold, 10)
            p.drawCentredString(30, 0, invoice.status.upper())
            p.restoreState()

        else:
            # Default - professional
            p.setFillColorRGB(*primary_rgb)
            p.setFont(font_bold, 18)
            p.drawString(50, height - 50, "INVOICE")
            _draw_image_box(p, settings.invoice_logo, width - 170, height - 80, 120, 70)
            p.setFillColorRGB(0, 0, 0)
            p.setFont(font_regular, 10)
            p.drawString(50, height - 70, f"Invoice Number: {invoice.invoice_number}")
            p.drawString(50, height - 85, f"Date: {invoice.created_at.strftime('%Y-%m-%d')}")
            p.drawString(50, height - 100, f"Due Date: {invoice.due_date.strftime('%Y-%m-%d')}")
            # Status keeps its semantic color (green/orange/red/gray) so users
            # can still glance and tell paid/pending/cancelled apart at a glance.
            status_palette = {
                'paid':      (0, 0.6, 0.2),
                'pending':   (0.9, 0.55, 0),
                'cancelled': (0.85, 0.1, 0.1),
            }
            p.setFillColorRGB(*status_palette.get(invoice.status, (0.4, 0.4, 0.4)))
            p.drawString(50, height - 115, f"Status: {invoice.status.upper()}")
            p.setFillColorRGB(0, 0, 0)

        # ---- FROM / TO blocks ---------------------------------------------
        section_y = height - 200
        p.setFillColorRGB(*primary_rgb)
        p.setFont(font_bold, 12)
        p.drawString(50, section_y, "FROM:")
        p.drawString(300, section_y, "TO:")
        p.setFillColorRGB(0, 0, 0)
        p.setFont(font_regular, 10)

        from_y = section_y - 15
        if settings.company_name:
            p.drawString(50, from_y, settings.company_name); from_y -= 15
        if settings.company_address:
            for line in settings.company_address.split('\n')[:3]:
                p.drawString(50, from_y, line.strip()); from_y -= 15
        if settings.company_email:
            p.drawString(50, from_y, settings.company_email); from_y -= 15
        if settings.company_phone:
            p.drawString(50, from_y, settings.company_phone); from_y -= 15

        to_y = section_y - 15
        p.drawString(300, to_y, invoice.client.name); to_y -= 15
        if invoice.client.company:
            p.drawString(300, to_y, invoice.client.company); to_y -= 15
        if invoice.client.email:
            p.drawString(300, to_y, invoice.client.email); to_y -= 15
        if invoice.client.address:
            for line in invoice.client.address.split('\n')[:3]:
                p.drawString(300, to_y, line.strip()); to_y -= 15

        # ---- Project ------------------------------------------------------
        y = min(from_y, to_y) - 10
        if invoice.project:
            p.setFont(font_bold, 11)
            p.drawString(50, y, f"Project: {invoice.project.name}")
            y -= 20

        # ---- Line items: header bar in primary, alternating rows in
        #      secondary so both color choices show up on every template.
        p.setFillColorRGB(*primary_rgb)
        p.rect(50, y - 5, width - 100, 22, fill=1, stroke=0)
        p.setFillColorRGB(1, 1, 1)
        p.setFont(font_bold, 11)
        p.drawString(60, y + 3, "DESCRIPTION")
        p.drawString(350, y + 3, "QUANTITY")
        p.drawString(420, y + 3, "RATE")
        p.drawString(500, y + 3, "AMOUNT")
        p.setFillColorRGB(0, 0, 0)
        y -= 25

        p.setFont(font_regular, 10)
        for idx, item in enumerate(invoice.items):
            if idx % 2 == 0:
                p.setFillColorRGB(*secondary_rgb)
                p.rect(50, y - 5, width - 100, 20, fill=1, stroke=0)
                p.setFillColorRGB(0, 0, 0)

            description = item.description if len(item.description) <= 45 else item.description[:42] + "..."
            p.drawString(60, y, description)
            p.drawString(350, y, f"{item.quantity}")
            p.drawString(420, y, f"{invoice.currency} {item.rate:.2f}")
            p.drawString(500, y, f"{invoice.currency} {item.amount:.2f}")
            y -= 20

            # Page break check; leave room for total + signature + footer
            if y < 160:
                p.showPage()
                p.setFont(font_regular, 10)
                y = height - 50
                p.drawString(50, y, "INVOICE CONTINUED")
                y -= 30

        # ---- Total --------------------------------------------------------
        y -= 10
        p.setStrokeColorRGB(*primary_rgb)
        p.setLineWidth(1.5)
        p.line(350, y + 5, width - 50, y + 5)
        p.setLineWidth(1)
        p.setFillColorRGB(*primary_rgb)
        p.setFont(font_bold, 12)
        p.drawString(420, y - 15, "TOTAL:")
        p.drawString(500, y - 15, f"{invoice.currency} {invoice.amount:.2f}")
        p.setFillColorRGB(0, 0, 0)

        # ---- Notes --------------------------------------------------------
        if invoice.notes:
            y -= 50
            p.setFillColorRGB(*primary_rgb)
            p.setFont(font_bold, 11)
            p.drawString(50, y, "NOTES:")
            p.setFillColorRGB(0, 0, 0)
            p.setFont(font_regular, 10)

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
            for line in notes_lines[:5]:
                p.drawString(50, y, line); y -= 15

        # ---- Signature + footer ------------------------------------------
        # Both anchor to fixed bottom positions; if the running ``y`` from
        # notes/total has crept down into that band, push to a fresh page
        # first so the signature/footer don't collide with the text above.
        # Reserve: signature block (~110 incl. caption) + footer (~50) +
        # safety buffer (20) when both present; otherwise just the footer.
        needed_bottom = (110 if settings.invoice_signature else 0) \
                        + (50 if settings.invoice_footer_text else 50) \
                        + 20
        if y < needed_bottom:
            p.showPage()

        if settings.invoice_signature:
            sig_y = 80
            _draw_image_box(p, settings.invoice_signature, 50, sig_y, 160, 50)
            p.setStrokeColorRGB(0.5, 0.5, 0.5)
            p.line(50, sig_y - 4, 210, sig_y - 4)
            p.setFont(font_italic, 9)
            p.setFillColorRGB(0.4, 0.4, 0.4)
            p.drawString(50, sig_y - 16, "Authorised signature")
            p.setFillColorRGB(0, 0, 0)

        # Footer text from settings (always at the bottom of the final page)
        if settings.invoice_footer_text:
            p.setFont(font_regular, 9)
            p.setFillColorRGB(0.5, 0.5, 0.5)
            footer_y = 40
            for line in settings.invoice_footer_text.split('\n')[:3]:
                p.drawCentredString(width / 2, footer_y, line.strip())
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
        logger.exception(f"Error generating PDF for invoice {id}")
        flash('Error generating PDF. Please try again.', 'danger')
        return redirect(url_for('invoices.view_invoice', id=id))
    except (ValueError, OSError, KeyError, TypeError) as e:
        logger.exception("Unexpected error generating PDF")
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
        logger.exception(f"Error deleting invoice {id}")
        flash('Error deleting invoice. Please try again.', 'danger')

    return redirect(url_for('invoices.list_invoices'))

# ---------------------------------------------------------------------------
# Invoice from time entries
# ---------------------------------------------------------------------------
def _format_minutes_as_hours(minutes: int) -> Decimal:
    """Convert duration in minutes to a Decimal hours value rounded to 2dp."""
    if not minutes:
        return Decimal('0.00')
    hours = (Decimal(int(minutes)) / Decimal(60)).quantize(
        _MONEY_QUANTUM, rounding=ROUND_HALF_UP,
    )
    return hours


def _time_to_invoice_enabled() -> bool:
    """Return True if the current user has the feature flag enabled."""
    settings = current_user.get_or_create_settings()
    return False if settings.time_to_invoice_enabled is False else True


@invoices_bp.route('/from-time-entries', methods=['GET', 'POST'])
@login_required
@handle_db_errors
def invoice_from_time_entries():
    # Hide feature behind 404 when toggled off so its existence isn't
    # leaked. Users can re-enable it from Settings -> Invoice Templates.
    if not _time_to_invoice_enabled():
        abort(404)

    form = TimeEntryToInvoiceForm()

    # Always populate the client choices so validation works on POST too.
    clients = Client.query.filter_by(user_id=current_user.id).order_by(Client.name).all()
    form.client_id.choices = [(0, '— Select a client —')] + [(c.id, c.name) for c in clients]

    # Resolve the chosen client/project (form data on POST, query string on GET).
    selected_client_id = form.client_id.data or request.args.get('client_id', type=int) or 0
    selected_project_id = form.project_id.data or request.args.get('project_id', type=int) or 0

    project_choices = [(0, '— Select a project —')]
    if selected_client_id:
        # Tenant-scope: filter by user_id even though the client lookup
        # already proves ownership, matching the rest of this blueprint.
        projects = Project.query.filter_by(
            client_id=selected_client_id,
            user_id=current_user.id,
        ).order_by(Project.name).all()
        project_choices += [(p.id, p.name) for p in projects]
    form.project_id.choices = project_choices

    # Load the unbilled, completed time entries for the chosen project.
    # Only entries with end_time set (no running timers) and billable=True.
    entries = []
    if selected_project_id:
        entries = (
            TimeEntry.query
            .join(Project, TimeEntry.project_id == Project.id)
            .filter(
                TimeEntry.project_id == selected_project_id,
                Project.user_id == current_user.id,
                TimeEntry.billable.is_(True),
                TimeEntry.end_time.isnot(None),
            )
            .order_by(TimeEntry.start_time.asc())
            .all()
        )
    form.entry_ids.choices = [(e.id, str(e.id)) for e in entries]

    if request.method == 'POST' and form.validate_on_submit():
        # Re-fetch entries inside a tenant-scoped query so a tampered
        # entry_ids list can't pull rows from other projects/users.
        chosen_ids = set(form.entry_ids.data or [])
        if not chosen_ids:
            flash('Select at least one time entry to invoice.', 'danger')
            return render_template(
                'from_time_entries.html', form=form, entries=entries,
                selected_client_id=selected_client_id,
                selected_project_id=selected_project_id,
            )

        chosen_entries = (
            TimeEntry.query
            .join(Project, TimeEntry.project_id == Project.id)
            .filter(
                TimeEntry.id.in_(chosen_ids),
                TimeEntry.project_id == form.project_id.data,
                Project.user_id == current_user.id,
                TimeEntry.billable.is_(True),
                TimeEntry.end_time.isnot(None),
            )
            .all()
        )
        if len(chosen_entries) != len(chosen_ids):
            flash(
                'Some selected entries are no longer eligible. Please reload and try again.',
                'warning',
            )
            return render_template(
                'from_time_entries.html', form=form, entries=entries,
                selected_client_id=selected_client_id,
                selected_project_id=selected_project_id,
            )

        # Verify the selected project really belongs to the chosen client.
        project = Project.query.filter_by(
            id=form.project_id.data, user_id=current_user.id,
        ).first()
        if not project or project.client_id != form.client_id.data:
            flash('Invalid project selection.', 'danger')
            return redirect(url_for('invoices.invoice_from_time_entries'))

        rate = form.rate.data
        if rate is None or rate <= 0:
            flash('Hourly rate must be greater than zero.', 'danger')
            return render_template(
                'from_time_entries.html', form=form, entries=entries,
                selected_client_id=selected_client_id,
                selected_project_id=selected_project_id,
            )

        try:
            invoice_number = f"INV-{uuid.uuid4().hex[:8].upper()}"
            if Invoice.query.filter_by(invoice_number=invoice_number).first():
                invoice_number = f"INV-{uuid.uuid4().hex[:8].upper()}"

            invoice = Invoice(
                invoice_number=invoice_number,
                amount=Decimal('0'),
                currency=form.currency.data,
                due_date=form.due_date.data,
                notes=form.notes.data,
                client_id=form.client_id.data,
                project_id=form.project_id.data,
                status=form.status.data,
            )
            db.session.add(invoice)
            db.session.flush()

            total_amount = Decimal('0')
            total_hours = Decimal('0')
            for entry in chosen_entries:
                hours = _format_minutes_as_hours(entry.duration or 0)
                if hours <= 0:
                    # Skip zero-duration entries silently; they'd produce
                    # noise items with $0.00 amounts.
                    continue
                amount = _to_money(hours * rate)
                description = (entry.description or '').strip()
                if not description:
                    description = f"Work on {entry.start_time.strftime('%Y-%m-%d')}"
                # Truncate to the InvoiceItem column safety: description is
                # Text so no hard cap, but keep it sane.
                description = description[:500]

                db.session.add(InvoiceItem(
                    description=description,
                    quantity=hours,
                    rate=rate,
                    amount=amount,
                    invoice_id=invoice.id,
                ))
                total_amount += amount
                total_hours += hours

                # Flip the existing billable flag to mark this entry as
                # already invoiced. See task plan for why we reuse this
                # column instead of adding a new one.
                entry.billable = False

            if total_hours <= 0:
                db.session.rollback()
                flash('Selected entries have zero billable duration.', 'danger')
                return render_template(
                    'from_time_entries.html', form=form, entries=entries,
                    selected_client_id=selected_client_id,
                    selected_project_id=selected_project_id,
                )

            invoice.amount = _to_money(total_amount)
            db.session.commit()
            logger.info(
                "Invoice %s created from %s time entries (%s hours) by user %s",
                invoice.invoice_number, len(chosen_entries), total_hours, current_user.id,
            )
            flash(
                f"Created invoice from {total_hours} hours across "
                f"{len(chosen_entries)} time entries.",
                'success',
            )
            return redirect(url_for('invoices.view_invoice', id=invoice.id))

        except IntegrityError:
            db.session.rollback()
            logger.exception("Integrity error creating invoice from time entries")
            flash('Error creating invoice: duplicate invoice number. Please try again.', 'danger')
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Database error creating invoice from time entries")
            flash('Error creating invoice. Please try again.', 'danger')

    # GET (or invalid POST) — render the form.
    return render_template(
        'from_time_entries.html', form=form, entries=entries,
        selected_client_id=selected_client_id,
        selected_project_id=selected_project_id,
    )
