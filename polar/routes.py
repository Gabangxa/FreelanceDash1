"""
Routes for Polar.sh subscription management.
"""
import logging
from datetime import datetime
from flask import (
    Blueprint, render_template, flash, redirect, request,
    url_for, current_app, session, jsonify
)
from flask_login import login_required, current_user
from sqlalchemy.exc import SQLAlchemyError
from app import db
from errors import handle_db_errors, UserFriendlyError
from models import User
from .polar_api import get_polar_api, PolarAPIError, is_polar_api_configured, get_webhook_url
from .models import Subscription, SubscriptionLog


logger = logging.getLogger(__name__)
bp = Blueprint('subscriptions', __name__, url_prefix='/subscriptions')


@bp.route('/')
@login_required
def index():
    """Display subscription information for the current user."""
    # Check for Polar API configuration and set api_configured flag
    api_configured = is_polar_api_configured()
    
    if not api_configured:
        logger.warning("Polar API not configured - API key is missing")
        flash("Subscription service requires API configuration. Please contact the administrator.", "warning")
    
    # Get current subscription for the user
    subscription = Subscription.query.filter_by(user_id=current_user.id).first()
    
    # Define subscription tiers and pricing for display
    subscription_tiers = [
        {
            'id': 'free',
            'name': 'Free',
            'description': 'Basic freelancing tools for getting started',
            'price_monthly': 0,
            'price_annually': 0,
            'features': [
                'Up to 3 clients',
                'Up to 5 projects',
                'Basic time tracking',
                'Standard invoicing'
            ]
        },
        {
            'id': 'professional',
            'name': 'Professional',
            'description': 'Advanced tools for professional freelancers',
            'price_monthly': 15,
            'price_annually': 150,
            'features': [
                'Unlimited clients',
                'Unlimited projects',
                'Advanced time tracking with reporting',
                'All invoice templates',
                'Custom branding',
                'Email support'
            ]
        },
        {
            'id': 'business',
            'name': 'Business',
            'description': 'Premium features for growing freelance businesses',
            'price_monthly': 30,
            'price_annually': 300,
            'features': [
                'Everything in Professional',
                'Priority support',
                'API access',
                'Team member accounts (up to 3)',
                'Custom invoice templates',
                'Advanced reporting and analytics'
            ]
        }
    ]
    
    return render_template('polar/subscription.html', 
                          subscription=subscription,
                          subscription_tiers=subscription_tiers,
                          api_configured=api_configured)


@bp.route('/checkout/<tier_id>')
@login_required
def checkout(tier_id):
    """
    Redirect to Polar.sh checkout page for the selected subscription tier.
    """
    # Validate tier_id
    valid_tiers = ['professional', 'business']
    if tier_id not in valid_tiers:
        flash('Invalid subscription tier', 'danger')
        return redirect(url_for('subscriptions.index'))
    
    # Check if Polar API is configured
    if not is_polar_api_configured():
        logger.error("Attempted checkout with unconfigured Polar API")
        flash('Subscription service requires API configuration. Please contact the administrator.', 'danger')
        return redirect(url_for('subscriptions.index'))
    
    try:
        # Prepare user data
        user_data = {
            'id': str(current_user.id),
            'email': current_user.email,
            'name': current_user.username
        }
        
        # Create success and cancel URLs
        success_url = url_for('subscriptions.checkout_success', _external=True)
        cancel_url = url_for('subscriptions.checkout_cancel', _external=True)
        
        # Create checkout session
        polar_api = get_polar_api()
        checkout_session = polar_api.create_checkout_session(
            user_data=user_data,
            tier_id=tier_id,
            success_url=success_url,
            cancel_url=cancel_url
        )
        
        # Save checkout session ID to verify later
        session['checkout_session_id'] = checkout_session['id']
        
        # Redirect to checkout URL
        return redirect(checkout_session['checkout_url'])
        
    except PolarAPIError as e:
        logger.exception("Polar checkout error")
        flash('Unable to create checkout session. Please try again later.', 'danger')
        return redirect(url_for('subscriptions.index'))


@bp.route('/checkout/success')
@login_required
@handle_db_errors
def checkout_success():
    """
    Handle successful checkout.
    """
    # Get session_id from Polar.sh redirect
    session_id = request.args.get('session_id')
    
    # Verify session ID exists
    if not session_id:
        flash('Invalid checkout session', 'danger')
        return redirect(url_for('subscriptions.index'))
    
    # Check if Polar API is configured
    if not is_polar_api_configured():
        logger.error("Attempted to complete checkout with unconfigured Polar API")
        flash('Subscription service requires API configuration. Please contact the administrator.', 'danger')
        return redirect(url_for('subscriptions.index'))
    
    # Verify session ID matches what we started with
    if session_id != session.get('checkout_session_id'):
        flash('Invalid checkout session', 'danger')
        return redirect(url_for('subscriptions.index'))
    
    try:
        # Get subscription details from Polar.sh API
        polar_api = get_polar_api()
        subscription_data = polar_api.get_checkout_session(session_id)
        
        # Validate that we have a subscription_id in the data
        if not subscription_data.get('subscription_id'):
            logger.error("Missing subscription_id in checkout data")
            flash('Missing subscription details. Please try again or contact support.', 'danger')
            return redirect(url_for('subscriptions.index'))
            
        # Create or update subscription in database
        subscription = Subscription.query.filter_by(user_id=current_user.id).first()
        
        if subscription:
            # Update existing subscription
            subscription.polar_subscription_id = subscription_data['subscription_id']
            subscription.tier_id = subscription_data['tier_id']
            subscription.tier_name = subscription_data['tier_name']
            subscription.status = 'active'
            subscription.amount = subscription_data['amount']
            subscription.currency = subscription_data['currency']
            subscription.billing_interval = subscription_data['interval']
            subscription.start_date = subscription_data['start_date']
            subscription.end_date = subscription_data.get('end_date')
            
            # Log the upgrade event
            subscription_log = SubscriptionLog(
                user_id=current_user.id,
                subscription_id=subscription.id,
                event_type='upgraded',
                details=subscription_data
            )
            db.session.add(subscription_log)
        else:
            # Create new subscription
            subscription = Subscription(
                user_id=current_user.id,
                polar_subscription_id=subscription_data['subscription_id'],
                tier_id=subscription_data['tier_id'],
                tier_name=subscription_data['tier_name'],
                status='active',
                amount=subscription_data['amount'],
                currency=subscription_data['currency'],
                billing_interval=subscription_data['interval'],
                start_date=subscription_data['start_date'],
                end_date=subscription_data.get('end_date')
            )
            db.session.add(subscription)
            
            # Log the creation event
            subscription_log = SubscriptionLog(
                user_id=current_user.id,
                event_type='created',
                details=subscription_data
            )
            db.session.add(subscription_log)
        
        db.session.commit()
        
        # Clear checkout session from session
        session.pop('checkout_session_id', None)
        
        flash('Your subscription has been activated successfully!', 'success')
        return redirect(url_for('subscriptions.index'))
        
    except PolarAPIError as e:
        logger.exception("Polar subscription activation error")
        db.session.rollback()
        flash('Unable to activate subscription. Please contact support.', 'danger')
        return redirect(url_for('subscriptions.index'))


@bp.route('/checkout/cancel')
@login_required
def checkout_cancel():
    """
    Handle cancelled checkout.
    """
    # Clear checkout session from session
    session.pop('checkout_session_id', None)
    
    flash('Subscription checkout was cancelled', 'info')
    return redirect(url_for('subscriptions.index'))


@bp.route('/webhook-url')
@login_required
def webhook_url():
    """
    Display the webhook URL that should be configured in Polar.sh.
    Only accessible to logged-in users to avoid exposing sensitive setup information.
    """
    webhook_url = get_webhook_url()
    
    return render_template(
        'polar/webhook_url.html',
        webhook_url=webhook_url,
        current_url=request.url_root
    )

@bp.route('/webhook', methods=['POST'])
@handle_db_errors
def webhook():
    """
    Webhook endpoint for Polar.sh subscription events.
    This endpoint receives webhook events from Polar.sh and processes them.
    """
    if not is_polar_api_configured():
        logger.error("Received webhook but Polar API is not configured")
        return jsonify({"error": "API not configured"}), 500
    
    # Get the webhook event data
    event_data = request.json
    
    if not event_data:
        logger.error("Empty webhook payload received")
        return jsonify({"error": "Empty payload"}), 400
    
    # Log the webhook event type
    event_type = event_data.get('type')
    logger.info(f"Received Polar webhook event: {event_type}")
    
    # Process different event types
    if event_type == 'subscription.created':
        process_subscription_created(event_data)
    elif event_type == 'subscription.updated':
        process_subscription_updated(event_data)
    elif event_type == 'subscription.cancelled':
        process_subscription_cancelled(event_data)
    elif event_type == 'subscription.payment_failed':
        process_subscription_payment_failed(event_data)
    else:
        logger.warning(f"Unhandled webhook event type: {event_type}")
        
    # Always return 200 OK to acknowledge receipt
    return jsonify({"status": "success"}), 200

def process_subscription_created(event_data):
    """Process a subscription.created webhook event."""
    try:
        subscription_id = event_data.get('subscription_id')
        user_id = event_data.get('user_data', {}).get('id')
        
        if not subscription_id or not user_id:
            logger.error("Missing required data in subscription.created event")
            return
        
        # Convert user_id to integer
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            logger.error(f"Invalid user_id in webhook: {user_id}")
            return
        
        # Check if user exists
        user = User.query.get(user_id)
        if not user:
            logger.error(f"User not found for webhook: {user_id}")
            return
        
        # Get subscription details from Polar API
        polar_api = get_polar_api()
        subscription_data = polar_api.get_subscription(subscription_id)
        
        # Create or update subscription in database
        subscription = Subscription.query.filter_by(user_id=user_id).first()
        if subscription:
            # Update existing subscription
            subscription.polar_subscription_id = subscription_id
            subscription.tier_id = subscription_data.get('tier_id')
            subscription.tier_name = subscription_data.get('tier_name')
            subscription.status = 'active'
            subscription.amount = subscription_data.get('amount')
            subscription.currency = subscription_data.get('currency')
            subscription.billing_interval = subscription_data.get('interval')
            subscription.start_date = subscription_data.get('start_date')
            subscription.end_date = subscription_data.get('end_date')
        else:
            # Create new subscription
            subscription = Subscription(
                user_id=user_id,
                polar_subscription_id=subscription_id,
                tier_id=subscription_data.get('tier_id'),
                tier_name=subscription_data.get('tier_name'),
                status='active',
                amount=subscription_data.get('amount'),
                currency=subscription_data.get('currency'),
                billing_interval=subscription_data.get('interval'),
                start_date=subscription_data.get('start_date'),
                end_date=subscription_data.get('end_date')
            )
            db.session.add(subscription)
        
        # Add log entry
        subscription_log = SubscriptionLog(
            user_id=user_id,
            subscription_id=subscription.id if subscription.id else None,
            event_type='webhook_created',
            details=event_data
        )
        db.session.add(subscription_log)
        db.session.commit()
        
        logger.info(f"Processed subscription.created webhook for user {user_id}")
    except (SQLAlchemyError, KeyError, ValueError) as e:
        db.session.rollback()
        logger.exception("Error processing subscription.created webhook")
        raise

def process_subscription_updated(event_data):
    """Process a subscription.updated webhook event."""
    try:
        subscription_id = event_data.get('subscription_id')
        user_id = event_data.get('user_data', {}).get('id')
        
        if not subscription_id or not user_id:
            logger.error("Missing required data in subscription.updated event")
            return
        
        # Convert user_id to integer
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            logger.error(f"Invalid user_id in webhook: {user_id}")
            return
        
        # Check if user exists
        user = User.query.get(user_id)
        if not user:
            logger.error(f"User not found for webhook: {user_id}")
            return
        
        # Get subscription from database
        subscription = Subscription.query.filter_by(
            user_id=user_id, 
            polar_subscription_id=subscription_id
        ).first()
        
        if not subscription:
            logger.error(f"Subscription not found for webhook: {subscription_id}")
            return
        
        # Get updated subscription details from Polar API
        polar_api = get_polar_api()
        subscription_data = polar_api.get_subscription(subscription_id)
        
        # Update subscription in database
        subscription.tier_id = subscription_data.get('tier_id', subscription.tier_id)
        subscription.tier_name = subscription_data.get('tier_name', subscription.tier_name)
        subscription.status = subscription_data.get('status', subscription.status)
        subscription.amount = subscription_data.get('amount', subscription.amount)
        subscription.currency = subscription_data.get('currency', subscription.currency)
        subscription.billing_interval = subscription_data.get('interval', subscription.billing_interval)
        subscription.start_date = subscription_data.get('start_date', subscription.start_date)
        subscription.end_date = subscription_data.get('end_date', subscription.end_date)
        
        # Add log entry
        subscription_log = SubscriptionLog(
            user_id=user_id,
            subscription_id=subscription.id,
            event_type='webhook_updated',
            details=event_data
        )
        db.session.add(subscription_log)
        db.session.commit()
        
        logger.info(f"Processed subscription.updated webhook for user {user_id}")
    except (SQLAlchemyError, KeyError, ValueError) as e:
        db.session.rollback()
        logger.exception("Error processing subscription.updated webhook")
        raise

def process_subscription_cancelled(event_data):
    """Process a subscription.cancelled webhook event."""
    try:
        subscription_id = event_data.get('subscription_id')
        user_id = event_data.get('user_data', {}).get('id')
        
        if not subscription_id or not user_id:
            logger.error("Missing required data in subscription.cancelled event")
            return
        
        # Convert user_id to integer
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            logger.error(f"Invalid user_id in webhook: {user_id}")
            return
        
        # Get subscription from database
        subscription = Subscription.query.filter_by(
            user_id=user_id, 
            polar_subscription_id=subscription_id
        ).first()
        
        if not subscription:
            logger.error(f"Subscription not found for webhook: {subscription_id}")
            return
        
        # Update subscription status
        subscription.status = 'cancelled'
        subscription.cancel_at = datetime.utcnow()
        
        # Add log entry
        subscription_log = SubscriptionLog(
            user_id=user_id,
            subscription_id=subscription.id,
            event_type='webhook_cancelled',
            details=event_data
        )
        db.session.add(subscription_log)
        db.session.commit()
        
        logger.info(f"Processed subscription.cancelled webhook for user {user_id}")
    except (SQLAlchemyError, KeyError, ValueError) as e:
        db.session.rollback()
        logger.exception("Error processing subscription.cancelled webhook")
        raise

def process_subscription_payment_failed(event_data):
    """Process a subscription.payment_failed webhook event."""
    try:
        subscription_id = event_data.get('subscription_id')
        user_id = event_data.get('user_data', {}).get('id')
        
        if not subscription_id or not user_id:
            logger.error("Missing required data in subscription.payment_failed event")
            return
        
        # Convert user_id to integer
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            logger.error(f"Invalid user_id in webhook: {user_id}")
            return
        
        # Get subscription from database
        subscription = Subscription.query.filter_by(
            user_id=user_id, 
            polar_subscription_id=subscription_id
        ).first()
        
        if not subscription:
            logger.error(f"Subscription not found for webhook: {subscription_id}")
            return
        
        # Update subscription status
        subscription.status = 'payment_failed'
        
        # Add log entry
        subscription_log = SubscriptionLog(
            user_id=user_id,
            subscription_id=subscription.id,
            event_type='webhook_payment_failed',
            details=event_data
        )
        db.session.add(subscription_log)
        db.session.commit()
        
        logger.info(f"Processed subscription.payment_failed webhook for user {user_id}")
    except (SQLAlchemyError, KeyError, ValueError) as e:
        db.session.rollback()
        logger.exception("Error processing subscription.payment_failed webhook")
        raise


@bp.route('/cancel', methods=['POST'])
@login_required
@handle_db_errors
def cancel_subscription():
    """
    Cancel current subscription.
    """
    subscription = Subscription.query.filter_by(user_id=current_user.id).first()
    
    if not subscription or subscription.status != 'active':
        flash('No active subscription to cancel', 'warning')
        return redirect(url_for('subscriptions.index'))
    
    # Check if Polar API is configured
    if not is_polar_api_configured():
        logger.error("Attempted to cancel subscription with unconfigured Polar API")
        flash('Subscription service requires API configuration. Please contact the administrator.', 'danger')
        return redirect(url_for('subscriptions.index'))
    
    try:
        # Cancel subscription via Polar.sh API
        polar_api = get_polar_api()
        result = polar_api.cancel_subscription(subscription.polar_subscription_id)
        
        # Update subscription in database
        subscription.status = 'cancelled'
        subscription.cancel_at = result.get('cancel_at')
        
        # Log the cancellation event
        subscription_log = SubscriptionLog(
            user_id=current_user.id,
            subscription_id=subscription.id,
            event_type='cancelled',
            details=result
        )
        db.session.add(subscription_log)
        db.session.commit()
        
        flash('Your subscription has been cancelled', 'success')
    except PolarAPIError as e:
        logger.exception("Polar subscription cancellation error")
        db.session.rollback()
        flash('Unable to cancel subscription. Please try again later.', 'danger')
    
    return redirect(url_for('subscriptions.index'))