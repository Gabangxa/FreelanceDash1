"""
Webhook routes for handling external notifications from various services
"""
import json
import hmac
import hashlib
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app
from app import db
from models import WebhookEvent, Notification, User
from webhooks.services import WebhookProcessor

# Setup logger
logger = logging.getLogger(__name__)
bp = Blueprint('webhooks', __name__, url_prefix='/webhooks')


@bp.route('/receive/<source>', methods=['POST'])
def receive_webhook(source):
    """
    Generic webhook endpoint for receiving notifications from external services
    
    Args:
        source: The source service (e.g., 'github', 'stripe', 'custom')
    """
    try:
        # Log incoming webhook
        logger.info(f"Received webhook from {source}")
        
        # Get request data
        payload = request.get_data(as_text=True)
        headers = dict(request.headers)
        
        # Extract event type from headers or payload
        event_type = _extract_event_type(source, headers, payload)
        
        # Verify webhook signature if configured
        signature_valid = _verify_webhook_signature(source, payload, headers)
        if not signature_valid:
            logger.warning(f"Invalid webhook signature from {source}")
            return jsonify({'error': 'Invalid signature'}), 401
        
        # Store webhook event in database
        webhook_event = WebhookEvent()
        webhook_event.source = source
        webhook_event.event_type = event_type
        webhook_event.payload = payload
        webhook_event.signature = headers.get('X-Signature') or headers.get('X-Hub-Signature-256')
        webhook_event.headers = json.dumps({k: v for k, v in headers.items() if k.lower().startswith('x-')})
        
        db.session.add(webhook_event)
        db.session.commit()  # Commit the event first
        
        # Process webhook (ideally this would be queued to a background job)
        # For now, we process synchronously but commit the event first for safety
        try:
            processor = WebhookProcessor()
            processor.process_webhook(webhook_event.id)
        except Exception as e:
            # Log processing error but don't fail the webhook reception
            logger.error(f"Error processing webhook {webhook_event.id}: {str(e)}")
            # Update the webhook event with error information in a separate transaction
            try:
                webhook_event.error_message = str(e)
                webhook_event.processed = True
                webhook_event.processed_at = datetime.utcnow()
                db.session.commit()
            except Exception:
                db.session.rollback()
        
        logger.info(f"Successfully processed webhook {webhook_event.id} from {source}")
        return jsonify({'status': 'success', 'webhook_id': webhook_event.id}), 200
        
    except Exception as e:
        logger.error(f"Error processing webhook from {source}: {str(e)}")
        db.session.rollback()
        return jsonify({'error': 'Internal server error'}), 500


@bp.route('/events', methods=['GET'])
def list_webhook_events():
    """List recent webhook events for debugging"""
    try:
        events = WebhookEvent.query.order_by(WebhookEvent.created_at.desc()).limit(50).all()
        
        events_data = []
        for event in events:
            events_data.append({
                'id': event.id,
                'source': event.source,
                'event_type': event.event_type,
                'processed': event.processed,
                'created_at': event.created_at.isoformat() if event.created_at else None,
                'error_message': event.error_message
            })
        
        return jsonify({'events': events_data}), 200
        
    except Exception as e:
        logger.error(f"Error listing webhook events: {str(e)}")
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
            
    except Exception as e:
        logger.warning(f"Could not extract event type for {source}: {str(e)}")
        return 'unknown'


def _verify_webhook_signature(source, payload, headers):
    """Verify webhook signature based on source"""
    try:
        # Get webhook secret from environment based on source
        secret_key = current_app.config.get(f'WEBHOOK_{source.upper()}_SECRET')
        
        if not secret_key:
            logger.info(f"No webhook secret configured for {source}, skipping signature verification")
            return True  # Allow webhooks without secrets for development
        
        # GitHub signature verification
        if source == 'github':
            signature = headers.get('X-Hub-Signature-256')
            if not signature:
                return False
            
            expected_signature = 'sha256=' + hmac.new(
                secret_key.encode('utf-8'),
                payload.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            return hmac.compare_digest(signature, expected_signature)
        
        # Stripe signature verification
        elif source == 'stripe':
            signature = headers.get('Stripe-Signature')
            if not signature:
                return False
            
            # Parse Stripe signature format: t=timestamp,v1=signature
            signature_elements = dict(item.split('=') for item in signature.split(',') if '=' in item)
            timestamp = signature_elements.get('t')
            stripe_signature = signature_elements.get('v1')
            
            if not timestamp or not stripe_signature:
                return False
            
            # Create the signed payload (timestamp + payload)
            signed_payload = timestamp + '.' + payload
            
            # Compute expected signature
            expected_signature = hmac.new(
                secret_key.encode('utf-8'),
                signed_payload.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            # Verify signature and timestamp (prevent replay attacks)
            timestamp_valid = abs(int(timestamp) - int(datetime.utcnow().timestamp())) < 300  # 5 minutes
            
            return hmac.compare_digest(stripe_signature, expected_signature) and timestamp_valid
        
        # Generic HMAC verification
        else:
            signature = headers.get('X-Signature') or headers.get('X-Hub-Signature')
            if not signature:
                return False
            
            expected_signature = hmac.new(
                secret_key.encode('utf-8'),
                payload.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            # Handle different signature formats
            if signature.startswith('sha256='):
                signature = signature[7:]
            
            return hmac.compare_digest(signature, expected_signature)
            
    except Exception as e:
        logger.error(f"Error verifying webhook signature for {source}: {str(e)}")
        return False