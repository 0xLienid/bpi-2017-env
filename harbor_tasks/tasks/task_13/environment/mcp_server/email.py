"""
Email system: EmailServer, EmailInbox, and Email data class.

The EmailServer owns pointers to inboxes (address -> inbox).
Each EmailInbox tracks messages and read/unread state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class Email:
    message_id: str
    to: str
    from_addr: str
    subject: str
    body: str
    timestamp: str
    in_reply_to: Optional[str] = None


class EmailInbox:
    """An inbox belonging to a single email address."""

    def __init__(self, address: str, server: EmailServer):
        self.address = address
        self.server = server
        self.messages: List[Email] = []
        self._read_ids: set = set()

    def receive(self, email: Email):
        self.messages.append(email)

    def check_inbox(self) -> dict:
        """Return high-level view of messages grouped by read/unread."""
        unread = []
        read = []
        for msg in self.messages:
            summary = {
                "message_id": msg.message_id,
                "from": msg.from_addr,
                "subject": msg.subject,
                "timestamp": msg.timestamp,
            }
            if msg.message_id in self._read_ids:
                read.append(summary)
            else:
                unread.append(summary)
        return {"unread": unread, "read": read}

    def read_email(self, message_id: str) -> Optional[dict]:
        """Read a specific email by message_id. Marks it as read."""
        for msg in self.messages:
            if msg.message_id == message_id:
                self._read_ids.add(message_id)
                return {
                    "message_id": msg.message_id,
                    "from": msg.from_addr,
                    "to": msg.to,
                    "subject": msg.subject,
                    "body": msg.body,
                    "timestamp": msg.timestamp,
                    "in_reply_to": msg.in_reply_to,
                }
        return None

    def send(self, to: str, subject: str, body: str, current_time: str) -> dict:
        """Compose and send a new email."""
        email = Email(
            message_id=str(uuid.uuid4()),
            to=to,
            from_addr=self.address,
            subject=subject,
            body=body,
            timestamp=current_time,
        )
        self.server.deliver(email)
        return {"message_id": email.message_id, "status": "sent"}

    def reply(self, message_id: str, body: str, current_time: str) -> Optional[dict]:
        """Reply to an existing email."""
        original = None
        for msg in self.messages:
            if msg.message_id == message_id:
                original = msg
                break
        if original is None:
            return None

        reply_email = Email(
            message_id=str(uuid.uuid4()),
            to=original.from_addr,
            from_addr=self.address,
            subject=f"Re: {original.subject}",
            body=body,
            timestamp=current_time,
            in_reply_to=original.message_id,
        )
        self.server.deliver(reply_email)
        return {"message_id": reply_email.message_id, "status": "sent"}


class EmailServer:
    """Central email server routing messages between inboxes."""

    def __init__(self):
        self.inboxes: Dict[str, EmailInbox] = {}

    def register_inbox(self, address: str) -> EmailInbox:
        inbox = EmailInbox(address, self)
        self.inboxes[address] = inbox
        return inbox

    def deliver(self, email: Email):
        """Deliver an email to the recipient's inbox."""
        recipient_inbox = self.inboxes.get(email.to)
        if recipient_inbox:
            recipient_inbox.receive(email)
        # Also store in sender's inbox as a "sent" copy
        sender_inbox = self.inboxes.get(email.from_addr)
        if sender_inbox and sender_inbox is not recipient_inbox:
            sender_inbox.receive(email)
