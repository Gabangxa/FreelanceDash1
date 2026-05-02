"""
Webhook routes for handling external notifications from various services
Enhanced with comprehensive security system
"""
import json
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app, g
from sqlalchemy.exc import SQLAlchemyError
from app import db
from models import WebhookEvent, Notification, User
from webhooks.services import WebhookProcessor
from webhooks.security import require_webhook_security, require_admin_auth, WebhookSecurity
from webhooks.storage import get_storage
from webhooks import ip_ranges

# Setup logger
logger = logging.getLogger(__name__)
bp = Blueprint('webhooks', __name__, url_prefix='/webhooks')


@bp.route('/receive/<source>', methods=['POST'])
@require_webhook_security
def receive_webhook(source):
    """
    Generic webhook endpoint for receiving notifications from external services
    Security validation is handled by @require_webhook_security decorator
    
    Args:
        source: The source service (e.g., 'github', 'stripe', 'custom')
    """
    try:
        # Log incoming webhook with security context
        security_info = getattr(g, 'webhook_security', {})
        logger.info(
            f"Processing authenticated webhook from {source} "
            f"(IP: {security_info.get('client_ip', 'unknown')}, "
            f"Size: {security_info.get('payload_size', 0)} bytes)"
        )
        
        # Get request data (already validated by security decorator)
        payload = request.get_data(as_text=True)
        headers = dict(request.headers)
        
        # Extract event type from headers or payload
        event_type = _extract_event_type(source, headers, payload)
        
        # Store webhook event in database with security information
        webhook_event = WebhookEvent()
        webhook_event.source = source
        webhook_event.event_type = event_type
        webhook_event.payload = payload
        webhook_event.signature = headers.get('X-Signature') or headers.get('X-Hub-Signature-256') or headers.get('Stripe-Signature')
        webhook_event.headers = json.dumps(security_info.get('headers', {}))
        
        # Add security metadata as JSON. Note: stored on the
        # ``event_metadata`` column -- ``metadata`` is a reserved attribute
        # on SQLAlchemy DeclarativeBase and silently does not persist.
        webhook_event.event_metadata = json.dumps({
            'client_ip': security_info.get('client_ip'),
            'payload_size': security_info.get('payload_size'),
            'validation_time': security_info.get('validation_time'),
            'security_version': '2.0'
        })
        
        db.session.add(webhook_event)
        db.session.commit()  # Commit the event first
        
        # Process webhook (ideally this would be queued to a background job)
        # For now, we process synchronously but commit the event first for safety
        try:
            processor = WebhookProcessor()
            processor.process_webhook(webhook_event.id)
        except (SQLAlchemyError, KeyError, ValueError, RuntimeError) as e:
            # Log processing error but don't fail the webhook reception
            logger.exception(f"Error processing webhook {webhook_event.id}")
            # Update the webhook event with error information in a separate transaction
            try:
                webhook_event.error_message = str(e)
                webhook_event.processed = True
                webhook_event.processed_at = datetime.utcnow()
                db.session.commit()
            except SQLAlchemyError:
                db.session.rollback()
                logger.exception(f"Failed to record processing error on webhook {webhook_event.id}")
        
        logger.info(f"Successfully processed webhook {webhook_event.id} from {source}")
        return jsonify({'status': 'success', 'webhook_id': webhook_event.id}), 200
        
    except (SQLAlchemyError, KeyError, ValueError, OSError) as e:
        db.session.rollback()
        logger.exception(f"Error processing webhook from {source}")
        return jsonify({'error': 'Internal server error'}), 500


@bp.route('/events', methods=['GET'])
@require_admin_auth
def list_webhook_events():
    """List recent webhook events for debugging"""
    try:
        events = WebhookEvent.query.order_by(WebhookEvent.created_at.desc()).limit(50).all()
        
        events_data = []
        for event in events:
            # Parse metadata if available
            metadata = {}
            if event.event_metadata:
                try:
                    metadata = json.loads(event.event_metadata)
                except json.JSONDecodeError:
                    metadata = {}
            
            events_data.append({
                'id': event.id,
                'source': event.source,
                'event_type': event.event_type,
                'processed': event.processed,
                'created_at': event.created_at.isoformat() if event.created_at else None,
                'error_message': event.error_message,
                'metadata': metadata
            })
        
        return jsonify({'events': events_data}), 200
        
    except SQLAlchemyError as e:
        logger.exception("Error listing webhook events")
        return jsonify({'error': 'Internal server error'}), 500


@bp.route('/security/status', methods=['GET'])
@require_admin_auth
def security_status():
    """Get webhook security status and statistics"""
    try:
        # Pull counter aggregates from the shared storage backend so the
        # numbers reflect the union across all gunicorn workers, not just
        # the worker handling this request.
        storage = get_storage()
        active_rate_limits = storage.active_rate_limit_keys()
        failed_attempts = storage.total_failed_attempts(
            WebhookSecurity.FAILED_ATTEMPT_WINDOW_SECONDS
        )

        # Get recent webhook event statistics
        from datetime import timedelta
        recent_events = WebhookEvent.query.filter(
            WebhookEvent.created_at >= datetime.utcnow() - timedelta(hours=24)
        ).count()
        
        failed_events = WebhookEvent.query.filter(
            WebhookEvent.created_at >= datetime.utcnow() - timedelta(hours=24),
            WebhookEvent.error_message.isnot(None)
        ).count()
        
        # Per-source dynamic IP allowlist health. Surfaced here so an
        # operator can tell at a glance whether the GitHub/Stripe range
        # cache is being served from upstream or from the static
        # fallback (which happens during an upstream outage), and how
        # long ago each source was refreshed -- previously the only way
        # to know was to grep server logs.
        ip_allowlist = ip_ranges.all_statuses()

        # Failed-attempt counters are kept on a 1h rolling window (see
        # WebhookSecurity.FAILED_ATTEMPT_WINDOW_SECONDS), so the metric
        # name reflects that window. Webhook event counts are reported
        # over a 24h window from the persisted WebhookEvent table.
        return jsonify({
            'security_status': {
                'active_rate_limits': active_rate_limits,
                'failed_attempts_1h': failed_attempts,
                'total_events_24h': recent_events,
                'failed_events_24h': failed_events,
                'success_rate': round((recent_events - failed_events) / max(recent_events, 1) * 100, 2)
            },
            'storage_backend': storage.name,
            'ip_allowlist': ip_allowlist,
            'sources': list(WebhookSecurity.RATE_LIMITS.keys()),
            'timestamp': datetime.utcnow().isoformat()
        }), 200
        
    except (SQLAlchemyError, KeyError, ValueError) as e:
        logger.exception("Error getting security status")
        return jsonify({'error': 'Internal server error'}), 500


@bp.route('/security/clear-cache', methods=['POST'])
@require_admin_auth
def clear_security_cache():
    """Clear rate limiting and failed attempts cache"""
    try:
        get_storage().clear_counters()

        logger.info("Webhook security cache cleared by admin")
        return jsonify({'message': 'Security cache cleared successfully'}), 200
        
    except (SQLAlchemyError, OSError, RuntimeError, ConnectionError) as e:
        logger.exception("Error clearing security cache")
        return jsonify({'error': 'Internal server error'}), 500


def _extract_event_type(source, headers, payload):
    """Extract event type from webhook headers or payload"""
    try:
        # GitHub webhooks
        if source == 'github':
            return headers.get('X-GitHub-Event', 'unknown')
        
        # Stripe webhooks
        elif source == 'stripe':
            try:
                data = json.loads(payload)
                return data.get('type', 'unknown')
            except json.JSONDecodeError:
                return 'unknown'
        
        # Generic webhooks - try to extract from headers
        elif 'X-Event-Type' in headers:
            return headers['X-Event-Type']
        elif 'X-Event' in headers:
            return headers['X-Event']
        
        # Try to extract from payload
        try:
            data = json.loads(payload)
            return data.get('event_type') or data.get('type') or data.get('event') or 'unknown'
        except json.JSONDecodeError:
            return 'unknown'
            
    except (KeyError, ValueError, AttributeError) as e:
        logger.exception(f"Could not extract event type for {source}")
        return 'unknown'


# Legacy signature verification function removed
# Security is now handled by the WebhookSecurity class and @require_webhook_security decorator