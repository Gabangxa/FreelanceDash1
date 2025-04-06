"""
Polar.sh API integration for Freelancer Suite.
Handles subscription management and payment processing.
"""
import os
import requests
import logging
import time
from datetime import datetime
from urllib.parse import urljoin
from flask import current_app

logger = logging.getLogger(__name__)

class PolarAPI:
    """Client for the Polar.sh API."""
    
    BASE_URL = "https://api.polar.sh/v1/"
    
    def __init__(self, api_key=None):
        """
        Initialize the Polar API client.
        
        Args:
            api_key: The Polar API key. If None, it will be loaded from environment.
        """
        self.api_key = api_key or os.environ.get("POLAR_API_KEY")
        if not self.api_key:
            raise ValueError("Polar API key is required")
        
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        })
    
    def _make_request(self, method, endpoint, **kwargs):
        """
        Make a request to the Polar API.
        
        Args:
            method: HTTP method (get, post, etc.)
            endpoint: API endpoint to call
            **kwargs: Additional arguments to pass to requests
            
        Returns:
            Response data as JSON
            
        Raises:
            PolarAPIError: If the API returns an error
        """
        url = urljoin(self.BASE_URL, endpoint)
        
        # Check if API key is present
        if not self.api_key:
            logger.error("No Polar API key available")
            raise PolarAPIError("Polar API key is missing. Please configure it in the environment variables.")
        
        try:
            # Add timeout to prevent hanging requests
            timeout = kwargs.pop('timeout', 10)
            
            # Log the request being made (without sensitive data)
            masked_kwargs = kwargs.copy()
            if 'json' in masked_kwargs and isinstance(masked_kwargs['json'], dict):
                if 'user' in masked_kwargs['json']:
                    masked_kwargs['json']['user'] = "***REDACTED***"
            
            logger.info(f"Making Polar API request: {method.upper()} {endpoint}")
            
            # Make the actual request
            response = self.session.request(method, url, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.Timeout:
            logger.error(f"Polar API timeout: {method.upper()} {endpoint}")
            raise PolarAPIError("The request to Polar API timed out. Please try again later.")
            
        except requests.exceptions.ConnectionError:
            logger.error(f"Polar API connection error: {method.upper()} {endpoint}")
            raise PolarAPIError("Could not connect to Polar API. Please check your internet connection.")
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Polar API error: {str(e)}")
            
            # Try to extract more details from the response
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_data = e.response.json()
                    error_message = error_data.get('message', str(e))
                    logger.error(f"Polar API error details: {error_data}")
                    raise PolarAPIError(f"Polar API error: {error_message}")
                except ValueError:
                    status_code = e.response.status_code
                    logger.error(f"Polar API error status: {status_code}")
                    
                    if status_code == 401:
                        raise PolarAPIError("Authentication failed. Please check your Polar API key.")
                    elif status_code == 403:
                        raise PolarAPIError("You don't have permission to access this resource.")
                    elif status_code >= 500:
                        raise PolarAPIError("Polar API is currently experiencing issues. Please try again later.")
                    
            raise PolarAPIError(f"Error communicating with Polar API: {str(e)}") from e
    
    # Subscription Management
    
    def get_subscription_tiers(self):
        """
        Get available subscription tiers.
        
        Returns:
            List of subscription tiers
        """
        return self._make_request("get", "subscription/tiers")
    
    def create_subscription(self, user_data, tier_id, payment_method_id=None):
        """
        Create a new subscription for a user.
        
        Args:
            user_data: User information dictionary
            tier_id: ID of the subscription tier
            payment_method_id: Optional payment method ID
            
        Returns:
            Subscription details
        """
        data = {
            "user": user_data,
            "tier_id": tier_id
        }
        if payment_method_id:
            data["payment_method_id"] = payment_method_id
            
        return self._make_request("post", "subscriptions", json=data)
    
    def get_subscription(self, subscription_id):
        """
        Get subscription details.
        
        Args:
            subscription_id: ID of the subscription
            
        Returns:
            Subscription details
        """
        return self._make_request("get", f"subscriptions/{subscription_id}")
    
    def cancel_subscription(self, subscription_id):
        """
        Cancel a subscription.
        
        Args:
            subscription_id: ID of the subscription
            
        Returns:
            Cancellation confirmation
        """
        logger.info(f"Cancelling subscription: {subscription_id}")
        return self._make_request("post", f"subscriptions/{subscription_id}/cancel")
    
    def upgrade_subscription(self, subscription_id, new_tier_id):
        """
        Upgrade a subscription to a new tier.
        
        Args:
            subscription_id: ID of the subscription
            new_tier_id: ID of the new subscription tier
            
        Returns:
            Updated subscription details
        """
        data = {"tier_id": new_tier_id}
        return self._make_request("post", f"subscriptions/{subscription_id}/upgrade", json=data)
    
    # Payment Processing
    
    def create_checkout_session(self, user_data, tier_id, success_url, cancel_url):
        """
        Create a checkout session for subscription payment.
        
        Args:
            user_data: User information dictionary
            tier_id: ID of the subscription tier
            success_url: URL to redirect after successful payment
            cancel_url: URL to redirect if payment is cancelled
            
        Returns:
            Checkout session details with redirect URL
        """
        # Log the API request for debugging
        logger.info(f"Creating checkout session for tier {tier_id}")
        
        # Prepare the request data for the Polar.sh API
        data = {
            "user": user_data,
            "tier_id": tier_id,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "mode": "subscription"
        }
        
        # Make the actual API request to Polar's checkout endpoint
        try:
            response = self._make_request("post", "checkout/session", json=data)
            logger.info(f"Successfully created checkout session: {response.get('id')}")
            return response
        except Exception as e:
            logger.error(f"Failed to create checkout session: {str(e)}")
            raise PolarAPIError(f"Unable to create checkout session: {str(e)}")
    
    def get_payment_methods(self, user_id):
        """
        Get available payment methods for a user.
        
        Args:
            user_id: ID of the user
            
        Returns:
            List of payment methods
        """
        return self._make_request("get", f"users/{user_id}/payment-methods")
        
    def get_checkout_session(self, session_id):
        """
        Get details of a checkout session.
        
        Args:
            session_id: ID of the checkout session
            
        Returns:
            Checkout session details
        """
        logger.info(f"Getting checkout session details for: {session_id}")
        
        try:
            # Call the Polar.sh API to get session details
            return self._make_request("get", f"checkout/session/{session_id}")
        except Exception as e:
            logger.error(f"Error getting checkout session: {str(e)}")
            raise PolarAPIError(f"Unable to retrieve checkout session information: {str(e)}")


class PolarAPIError(Exception):
    """Exception raised for Polar API errors."""
    pass


# Singleton instance
_polar_api_instance = None

def get_polar_api():
    """
    Get or create the Polar API client singleton.
    
    Returns:
        PolarAPI instance
        
    Raises:
        PolarAPIError: If the API key is missing or invalid
    """
    global _polar_api_instance
    
    if _polar_api_instance is None:
        # Check for API key
        api_key = os.environ.get("POLAR_API_KEY")
        
        # Require a valid API key
        if not api_key:
            logger.error("No POLAR_API_KEY found in environment. Please configure this API key.")
            raise PolarAPIError("Polar API key is required. Please contact the administrator to set up your API key.")
            
        try:
            _polar_api_instance = PolarAPI()
        except ValueError as e:
            # Convert ValueError to PolarAPIError for consistent error handling
            logger.error(f"Failed to initialize Polar API client: {str(e)}")
            raise PolarAPIError(f"Failed to initialize Polar API: {str(e)}")
            
    return _polar_api_instance

def is_polar_api_configured():
    """
    Check if the Polar API is properly configured with the required API key.
    
    Returns:
        bool: True if the API is configured, False otherwise
    """
    try:
        # Check if the POLAR_API_KEY exists in environment
        api_key = os.environ.get("POLAR_API_KEY")
        return bool(api_key)
    except Exception:
        return False

def get_webhook_url():
    """
    Get the webhook URL for Polar.sh subscription events.
    
    This is the URL that Polar will send webhook events to.
    When configuring your Polar.sh webhook, you should use this URL.
    
    Returns:
        str: The full webhook URL
    """
    try:
        # Get the application's external URL from configuration or build it
        from flask import url_for, current_app
        
        # Generate the webhook URL using url_for
        webhook_url = url_for('subscriptions.webhook', _external=True)
        return webhook_url
    except Exception as e:
        logger.error(f"Error generating Polar webhook URL: {str(e)}")
        # Fallback to a placeholder - this should be replaced with actual URL
        return "https://yourapp.replit.app/subscriptions/webhook"