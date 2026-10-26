import logging
import os
from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)

try:
    from weasyprint import HTML
    WEASYPRINT_AVAILABLE = True
except (ImportError, OSError) as e:
    logger.warning(f"WeasyPrint could not be loaded: {e}. PDF generation will be mocked.")
    WEASYPRINT_AVAILABLE = False

# Basic template since we don't have a full HTML file
DEFAULT_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Rental Agreement</title>
    <style>
        body { font-family: sans-serif; line-height: 1.6; margin: 40px; }
        h1 { text-align: center; }
        .details { margin-top: 30px; }
        .details th { text-align: left; padding-right: 20px; }
    </style>
</head>
<body>
    <h1>Rental Agreement</h1>
    <p>This agreement is made for <strong>{{ property_name }}</strong> between:</p>
    <p><strong>Owner:</strong> {{ owner_name }}<br>
    <strong>Tenant:</strong> {{ tenant_name }}</p>
    
    <div class="details">
        <table>
            <tr><th>Bed/Room:</th><td>{{ bed_name }} ({{ room_name }})</td></tr>
            <tr><th>Rent:</th><td>₹{{ rent_amount }} per month</td></tr>
            <tr><th>Deposit:</th><td>₹{{ deposit_amount }}</td></tr>
            <tr><th>Check-in Date:</th><td>{{ check_in_date }}</td></tr>
            <tr><th>Expected Checkout:</th><td>{{ expected_checkout }}</td></tr>
            <tr><th>Notice Period:</th><td>{{ notice_period }} days</td></tr>
            <tr><th>Lock-in Period:</th><td>{{ lock_in }} months</td></tr>
        </table>
    </div>
    
    <div style="margin-top: 40px;">
        <h3>House Rules & Gate Timing</h3>
        <p>{{ house_rules }}</p>
        <p>Gate Timing: {{ gate_timing }}</p>
    </div>
    
    <div style="margin-top: 80px; display: flex; justify-content: space-between;">
        <div>_______________________<br>Owner Signature</div>
        <div>_______________________<br>Tenant Signature</div>
    </div>
</body>
</html>
"""

def generate_rental_agreement(context: dict, output_path: str):
    from jinja2 import Template
    template = Template(DEFAULT_TEMPLATE)
    html_out = template.render(**context)
    
    if not WEASYPRINT_AVAILABLE:
        logger.warning(f"Mocking PDF generation to {output_path}")
        with open(output_path, "w") as f:
            f.write("Mock PDF Content (WeasyPrint not available)")
        return
        
    try:
        HTML(string=html_out).write_pdf(output_path)
    except Exception as e:
        logger.error(f"WeasyPrint failed: {e}")
        raise e
