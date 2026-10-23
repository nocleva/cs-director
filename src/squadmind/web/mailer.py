"""E-posta gönderimi (SMTP): doğrulama ve şifre sıfırlama bağlantıları için.

Ayarlı değilse (`configured` yanlış) bu özellikler arayüzde gizlenir ve uç noktalar 404 verir.
Güvenlik: gönderilen metin (içinde imzalı bağlantı var) HİÇBİR yere günlüğe yazılmaz.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

log = logging.getLogger("squadmind.web.mailer")


class MailError(Exception):
    """E-posta gönderilemedi (ayrıntı yalnızca günlüğe)."""


@dataclass
class Mailer:
    host: str
    port: int
    user: str
    password: str
    sender: str
    timeout: float = 15.0

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)

    def send(self, to: str, subject: str, body: str) -> None:
        """Düz metin e-posta yollar; 465 = SSL, diğer portlarda STARTTLS (25 hariç)."""
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(body)
        try:
            if self.port == 465:
                smtp: smtplib.SMTP = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout,
                                                       context=ssl.create_default_context())
            else:
                smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            with smtp:
                if self.port != 465 and self.port != 25:
                    smtp.starttls(context=ssl.create_default_context())
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            # Metin / bağlantı günlüğe girmez; sadece hata türü
            log.warning("E-posta gönderilemedi: %s", type(exc).__name__)
            raise MailError(type(exc).__name__) from exc
