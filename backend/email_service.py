import os
from datetime import datetime

import resend
from dotenv import load_dotenv

load_dotenv()

resend.api_key = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_FROM", "Placement Assistant <onboarding@resend.dev>")


def send_login_alert_email(to_email: str, user_name: str = "User", login_ip: str = "Unknown"):
    current_time = datetime.now().strftime("%d %b %Y, %I:%M %p")

    html_content = f"""
    <div style="font-family: Arial, sans-serif; line-height: 1.6;">
        <h2>Successful login to your Placement Assistant account</h2>
        <p>Hello {user_name},</p>
        <p>We detected a successful login to your account.</p>

        <p><strong>Details:</strong></p>
        <ul>
            <li>Email: {to_email}</li>
            <li>Time: {current_time}</li>
            <li>IP Address: {login_ip}</li>
        </ul>

        <p>If this was you, no action is needed.</p>
        <p>If this was not you, please change your password immediately.</p>

        <br>
        <p>Thanks,</p>
        <p>Placement Assistant</p>
    </div>
    """

    resend.Emails.send({
        "from": EMAIL_FROM,
        "to": [to_email],
        "subject": "Login alert - Placement Assistant",
        "html": html_content,
    })