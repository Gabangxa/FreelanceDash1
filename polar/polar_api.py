"""
Polar.sh API integration for Freelancer Suite.
Handles subscription management and payment processing.
"""
import os
import requests
import logging
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
        
        try:
            response = self.session.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Polar API error: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_data = e.response.json()
                    logger.error(f"Polar API error details: {error_data}")
                except ValueError:
                    logger.error(f"Polar API error status: {e.response.status_code}")
            raise PolarAPIError(str(e)) from e
    
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
        data = {
            "user": user_data,
            "tier_id": tier_id,
            "success_url": success_url,
            "cancel_url": cancel_url
        }
        return self._make_request("post", "checkout/session", json=data)
    
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
        return self._make_request("get", f"checkout/session/{session_id}")


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
    """
    global _polar_api_instance
    if _polar_api_instance is None:
        _polar_api_instance = PolarAPI()
    return _polar_api_instance