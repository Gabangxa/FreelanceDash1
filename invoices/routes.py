from concurrent.futures import TimeoutError as FuturesTimeoutError
from flask import Blueprint, render_template, redirect, url_for, flash, request, make_response, jsonify, abort, send_file
from flask_login import login_required, current_user
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from app import db, logger
from models import Invoice, InvoiceItem, Client, Project, TimeEntry
from invoices.forms import InvoiceForm, InvoiceItemForm, TimeEntryToInvoiceForm
from invoices.pdf_generator import generate_invoice_pdf
from invoices import get_pdf_executor
from io import BytesIO
import uuid
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from errors import handle_db_errors, UserFriendlyError

# Per-render wall-clock budget. Anything slower than this and the user
# is better served by an immediate "try again in a moment" 503 than a
# spinning browser tab. Tune in tandem with the gunicorn ``--timeout``
# (currently 120s) -- this must stay well below it.
_PDF_RENDER_TIMEOUT_SECONDS = 30

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
# PDF branding helpers were moved to ``invoices/pdf_generator.py`` when
# the synchronous ReportLab render was off-loaded onto a thread pool.
# ---------------------------------------------------------------------------

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
        # Tenant ownership check stays on the request thread: we resolve
        # the invoice (and 404 cross-tenant) BEFORE submitting anything
        # to the executor. The worker thread has no current_user, so it
        # cannot make this authorization decision itself -- it just
        # trusts the id once we hand it over.
        invoice = (Invoice.query
                  .join(Client)
                  .filter(Invoice.id == id, Client.user_id == current_user.id)
                  .with_entities(Invoice.id, Invoice.invoice_number)
                  .first_or_404())
        invoice_id = invoice.id
        invoice_number = invoice.invoice_number
        user_id = current_user.id

        # Off-load the ReportLab render onto the module-level thread
        # pool so the gunicorn worker is free to serve the next request
        # while the (CPU-bound, GIL-releasing during image decode) PDF
        # is built. ``.result(timeout=...)`` blocks this thread but the
        # worker can be re-used as soon as the future resolves.
        #
        # Submission itself can fail (e.g. executor shutdown during a
        # graceful restart) so the submit + wait both live inside the
        # same error boundary -- any failure short of a fresh DB error
        # surfaces as a user-facing 503, never a stack-traced 500.
        future = None
        try:
            future = get_pdf_executor().submit(
                generate_invoice_pdf, invoice_id, user_id,
            )
            pdf_bytes = future.result(timeout=_PDF_RENDER_TIMEOUT_SECONDS)
        except FuturesTimeoutError:
            # Don't let the future keep running and steal a worker slot
            # forever -- mark it cancelled (best-effort: cancel only
            # works if it hasn't started; if it has, it will run to
            # completion in the background but we no longer care).
            if future is not None:
                future.cancel()
            logger.warning(
                "PDF render for invoice %s exceeded %ss budget (user=%s)",
                invoice_id, _PDF_RENDER_TIMEOUT_SECONDS, user_id,
            )
            return make_response(
                "PDF generation is taking longer than expected. "
                "Please try again in a moment.",
                503,
            )
        except LookupError:
            # Race: invoice was deleted (or ownership changed) between
            # the route's ownership check and the worker's re-check.
            # 404 mirrors the normal missing-row behaviour.
            abort(404)
        except Exception:
            logger.exception(
                "PDF render failed for invoice %s (user=%s)",
                invoice_id, user_id,
            )
            return make_response(
                "Could not generate the PDF right now. "
                "Please try again in a moment.",
                503,
            )

        logger.info(
            "PDF generated for invoice #%s by user %s (%d bytes)",
            invoice_number, user_id, len(pdf_bytes),
        )
        return send_file(
            BytesIO(pdf_bytes),
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f'invoice_{invoice_number}.pdf',
        )

    except SQLAlchemyError as e:
        logger.exception(f"Database error preparing PDF for invoice {id}")
        flash('Error generating PDF. Please try again.', 'danger')
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

        # Clear the Task #28 invoiced markers on any time entries that
        # were rolled into this invoice so they become invoiceable
        # again. Done explicitly (not via DB-level ON DELETE SET NULL)
        # because the bootstrap ALTER block has to stay portable
        # across SQLite/Postgres -- the cascade behaviour is not
        # guaranteed once the column is added at runtime. The
        # ``billable`` flag is left untouched: it represents the
        # user's "is this work chargeable?" intent, which is
        # independent of whether it was invoiced.
        TimeEntry.query.filter_by(invoice_id=invoice.id).update(
            {'invoice_id': None, 'invoiced_at': None},
            synchronize_session=False,
        )

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
    # Only entries with end_time set (no running timers), billable=True,
    # and not already rolled into an invoice (Task #28: ``invoiced_at``
    # stays NULL until the entry is invoiced; the v1 flow that flipped
    # ``billable`` to FALSE is no longer in use).
    entries = []
    if selected_project_id:
        entries = (
            TimeEntry.query
            .join(Project, TimeEntry.project_id == Project.id)
            .filter(
                TimeEntry.project_id == selected_project_id,
                Project.user_id == current_user.id,
                TimeEntry.billable.is_(True),
                TimeEntry.invoiced_at.is_(None),
                TimeEntry.end_time.isnot(None),
            )
            .order_by(TimeEntry.start_time.asc())
            .all()
        )
    form.entry_ids.choices = [(e.id, str(e.id)) for e in entries]

    # Pre-fill the hourly rate from the chosen project's saved default
    # (Task #27). Only on GET and only when the user hasn't typed
    # something into the field yet, so we never clobber an in-progress
    # edit. Projects without a saved default behave as before -- the
    # field stays empty and the user types a rate in.
    if (
        request.method == 'GET'
        and selected_project_id
        and not form.rate.data
    ):
        _project_for_rate = Project.query.filter_by(
            id=selected_project_id, user_id=current_user.id,
        ).first()
        if _project_for_rate and _project_for_rate.default_hourly_rate is not None:
            form.rate.data = _project_for_rate.default_hourly_rate

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
                TimeEntry.invoiced_at.is_(None),
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

            # Reject the whole submission if any selected entry has
            # zero/negative duration so the flash message and billed-flag
            # mutations always match the user's selection exactly (one
            # InvoiceItem per selected entry, no silent skips).
            zero_duration = [e for e in chosen_entries
                             if _format_minutes_as_hours(e.duration or 0) <= 0]
            if zero_duration:
                db.session.rollback()
                flash(
                    'One or more selected entries have zero duration. '
                    'Edit or deselect them and try again.',
                    'danger',
                )
                return render_template(
                    'from_time_entries.html', form=form, entries=entries,
                    selected_client_id=selected_client_id,
                    selected_project_id=selected_project_id,
                )

            total_amount = Decimal('0')
            total_hours = Decimal('0')
            for entry in chosen_entries:
                hours = _format_minutes_as_hours(entry.duration or 0)
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

                # Mark this entry as invoiced (Task #28). We set both
                # the timestamp and the FK to the new invoice so the
                # entry is hidden from future "From Time Entries"
                # picks and so deleting the invoice can clear the
                # marker. ``billable`` is intentionally left alone --
                # it tracks chargeability, not billed-status.
                entry.invoiced_at = datetime.utcnow()
                entry.invoice_id = invoice.id

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
