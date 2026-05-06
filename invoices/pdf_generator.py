"""Standalone, thread-safe invoice PDF generator.

Extracted from ``invoices/routes.py`` so the synchronous ReportLab render
can be off-loaded onto a ``ThreadPoolExecutor`` instead of blocking the
gunicorn request thread for the full duration of the render (which on a
many-line-item invoice with logos and signatures can run several seconds).

The PDF layout, fonts, colors, page-break logic, and ReportLab calls are
**unchanged** -- only the location where they execute has moved. Any
visual regression here means something was copied incorrectly during the
extraction, not a deliberate redesign.

Threading contract:

* The function is called from a worker thread, **not** the Flask request
  thread. It therefore cannot touch ``current_user``, ``request``, or
  any other request-bound globals.
* It opens its own ``app.app_context()`` so SQLAlchemy's scoped session
  binds to this thread's local instead of leaking the request thread's
  session across threads (which would risk session corruption /
  ``InvalidRequestError`` under concurrent renders).
* Tenant ownership is enforced **defence-in-depth**: the route layer
  is the primary check (it 404s cross-tenant before submitting), and
  this function re-checks ``Invoice.client.user_id == user_id`` when
  it fetches the row. That eliminates a small TOCTOU window where
  ownership could change (or a buggy caller could forget to check)
  between the route's authorization and the worker's render.
"""
from __future__ import annotations

from decimal import Decimal
from io import BytesIO

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader


# ReportLab built-in font triples: (regular, bold, italic). Keyed by the
# value persisted in UserSettings.invoice_font. No font file shipping is
# required for any of these -- they're embedded in ReportLab itself.
_FONT_MAP = {
    'helvetica': ('Helvetica',   'Helvetica-Bold', 'Helvetica-Oblique'),
    'times':     ('Times-Roman', 'Times-Bold',     'Times-Italic'),
    'courier':   ('Courier',     'Courier-Bold',   'Courier-Oblique'),
}


def _hex_to_rgb(hex_str, default):
    """Convert ``#RGB`` / ``#RRGGBB`` to a 0..1 RGB tuple. Falls back to
    ``default`` (also a 0..1 tuple) on any malformed input."""
    try:
        h = (hex_str or '').lstrip('#').strip()
        if len(h) == 3:
            h = ''.join(c * 2 for c in h)
        if len(h) != 6:
            return default
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except (ValueError, TypeError):
        return default


def _draw_image_box(p, image_bytes, x, y, max_w, max_h, logger):
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
        p.drawImage(img, x, y, width=w, height=h, mask='auto',
                    preserveAspectRatio=True)
    except (OSError, ValueError):
        logger.exception("Error adding image to PDF")


def generate_invoice_pdf(invoice_id: int, user_id: int) -> bytes:
    """Render invoice #``invoice_id`` (owned by user ``user_id``) to
    PDF bytes.

    Runs entirely inside its own Flask app context, so it is safe to
    invoke from a worker thread. Returns the raw PDF byte string ready
    to hand to ``send_file`` / ``make_response``.

    Raises ``LookupError`` if the invoice id doesn't exist **or** is
    not owned by ``user_id``. The route layer should already have
    verified ownership; the worker re-checks defensively to close the
    TOCTOU window between the request-thread check and the worker
    fetch (and to make the function safely callable in isolation, e.g.
    from a future background-renderer cron).
    """
    # Imports kept inside the function so this module is import-safe
    # even before ``app`` has finished initialising (matters for the
    # module-level executor wiring in ``invoices/__init__.py``).
    from app import app, db, logger
    from models import Invoice, Client

    with app.app_context():
        invoice = (
            Invoice.query
            .join(Client)
            .options(
                db.joinedload(Invoice.client),
                db.joinedload(Invoice.items),
                db.joinedload(Invoice.project),
            )
            .filter(Invoice.id == invoice_id, Client.user_id == user_id)
            .first()
        )
        if invoice is None:
            raise LookupError(
                f"Invoice {invoice_id} not found or not owned by user {user_id}"
            )

        # Settings row hangs off the verified owner. We just enforced
        # ``Client.user_id == user_id`` in the query above, so this is
        # guaranteed to be the right user's branding -- no risk of
        # leaking another tenant's logo/colors into the PDF.
        owner = invoice.client.user
        settings = owner.get_or_create_settings()

        buffer = BytesIO()
        p = canvas.Canvas(buffer, pagesize=letter)
        width, height = letter

        # ---- Resolve branding tokens (font, colors, template) ---------
        primary_rgb = _hex_to_rgb(
            settings.invoice_color_primary, (0.114, 0.114, 0.122)
        )  # #1d1d1f
        secondary_rgb = _hex_to_rgb(
            settings.invoice_color_secondary, (0.97, 0.97, 0.97)
        )  # #f7f7f7
        font_key = (settings.invoice_font or 'helvetica').lower()
        font_regular, font_bold, font_italic = _FONT_MAP.get(
            font_key, _FONT_MAP['helvetica']
        )
        template_name = settings.invoice_template or 'default'

        # ---- Header (template-specific) -------------------------------
        if template_name == 'modern':
            p.setFillColorRGB(*primary_rgb)
            p.rect(0, height - 100, width, 100, fill=1, stroke=0)
            _draw_image_box(p, settings.invoice_logo, 50, height - 90, 120, 70, logger)
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
            _draw_image_box(p, settings.invoice_logo, width - 170, height - 80, 120, 70, logger)
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
            p.setFillColorRGB(*primary_rgb)
            p.rect(0, height - 120, width, 120, fill=1, stroke=0)
            _draw_image_box(p, settings.invoice_logo, 50, height - 105, 120, 80, logger)
            p.setFillColorRGB(1, 1, 1)
            p.setFont(font_bold, 28)
            p.drawCentredString(width / 2, height - 70, "INVOICE")
            p.setFont(font_bold, 14)
            p.drawCentredString(width / 2, height - 100, f"#{invoice.invoice_number}")
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
            _draw_image_box(p, settings.invoice_logo, width - 170, height - 80, 120, 70, logger)
            p.setFillColorRGB(0, 0, 0)
            p.setFont(font_regular, 10)
            p.drawString(50, height - 70, f"Invoice Number: {invoice.invoice_number}")
            p.drawString(50, height - 85, f"Date: {invoice.created_at.strftime('%Y-%m-%d')}")
            p.drawString(50, height - 100, f"Due Date: {invoice.due_date.strftime('%Y-%m-%d')}")
            status_palette = {
                'paid':      (0, 0.6, 0.2),
                'pending':   (0.9, 0.55, 0),
                'cancelled': (0.85, 0.1, 0.1),
            }
            p.setFillColorRGB(*status_palette.get(invoice.status, (0.4, 0.4, 0.4)))
            p.drawString(50, height - 115, f"Status: {invoice.status.upper()}")
            p.setFillColorRGB(0, 0, 0)

        # ---- FROM / TO blocks ----------------------------------------
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

        # ---- Project --------------------------------------------------
        y = min(from_y, to_y) - 10
        if invoice.project:
            p.setFont(font_bold, 11)
            p.drawString(50, y, f"Project: {invoice.project.name}")
            y -= 20

        # ---- Line items ----------------------------------------------
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

            description = item.description if len(item.description) <= 45 \
                else item.description[:42] + "..."
            p.drawString(60, y, description)
            p.drawString(350, y, f"{item.quantity}")
            p.drawString(420, y, f"{invoice.currency} {item.rate:.2f}")
            p.drawString(500, y, f"{invoice.currency} {item.amount:.2f}")
            y -= 20

            if y < 160:
                p.showPage()
                p.setFont(font_regular, 10)
                y = height - 50
                p.drawString(50, y, "INVOICE CONTINUED")
                y -= 30

        # ---- Total ----------------------------------------------------
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

        # ---- Notes ----------------------------------------------------
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

        # ---- Signature + footer --------------------------------------
        needed_bottom = (110 if settings.invoice_signature else 0) \
                        + (50 if settings.invoice_footer_text else 50) \
                        + 20
        if y < needed_bottom:
            p.showPage()

        if settings.invoice_signature:
            sig_y = 80
            _draw_image_box(p, settings.invoice_signature, 50, sig_y, 160, 50, logger)
            p.setStrokeColorRGB(0.5, 0.5, 0.5)
            p.line(50, sig_y - 4, 210, sig_y - 4)
            p.setFont(font_italic, 9)
            p.setFillColorRGB(0.4, 0.4, 0.4)
            p.drawString(50, sig_y - 16, "Authorised signature")
            p.setFillColorRGB(0, 0, 0)

        if settings.invoice_footer_text:
            p.setFont(font_regular, 9)
            p.setFillColorRGB(0.5, 0.5, 0.5)
            footer_y = 40
            for line in settings.invoice_footer_text.split('\n')[:3]:
                p.drawCentredString(width / 2, footer_y, line.strip())
                footer_y -= 12

        p.showPage()
        p.save()

        return buffer.getvalue()
