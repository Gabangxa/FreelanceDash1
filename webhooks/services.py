"""
Webhook processing services for handling different types of webhook events
"""
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any
from app import db
from models import WebhookEvent, Notification, User, NotificationSettings

# Setup logger
logger = logging.getLogger(__name__)


class WebhookProcessor:
    """Process webhook events and create appropriate notifications"""
    
    def process_webhook(self, webhook_event_id: int) -> bool:
        """
        Process a webhook event and create notifications
        
        Args:
            webhook_event_id: ID of the webhook event to process
            
        Returns:
            bool: True if processing was successful
        """
        try:
            webhook_event = WebhookEvent.query.get(webhook_event_id)
            if not webhook_event:
                logger.error(f"Webhook event {webhook_event_id} not found")
                return False
            
            # Parse payload
            try:
                payload_data = json.loads(webhook_event.payload)
            except json.JSONDecodeError:
                logger.error(f"Invalid JSON payload in webhook {webhook_event_id}")
                webhook_event.error_message = "Invalid JSON payload"
                webhook_event.processed = True
                webhook_event.processed_at = datetime.utcnow()
                db.session.commit()
                return False
            
            # Process based on source
            success = False
            if webhook_event.source == 'github':
                success = self._process_github_webhook(webhook_event, payload_data)
            elif webhook_event.source == 'stripe':
                success = self._process_stripe_webhook(webhook_event, payload_data)
            elif webhook_event.source == 'custom':
                success = self._process_custom_webhook(webhook_event, payload_data)
            else:
                success = self._process_generic_webhook(webhook_event, payload_data)
            
            # Mark as processed
            webhook_event.processed = True
            webhook_event.processed_at = datetime.utcnow()
            
            if not success:
                webhook_event.error_message = f"Failed to process {webhook_event.source} webhook"
            
            db.session.commit()
            
            logger.info(f"Webhook {webhook_event_id} processed successfully: {success}")
            return success
            
        except Exception as e:
            logger.error(f"Error processing webhook {webhook_event_id}: {str(e)}")
            try:
                webhook_event = WebhookEvent.query.get(webhook_event_id)
                if webhook_event:
                    webhook_event.error_message = str(e)
                    webhook_event.processed = True
                    webhook_event.processed_at = datetime.utcnow()
                    db.session.commit()
            except Exception:
                db.session.rollback()
            return False
    
    def _process_github_webhook(self, webhook_event: WebhookEvent, payload: Dict[Any, Any]) -> bool:
        """Process GitHub webhook events"""
        try:
            event_type = webhook_event.event_type
            repo_name = payload.get('repository', {}).get('name', 'Unknown Repository')
            
            # Create notifications based on event type
            if event_type == 'push':
                commits = payload.get('commits', [])
                message = f"New push to {repo_name} with {len(commits)} commit(s)"
                
            elif event_type == 'pull_request':
                action = payload.get('action')
                pr_title = payload.get('pull_request', {}).get('title', 'Unknown PR')
                message = f"Pull request {action}: {pr_title} in {repo_name}"
                
            elif event_type == 'issues':
                action = payload.get('action')
                issue_title = payload.get('issue', {}).get('title', 'Unknown Issue')
                message = f"Issue {action}: {issue_title} in {repo_name}"
                
            else:
                message = f"GitHub {event_type} event in {repo_name}"
            
            # Create notification for all users (or specific users based on your logic)
            self._create_system_notification(
                title=f"GitHub: {repo_name}",
                message=message,
                webhook_event=webhook_event,
                notification_type='webhook',
                priority='normal'
            )
            
            return True
            
        except Exception as e:
            logger.error(f"Error processing GitHub webhook: {str(e)}")
            return False
    
    def _process_stripe_webhook(self, webhook_event: WebhookEvent, payload: Dict[Any, Any]) -> bool:
        """Process Stripe webhook events"""
        try:
            event_type = webhook_event.event_type
            
            # Handle payment-related events
            if event_type.startswith('payment_intent.'):
                amount = payload.get('data', {}).get('object', {}).get('amount', 0)
                currency = payload.get('data', {}).get('object', {}).get('currency', 'USD')
                message = f"Payment {event_type.split('.')[-1]}: {amount/100:.2f} {currency.upper()}"
                priority = 'high' if 'failed' in event_type else 'normal'
                
            elif event_type.startswith('customer.'):
                customer_email = payload.get('data', {}).get('object', {}).get('email', 'Unknown Customer')
                message = f"Customer {event_type.split('.')[-1]}: {customer_email}"
                priority = 'normal'
                
            else:
                message = f"Stripe event: {event_type}"
                priority = 'normal'
            
            # Create notification
            self._create_system_notification(
                title="Stripe Payment",
                message=message,
                webhook_event=webhook_event,
                notification_type='webhook',
                priority=priority
            )
            
            return True
            
        except Exception as e:
            logger.error(f"Error processing Stripe webhook: {str(e)}")
            return False
    
    def _process_custom_webhook(self, webhook_event: WebhookEvent, payload: Dict[Any, Any]) -> bool:
        """Process custom webhook events from user applications"""
        try:
            # Extract notification details from payload
            title = payload.get('title', f"Custom Webhook: {webhook_event.event_type}")
            message = payload.get('message', 'Custom webhook event received')
            priority = payload.get('priority', 'normal')
            user_id = payload.get('user_id')
            action_url = payload.get('action_url')
            
            # Validate priority
            if priority not in ['low', 'normal', 'high', 'urgent']:
                priority = 'normal'
            
            # Create notification
            if user_id:
                # Specific user notification
                self._create_user_notification(
                    user_id=user_id,
                    title=title,
                    message=message,
                    webhook_event=webhook_event,
                    notification_type='webhook',
                    priority=priority,
                    action_url=action_url
                )
            else:
                # System-wide notification
                self._create_system_notification(
                    title=title,
                    message=message,
                    webhook_event=webhook_event,
                    notification_type='webhook',
                    priority=priority,
                    action_url=action_url
                )
            
            return True
            
        except Exception as e:
            logger.error(f"Error processing custom webhook: {str(e)}")
            return False
    
    def _process_generic_webhook(self, webhook_event: WebhookEvent, payload: Dict[Any, Any]) -> bool:
        """Process generic webhook events"""
        try:
            # Create a basic notification
            message = f"Webhook received from {webhook_event.source}: {webhook_event.event_type}"
            
            self._create_system_notification(
                title=f"Webhook: {webhook_event.source}",
                message=message,
                webhook_event=webhook_event,
                notification_type='webhook',
                priority='normal'
            )
            
            return True
            
        except Exception as e:
            logger.error(f"Error processing generic webhook: {str(e)}")
            return False
    
    def _create_user_notification(self, user_id: int, title: str, message: str, 
                                webhook_event: WebhookEvent, notification_type: str = 'webhook',
                                priority: str = 'normal', action_url: Optional[str] = None) -> bool:
        """Create a notification for a specific user"""
        try:
            # Check if user exists
            user = User.query.get(user_id)
            if not user:
                logger.warning(f"User {user_id} not found for notification")
                return False
            
            # Check user's notification preferences
            settings = NotificationSettings.get_or_create_for_user(user_id)
            if not settings.inapp_webhook_events:
                logger.info(f"User {user_id} has disabled webhook notifications")
                return True  # Not an error, just user preference
            
            # Create notification
            notification = Notification()
            notification.user_id = user_id
            notification.title = title
            notification.message = message
            notification.notification_type = notification_type
            notification.priority = priority
            notification.webhook_event_id = webhook_event.id
            notification.action_url = action_url
            
            db.session.add(notification)
            db.session.commit()
            
            # Deliver the notification
            from notifications.services import NotificationDeliveryService
            delivery_result = NotificationDeliveryService.deliver_notification(notification.id)
            
            logger.info(f"Created notification {notification.id} for user {user_id}. Delivery: {delivery_result}")
            return True
            
        except Exception as e:
            logger.error(f"Error creating user notification: {str(e)}")
            return False
    
    def _create_system_notification(self, title: str, message: str, webhook_event: WebhookEvent,
                                  notification_type: str = 'webhook', priority: str = 'normal',
                                  action_url: Optional[str] = None) -> bool:
        """Create notifications for all users (system-wide notification)"""
        try:
            # Get all users who want webhook notifications
            users = User.query.all()
            notifications_created = 0
            
            for user in users:
                # Check user's notification preferences
                settings = NotificationSettings.get_or_create_for_user(user.id)
                if not settings.inapp_webhook_events:
                    continue
                
                # Create notification
                notification = Notification()
                notification.user_id = user.id
                notification.title = title
                notification.message = message
                notification.notification_type = notification_type
                notification.priority = priority
                notification.webhook_event_id = webhook_event.id
                notification.action_url = action_url
                
                db.session.add(notification)
                notifications_created += 1
            
            db.session.commit()
            
            # Deliver notifications for all users who have them
            from notifications.services import NotificationDeliveryService
            delivered_count = 0
            
            # Get the notifications we just created for delivery
            recent_notifications = Notification.query.filter_by(webhook_event_id=webhook_event.id).all()
            
            for notification in recent_notifications:
                delivery_result = NotificationDeliveryService.deliver_notification(notification.id)
                if any(r.get('status') == 'success' for r in delivery_result.values() if isinstance(r, dict)):
                    delivered_count += 1
            
            logger.info(f"Created {notifications_created} system notifications, delivered {delivered_count}")
            return True
            
        except Exception as e:
            logger.error(f"Error creating system notifications: {str(e)}")
            return False