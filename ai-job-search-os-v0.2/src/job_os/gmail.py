from __future__ import annotations

import base64
import os
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def credential_paths(
    credentials_path: str | None = None, token_path: str | None = None
) -> tuple[str, str]:
    credentials_path = (
        credentials_path
        or os.getenv("GMAIL_CREDENTIALS_PATH")
        or os.getenv("GMAIL_CREDENTIALS")
        or "credentials.json"
    )
    token_path = (
        token_path
        or os.getenv("GMAIL_TOKEN_PATH")
        or os.getenv("GMAIL_TOKEN")
        or "token.json"
    )
    return credentials_path, token_path


def gmail_service(credentials_path: str | None = None, token_path: str | None = None):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    credentials_path, token_path = credential_paths(credentials_path, token_path)
    creds = None
    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_path).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def body_parts(payload: dict) -> tuple[str, str]:
    text = html = ""
    stack = [payload]
    while stack:
        part = stack.pop()
        stack.extend(part.get("parts", []) or [])
        mime = part.get("mimeType")
        body = part.get("body", {})
        if body.get("attachmentId") or part.get("filename"):
            continue
        data = body.get("data")
        if not data:
            continue
        if mime == "text/plain":
            text += _decode(data)
        elif mime == "text/html":
            html += _decode(data)
    return text, html


def list_messages(service, query: str, max_results: int = 25):
    result = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    return result.get("messages", [])


def get_message(service, message_id: str) -> dict:
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    text, html = body_parts(msg.get("payload", {}))
    msg["text"] = text
    msg["html"] = html
    return msg
