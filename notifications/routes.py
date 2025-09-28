"""
Routes for in-app notification display and management
"""
import logging
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash
from flask_login import login_required, current_user
from app import db
from models import Notification

logger = logging.getLogger(__name__)

# Create notifications blueprint
notifications_bp = Blueprint(
    'notifications', 
    __name__, 
    url_prefix='/notifications',
    template_folder='../templates/notifications'
)


@notifications_bp.route('/')
@login_required
def list_notifications():
    """Display all notifications for the current user"""
    try:
        # Get pagination parameters
        page = request.args.get('page', 1, type=int)
        per_page = 20
        
        # Get filter parameters
        notification_type = request.args.get('type', '')
        status = request.args.get('status', '')
        
        # Build query
        query = Notification.query.filter(Notification.user_id == current_user.id)
        
        # Apply filters
        if notification_type:
            query = query.filter(Notification.notification_type == notification_type)
        
        if status == 'read':
            query = query.filter(Notification.read == True)
        elif status == 'unread':
            query = query.filter(Notification.read == False)
        
        # Order by creation date (newest first)
        query = query.order_by(Notification.created_at.desc())
        
        # Paginate results
        notifications = query.paginate(
            page=page,
            per_page=per_page,
            error_out=False
        )
        
        # Get notification statistics
        total_count = Notification.query.filter(Notification.user_id == current_user.id).count()
        unread_count = Notification.query.filter(
            Notification.user_id == current_user.id,
            Notification.read == False
        ).count()
        
        # Get available notification types for filter dropdown
        notification_types = db.session.query(Notification.notification_type)\
            .filter(Notification.user_id == current_user.id)\
            .distinct().all()
        notification_types = [t[0] for t in notification_types]
        
        return render_template(
            'notifications/list.html',
            notifications=notifications,
            total_count=total_count,
            unread_count=unread_count,
            notification_types=notification_types,
            current_type=notification_type,
            current_status=status
        )
        
    except Exception as e:
        logger.error(f"Error listing notifications for user {current_user.id}: {str(e)}")
        flash('Error loading notifications. Please try again.', 'danger')
        return render_template('notifications/list.html', notifications=None)


@notifications_bp.route('/<int:notification_id>')
@login_required
def view_notification(notification_id):
    """View a specific notification and mark it as read"""
    try:
        notification = Notification.query.filter(
            Notification.id == notification_id,
            Notification.user_id == current_user.id
        ).first_or_404()
        
        # Mark as read if not already read
        if not notification.read:
            notification.read = True
            notification.read_at = datetime.utcnow()
            db.session.commit()
            logger.info(f"Marked notification {notification_id} as read for user {current_user.id}")
        
        return render_template('notifications/detail.html', notification=notification)
        
    except Exception as e:
        logger.error(f"Error viewing notification {notification_id} for user {current_user.id}: {str(e)}")
        flash('Error loading notification. Please try again.', 'danger')
        return redirect(url_for('notifications.list_notifications'))


@notifications_bp.route('/mark-read/<int:notification_id>', methods=['POST'])
@login_required
def mark_as_read(notification_id):
    """Mark a specific notification as read"""
    try:
        notification = Notification.query.filter(
            Notification.id == notification_id,
            Notification.user_id == current_user.id
        ).first_or_404()
        
        notification.read = True
        notification.read_at = datetime.utcnow()
        db.session.commit()
        
        logger.info(f"Marked notification {notification_id} as read for user {current_user.id}")
        
        # Return JSON response for AJAX requests
        if request.is_json or request.headers.get('Content-Type') == 'application/json':
            return jsonify({
                'success': True,
                'message': 'Notification marked as read'
            })
        
        flash('Notification marked as read', 'success')
        return redirect(url_for('notifications.list_notifications'))
        
    except Exception as e:
        logger.error(f"Error marking notification {notification_id} as read: {str(e)}")
        
        if request.is_json or request.headers.get('Content-Type') == 'application/json':
            return jsonify({
                'success': False,
                'message': 'Error marking notification as read'
            }), 500
        
        flash('Error marking notification as read', 'danger')
        return redirect(url_for('notifications.list_notifications'))


@notifications_bp.route('/mark-all-read', methods=['POST'])
@login_required
def mark_all_as_read():
    """Mark all notifications as read for the current user"""
    try:
        updated_count = Notification.query.filter(
            Notification.user_id == current_user.id,
            Notification.read == False
        ).update({
            'read': True,
            'read_at': datetime.utcnow()
        })
        
        db.session.commit()
        
        logger.info(f"Marked {updated_count} notifications as read for user {current_user.id}")
        
        # Return JSON response for AJAX requests
        if request.is_json or request.headers.get('Content-Type') == 'application/json':
            return jsonify({
                'success': True,
                'message': f'Marked {updated_count} notifications as read',
                'updated_count': updated_count
            })
        
        flash(f'Marked {updated_count} notifications as read', 'success')
        return redirect(url_for('notifications.list_notifications'))
        
    except Exception as e:
        logger.error(f"Error marking all notifications as read for user {current_user.id}: {str(e)}")
        
        if request.is_json or request.headers.get('Content-Type') == 'application/json':
            return jsonify({
                'success': False,
                'message': 'Error marking notifications as read'
            }), 500
        
        flash('Error marking notifications as read', 'danger')
        return redirect(url_for('notifications.list_notifications'))


@notifications_bp.route('/delete/<int:notification_id>', methods=['POST'])
@login_required
def delete_notification(notification_id):
    """Delete a specific notification"""
    try:
        notification = Notification.query.filter(
            Notification.id == notification_id,
            Notification.user_id == current_user.id
        ).first_or_404()
        
        db.session.delete(notification)
        db.session.commit()
        
        logger.info(f"Deleted notification {notification_id} for user {current_user.id}")
        
        # Return JSON response for AJAX requests
        if request.is_json or request.headers.get('Content-Type') == 'application/json':
            return jsonify({
                'success': True,
                'message': 'Notification deleted'
            })
        
        flash('Notification deleted', 'success')
        return redirect(url_for('notifications.list_notifications'))
        
    except Exception as e:
        logger.error(f"Error deleting notification {notification_id}: {str(e)}")
        
        if request.is_json or request.headers.get('Content-Type') == 'application/json':
            return jsonify({
                'success': False,
                'message': 'Error deleting notification'
            }), 500
        
        flash('Error deleting notification', 'danger')
        return redirect(url_for('notifications.list_notifications'))


@notifications_bp.route('/api/unread-count')
@login_required
def get_unread_count():
    """API endpoint to get unread notification count for the current user"""
    try:
        unread_count = Notification.query.filter(
            Notification.user_id == current_user.id,
            Notification.read == False
        ).count()
        
        return jsonify({
            'success': True,
            'unread_count': unread_count
        })
        
    except Exception as e:
        logger.error(f"Error getting unread count for user {current_user.id}: {str(e)}")
        return jsonify({
            'success': False,
            'unread_count': 0
        }), 500


@notifications_bp.route('/api/recent')
@login_required
def get_recent_notifications():
    """API endpoint to get recent notifications for dropdown/popup display"""
    try:
        # Get 5 most recent notifications
        notifications = Notification.query.filter(
            Notification.user_id == current_user.id
        ).order_by(
            Notification.created_at.desc()
        ).limit(5).all()
        
        notifications_data = []
        for notification in notifications:
            notifications_data.append({
                'id': notification.id,
                'title': notification.title,
                'message': notification.message[:100] + '...' if len(notification.message) > 100 else notification.message,
                'type': notification.notification_type,
                'read': notification.read,
                'created_at': notification.created_at.isoformat() if notification.created_at else None,
                'url': url_for('notifications.view_notification', notification_id=notification.id)
            })
        
        return jsonify({
            'success': True,
            'notifications': notifications_data
        })
        
    except Exception as e:
        logger.error(f"Error getting recent notifications for user {current_user.id}: {str(e)}")
        return jsonify({
            'success': False,
            'notifications': []
        }), 500