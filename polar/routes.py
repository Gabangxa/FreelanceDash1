"""
Routes for Polar.sh subscription management.
"""
import logging
from flask import (
    Blueprint, render_template, flash, redirect, request,
    url_for, current_app, session
)
from flask_login import login_required, current_user
from app import db
from errors import handle_db_errors, UserFriendlyError
from .polar_api import get_polar_api, PolarAPIError, is_polar_api_configured
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
        logger.error(f"Polar checkout error: {str(e)}")
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
        logger.error(f"Polar subscription activation error: {str(e)}")
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
        logger.error(f"Polar subscription cancellation error: {str(e)}")
        db.session.rollback()
        flash('Unable to cancel subscription. Please try again later.', 'danger')
    
    return redirect(url_for('subscriptions.index'))