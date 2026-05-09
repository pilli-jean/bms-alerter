# Usage
# set env variables EMAIL_USER, EMAIL_PASSWORD, EMAIL_TO
# python3 send-email.py --subject "Subject" --body "Body"

import smtplib
import os
from email.mime.text import MIMEText
import argparse


def send_mail(subject,body):
    sender = os.getenv("EMAIL_USER")
    password = os.getenv("EMAIL_PASSWORD")
    receiver = os.getenv("EMAIL_TO")

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = receiver    

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--subject",required=True)
    p.add_argument("--body",required=True)
    args = p.parse_args()
    send_mail(args.subject,args.body)