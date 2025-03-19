from flask import Blueprint, render_template
from flask_login import current_user

faq_bp = Blueprint('faq', __name__, url_prefix='/faq')

@faq_bp.route('/')
def index():
    """Display the main FAQ page with categorized questions and answers."""
    # FAQ categories with questions and answers
    faq_categories = [
        {
            'title': 'Account Management',
            'items': [
                {
                    'question': 'How do I reset my password?',
                    'answer': 'Click on the "Forgot Password" link on the login page. You will receive an email with instructions to reset your password.'
                },
                {
                    'question': 'How can I update my profile information?',
                    'answer': 'Go to your account settings page by clicking on your username in the top right corner and select "Profile". From there, you can update your information.'
                },
                {
                    'question': 'Is my data secure?',
                    'answer': 'Yes, we use industry-standard encryption and security measures to protect your data. Your password is securely hashed and we never store it in plain text.'
                }
            ]
        },
        {
            'title': 'Clients',
            'items': [
                {
                    'question': 'How do I add a new client?',
                    'answer': 'Navigate to the Clients section and click the "Add Client" button. Fill in the required information and save.'
                },
                {
                    'question': 'Can I import client data from other platforms?',
                    'answer': 'Currently, we don\'t support direct imports. However, you can manually add clients to the system.'
                },
                {
                    'question': 'How do I delete a client?',
                    'answer': 'Go to the client\'s detail page and click the "Delete" button. Note that this will also delete all projects and invoices associated with the client.'
                }
            ]
        },
        {
            'title': 'Projects',
            'items': [
                {
                    'question': 'How do I track time for a project?',
                    'answer': 'Navigate to the project detail page and use the "Add Time Entry" button to record time. You can specify the start and end time, or use the timer feature.'
                },
                {
                    'question': 'Can I set up recurring tasks?',
                    'answer': 'Yes, when creating or editing a task, you can set it to repeat at specific intervals (daily, weekly, monthly).'
                },
                {
                    'question': 'How do I mark a project as complete?',
                    'answer': 'On the project detail page, click the "Complete Project" button or change the project status to "Completed" in the edit form.'
                }
            ]
        },
        {
            'title': 'Invoices',
            'items': [
                {
                    'question': 'How are invoice numbers generated?',
                    'answer': 'Invoice numbers are automatically generated with a unique sequential identifier. You can customize the format in your account settings.'
                },
                {
                    'question': 'Can I customize invoice templates?',
                    'answer': 'Yes, you can customize the look and feel of your invoices from the Settings page. You can add your logo, change colors, and modify the layout.'
                },
                {
                    'question': 'How do I send invoices to clients?',
                    'answer': 'After creating an invoice, click the "Send" button to email it directly to your client, or download the PDF to send it manually.'
                },
                {
                    'question': 'Can I accept online payments?',
                    'answer': 'Not yet, but we\'re working on integrating payment gateways for direct online payments. This feature will be available in a future update.'
                }
            ]
        },
        {
            'title': 'Technical Support',
            'items': [
                {
                    'question': 'What should I do if I encounter an error?',
                    'answer': 'Try refreshing the page first. If the error persists, please contact our support team with a screenshot and description of what you were doing when the error occurred.'
                },
                {
                    'question': 'Is there a mobile app available?',
                    'answer': 'We don\'t have a dedicated mobile app yet, but our website is fully responsive and works well on mobile devices.'
                },
                {
                    'question': 'Can I export my data?',
                    'answer': 'Yes, you can export client, project, and invoice data in CSV or Excel format from the respective sections.'
                }
            ]
        }
    ]
    
    return render_template('faq/index.html', faq_categories=faq_categories, title='Frequently Asked Questions')