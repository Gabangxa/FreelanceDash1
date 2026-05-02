from flask import render_template, flash, redirect, url_for, request, jsonify
from flask_login import login_required
from admin import bp
from admin.decorators import admin_required
from models import User, Client, Project, Invoice, WebhookEvent, Notification, TimeEntry
from app import db
from datetime import datetime, timedelta
from sqlalchemy import func, desc
from sqlalchemy.exc import SQLAlchemyError
import json
import logging

logger = logging.getLogger(__name__)


@bp.route('/dashboard')
@login_required
@admin_required
def dashboard():
    """Admin dashboard with key metrics"""
    
    # User statistics
    total_users = User.query.count()
    new_users_30d = User.query.filter(
        User.created_at >= datetime.utcnow() - timedelta(days=30)
    ).count()
    admin_users = User.query.filter_by(is_admin=True).count()
    
    # Client statistics
    total_clients = Client.query.count()
    new_clients_30d = Client.query.filter(
        Client.created_at >= datetime.utcnow() - timedelta(days=30)
    ).count()
    
    # Project statistics
    total_projects = Project.query.count()
    active_projects = Project.query.filter_by(status='active').count()
    completed_projects = Project.query.filter_by(status='completed').count()
    
    # Invoice statistics
    total_invoices = Invoice.query.count()
    paid_invoices = Invoice.query.filter_by(status='paid').count()
    pending_invoices = Invoice.query.filter_by(status='pending').count()
    overdue_invoices = Invoice.query.filter(
        Invoice.status == 'pending',
        Invoice.due_date < datetime.utcnow()
    ).count()
    
    # Revenue calculations
    total_revenue = db.session.query(func.sum(Invoice.amount)).filter(
        Invoice.status == 'paid'
    ).scalar() or 0
    
    revenue_30d = db.session.query(func.sum(Invoice.amount)).filter(
        Invoice.status == 'paid',
        Invoice.created_at >= datetime.utcnow() - timedelta(days=30)
    ).scalar() or 0
    
    # Webhook statistics
    total_webhooks = WebhookEvent.query.count()
    webhooks_24h = WebhookEvent.query.filter(
        WebhookEvent.created_at >= datetime.utcnow() - timedelta(hours=24)
    ).count()
    failed_webhooks_24h = WebhookEvent.query.filter(
        WebhookEvent.created_at >= datetime.utcnow() - timedelta(hours=24),
        WebhookEvent.error_message.isnot(None)
    ).count()
    webhook_success_rate = round(
        (webhooks_24h - failed_webhooks_24h) / max(webhooks_24h, 1) * 100, 2
    )
    
    # Notification statistics
    total_notifications = Notification.query.count()
    unread_notifications = Notification.query.filter_by(read=False).count()
    notifications_24h = Notification.query.filter(
        Notification.created_at >= datetime.utcnow() - timedelta(hours=24)
    ).count()
    
    # Time tracking statistics. ``TimeEntry.duration`` is stored in
    # minutes (see models.py); ``minutes_to_hours`` is the single source
    # of truth for the conversion -- using it here makes it impossible
    # to reintroduce the C2 ``/3600`` bug that under-reported totals 60x.
    from utils.duration import minutes_to_hours
    total_hours = round(
        minutes_to_hours(db.session.query(func.sum(TimeEntry.duration)).scalar()),
        2,
    )
    billable_hours = round(
        minutes_to_hours(
            db.session.query(func.sum(TimeEntry.duration))
            .filter(TimeEntry.billable == True)
            .scalar()
        ),
        2,
    )
    
    # Recent activity
    recent_users = User.query.order_by(desc(User.created_at)).limit(5).all()
    recent_projects = Project.query.order_by(desc(Project.created_at)).limit(5).all()
    recent_invoices = Invoice.query.order_by(desc(Invoice.created_at)).limit(5).all()
    
    # Webhook sources breakdown
    webhook_sources = db.session.query(
        WebhookEvent.source,
        func.count(WebhookEvent.id).label('count')
    ).group_by(WebhookEvent.source).all()
    
    # User growth chart data (last 30 days)
    user_growth = []
    for i in range(30, -1, -1):
        date = (datetime.utcnow() - timedelta(days=i)).date()
        count = User.query.filter(func.date(User.created_at) == date).count()
        user_growth.append({
            'date': date.strftime('%Y-%m-%d'),
            'count': count
        })
    
    # Revenue chart data (last 30 days)
    revenue_chart = []
    for i in range(30, -1, -1):
        date = (datetime.utcnow() - timedelta(days=i)).date()
        daily_revenue = db.session.query(func.sum(Invoice.amount)).filter(
            func.date(Invoice.created_at) == date,
            Invoice.status == 'paid'
        ).scalar() or 0
        revenue_chart.append({
            'date': date.strftime('%Y-%m-%d'),
            'amount': float(daily_revenue)
        })
    
    return render_template('admin/dashboard.html',
        # User stats
        total_users=total_users,
        new_users_30d=new_users_30d,
        admin_users=admin_users,
        
        # Client stats
        total_clients=total_clients,
        new_clients_30d=new_clients_30d,
        
        # Project stats
        total_projects=total_projects,
        active_projects=active_projects,
        completed_projects=completed_projects,
        
        # Invoice stats
        total_invoices=total_invoices,
        paid_invoices=paid_invoices,
        pending_invoices=pending_invoices,
        overdue_invoices=overdue_invoices,
        
        # Revenue stats
        total_revenue=total_revenue,
        revenue_30d=revenue_30d,
        
        # Webhook stats
        total_webhooks=total_webhooks,
        webhooks_24h=webhooks_24h,
        failed_webhooks_24h=failed_webhooks_24h,
        webhook_success_rate=webhook_success_rate,
        webhook_sources=webhook_sources,
        
        # Notification stats
        total_notifications=total_notifications,
        unread_notifications=unread_notifications,
        notifications_24h=notifications_24h,
        
        # Time stats
        total_hours=total_hours,
        billable_hours=billable_hours,
        
        # Recent activity
        recent_users=recent_users,
        recent_projects=recent_projects,
        recent_invoices=recent_invoices,
        
        # Chart data
        user_growth=json.dumps(user_growth),
        revenue_chart=json.dumps(revenue_chart)
    )


@bp.route('/users')
@login_required
@admin_required
def users():
    """Manage users"""
    page = request.args.get('page', 1, type=int)
    per_page = 50
    
    users = User.query.order_by(desc(User.created_at)).paginate(
        page=page, per_page=per_page, error_out=False
    )
    
    return render_template('admin/users.html', users=users)


@bp.route('/users/<int:user_id>/toggle-admin', methods=['POST'])
@login_required
@admin_required
def toggle_admin(user_id):
    """Toggle admin status for a user"""
    user = User.query.get_or_404(user_id)
    
    # Prevent removing admin from yourself
    from flask_login import current_user
    if user.id == current_user.id:
        flash('You cannot remove admin privileges from yourself.', 'warning')
        return redirect(url_for('admin.users'))
    
    user.is_admin = not user.is_admin
    db.session.commit()
    
    action = 'granted' if user.is_admin else 'removed'
    flash(f'Admin privileges {action} for {user.username}.', 'success')
    
    return redirect(url_for('admin.users'))


@bp.route('/webhooks')
@login_required
@admin_required
def webhooks():
    """View webhook events"""
    page = request.args.get('page', 1, type=int)
    per_page = 50

    webhook_events = WebhookEvent.query.order_by(desc(WebhookEvent.created_at)).paginate(
        page=page, per_page=per_page, error_out=False
    )

    # Surface the dynamic IP allowlist + storage backend health in the
    # admin webhooks page so operators don't have to hit the JSON
    # endpoint or grep server logs to know whether the GitHub/Stripe
    # range refresh is healthy. We swallow any exception here so a
    # transient storage outage can never take the events list offline.
    security_health = {
        'storage_backend': None,
        'ip_allowlist': [],
        'nats': None,
        'error': None,
    }
    try:
        from webhooks.storage import get_storage
        from webhooks import ip_ranges
        import nats_client
        security_health['storage_backend'] = get_storage().name
        security_health['ip_allowlist'] = ip_ranges.all_statuses()
        # NATS connection state, last published event timestamp, etc.
        # Always present (no-op stub when NATS_URL is unset) so the
        # template can render unconditionally.
        security_health['nats'] = nats_client.state()
    except Exception as exc:  # noqa: BLE001 - status panel must never crash
        security_health['error'] = str(exc)

    return render_template(
        'admin/webhooks.html',
        webhook_events=webhook_events,
        security_health=security_health,
    )


@bp.route('/system')
@login_required
@admin_required
def system():
    """System health and configuration"""
    import sys
    import platform
    from sqlalchemy import text
    
    # Database statistics
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    table_names = inspector.get_table_names()
    
    # Get table row counts. We narrow this from a bare ``except`` to the
    # specific DB-side errors that can plausibly fire here (missing
    # table, busy DB, etc). Anything else -- KeyboardInterrupt,
    # MemoryError, programming errors -- should still propagate so
    # operators see a real stack trace instead of a silent ``N/A``.
    table_stats = []
    for table in table_names:
        try:
            count = db.session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            table_stats.append({'name': table, 'rows': count})
        except SQLAlchemyError:
            logger.exception("Failed to count rows for table %s", table)
            table_stats.append({'name': table, 'rows': 'N/A'})
    
    system_info = {
        'python_version': sys.version,
        'platform': platform.platform(),
        'database_tables': len(table_names),
        'table_stats': table_stats
    }
    
    return render_template('admin/system.html', system_info=system_info)
