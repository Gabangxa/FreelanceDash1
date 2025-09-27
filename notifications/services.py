"""
Notification delivery service for the Freelancer Suite application.
"""
import logging
from datetime import datetime
from app import db
from models import Notification, NotificationSettings, User
from mail import send_notification_email

# Setup logger
logger = logging.getLogger('notifications')

class NotificationDeliveryService:
    """Service for delivering notifications through various channels."""
    
    @staticmethod
    def deliver_notification(notification_id, channels=None):
        """
        Deliver a notification through specified channels.
        
        Args:
            notification_id (int): ID of the notification to deliver
            channels (list): List of delivery channels ('email', 'in_app'). 
                           If None, uses user preferences.
        
        Returns:
            dict: Delivery results with status for each channel
        """
        try:
            # Get the notification
            notification = Notification.query.get(notification_id)
            if not notification:
                logger.error(f"Notification {notification_id} not found")
                return {'error': 'Notification not found'}
            
            # Get the user
            user = User.query.get(notification.user_id)
            if not user:
                logger.error(f"User {notification.user_id} not found for notification {notification_id}")
                return {'error': 'User not found'}
            
            # Get user notification settings
            settings = NotificationSettings.query.filter_by(user_id=user.id).first()
            if not settings:
                # Create default settings if none exist
                settings = NotificationSettings()
                settings.user_id = user.id
                settings.email_enabled = True
                settings.digest_frequency = 'immediate'
                settings.inapp_enabled = True
                settings.email_webhook_events = True
                settings.inapp_webhook_events = True
                db.session.add(settings)
                db.session.commit()
            
            # Determine delivery channels
            if channels is None:
                channels = []
                if settings.email_enabled and settings.email_webhook_events:
                    # Check email frequency preferences
                    if settings.digest_frequency == 'immediate':
                        channels.append('email')
                    elif notification.priority == 'high':
                        channels.append('email')
                
                if settings.inapp_enabled and settings.inapp_webhook_events:
                    channels.append('in_app')
            
            results = {}
            
            # Deliver via email
            if 'email' in channels:
                try:
                    email_sent = send_notification_email(user, notification)
                    if email_sent:
                        results['email'] = {
                            'status': 'success',
                            'timestamp': datetime.utcnow().isoformat()
                        }
                        logger.info(f"Email notification queued successfully for {user.email} (notification {notification_id})")
                    else:
                        results['email'] = {
                            'status': 'failed',
                            'error': 'send_notification_email returned False',
                            'timestamp': datetime.utcnow().isoformat()
                        }
                        logger.error(f"Failed to queue email notification for {user.email} (notification {notification_id})")
                except Exception as e:
                    logger.error(f"Error sending email notification: {str(e)}")
                    results['email'] = {
                        'status': 'error',
                        'error': str(e),
                        'timestamp': datetime.utcnow().isoformat()
                    }
            
            # Handle in-app notifications
            if 'in_app' in channels:
                # In-app notifications are handled by storing in database (already done)
                # Mark as delivered for in-app
                results['in_app'] = {
                    'status': 'success',
                    'timestamp': datetime.utcnow().isoformat()
                }
                logger.info(f"In-app notification ready for user {user.id} (notification {notification_id})")
            
            # Update notification delivery status
            notification.delivered = len([r for r in results.values() if r.get('status') == 'success']) > 0
            notification.delivery_attempts = (notification.delivery_attempts or 0) + 1
            notification.last_delivery_attempt = datetime.utcnow()
            
            db.session.commit()
            
            logger.info(f"Notification {notification_id} delivery completed: {results}")
            return results
            
        except Exception as e:
            logger.error(f"Error delivering notification {notification_id}: {str(e)}")
            db.session.rollback()
            return {'error': str(e)}
    
    @staticmethod
    def deliver_notifications_for_user(user_id, channels=None):
        """
        Deliver all undelivered notifications for a specific user.
        
        Args:
            user_id (int): ID of the user
            channels (list): List of delivery channels
        
        Returns:
            dict: Summary of delivery results
        """
        try:
            # Get undelivered notifications for the user
            undelivered = Notification.query.filter_by(
                user_id=user_id, 
                delivered=False
            ).all()
            
            if not undelivered:
                return {'message': 'No undelivered notifications', 'delivered_count': 0}
            
            results = []
            successful_deliveries = 0
            
            for notification in undelivered:
                delivery_result = NotificationDeliveryService.deliver_notification(
                    notification.id, 
                    channels
                )
                results.append({
                    'notification_id': notification.id,
                    'result': delivery_result
                })
                
                if any(r.get('status') == 'success' for r in delivery_result.values() if isinstance(r, dict)):
                    successful_deliveries += 1
            
            logger.info(f"Delivered {successful_deliveries}/{len(undelivered)} notifications for user {user_id}")
            
            return {
                'total_notifications': len(undelivered),
                'successful_deliveries': successful_deliveries,
                'results': results
            }
            
        except Exception as e:
            logger.error(f"Error delivering notifications for user {user_id}: {str(e)}")
            return {'error': str(e)}
    
    @staticmethod
    def get_unread_notifications(user_id, limit=50):
        """
        Get unread in-app notifications for a user.
        
        Args:
            user_id (int): ID of the user
            limit (int): Maximum number of notifications to return
        
        Returns:
            list: List of unread notifications
        """
        try:
            notifications = Notification.query.filter_by(
                user_id=user_id,
                read=False
            ).order_by(Notification.created_at.desc()).limit(limit).all()
            
            return [{
                'id': n.id,
                'title': n.title,
                'message': n.message,
                'priority': n.priority,
                'created_at': n.created_at.isoformat(),
                'source': n.source,
                'event_type': n.event_type
            } for n in notifications]
            
        except Exception as e:
            logger.error(f"Error getting unread notifications for user {user_id}: {str(e)}")
            return []
    
    @staticmethod
    def mark_notification_read(notification_id, user_id):
        """
        Mark a notification as read.
        
        Args:
            notification_id (int): ID of the notification
            user_id (int): ID of the user (for security check)
        
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            notification = Notification.query.filter_by(
                id=notification_id,
                user_id=user_id
            ).first()
            
            if not notification:
                logger.warning(f"Notification {notification_id} not found for user {user_id}")
                return False
            
            notification.read = True
            notification.read_at = datetime.utcnow()
            db.session.commit()
            
            logger.info(f"Notification {notification_id} marked as read by user {user_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error marking notification {notification_id} as read: {str(e)}")
            db.session.rollback()
            return False