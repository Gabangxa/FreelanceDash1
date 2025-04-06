"""
Database models for Polar.sh integration.
"""
from datetime import datetime
from app import db
from sqlalchemy import Index


class Subscription(db.Model):
    """Subscription model to store user subscription information."""
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    
    # Subscription details
    polar_subscription_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    tier_id = db.Column(db.String(50), nullable=False)
    tier_name = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default='active', index=True)
    
    # Billing details
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(3), default='USD')
    billing_interval = db.Column(db.String(20), default='month')  # month or year
    
    # Dates
    start_date = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    end_date = db.Column(db.DateTime, nullable=True)
    cancel_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationship
    user = db.relationship('User', backref=db.backref('subscription', uselist=False))
    
    # Indexes
    __table_args__ = (
        Index('idx_subscription_user_status', 'user_id', 'status'),
    )
    
    def __repr__(self):
        return f'<Subscription {self.polar_subscription_id} - {self.tier_name}>'
    
    def is_active(self):
        """Check if the subscription is active."""
        if self.status != 'active':
            return False
        
        if self.end_date and datetime.utcnow() > self.end_date:
            return False
            
        return True
        
    def get_features(self):
        """
        Get features available for this subscription tier.
        
        Returns:
            Dictionary of features and their values.
        """
        features = {}
        
        # Default features (free tier)
        features['clients_limit'] = 3
        features['projects_limit'] = 5
        features['custom_branding'] = False
        features['advanced_reporting'] = False
        features['invoice_templates'] = ['basic']
        
        # Professional tier
        if self.tier_name.lower() == 'professional':
            features['clients_limit'] = 0  # Unlimited
            features['projects_limit'] = 0  # Unlimited
            features['custom_branding'] = True
            features['advanced_reporting'] = True
            features['invoice_templates'] = ['basic', 'professional', 'elegant']
        
        # Business tier
        elif self.tier_name.lower() == 'business':
            features['clients_limit'] = 0  # Unlimited
            features['projects_limit'] = 0  # Unlimited
            features['custom_branding'] = True
            features['advanced_reporting'] = True
            features['team_members'] = 3
            features['api_access'] = True
            features['priority_support'] = True
            features['invoice_templates'] = ['basic', 'professional', 'elegant', 'premium', 'custom']
        
        return features


class SubscriptionLog(db.Model):
    """Logs of subscription events."""
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey('subscription.id'), index=True)
    
    # Event details
    event_type = db.Column(db.String(50), nullable=False, index=True)  # created, cancelled, upgraded, etc.
    details = db.Column(db.JSON)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    
    # Relationships
    user = db.relationship('User')
    subscription = db.relationship('Subscription')
    
    def __repr__(self):
        return f'<SubscriptionLog {self.event_type} - {self.timestamp}>'