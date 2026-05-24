import imaplib
import email
import re
import time
import os
import logging

logger = logging.getLogger(__name__)

IMAP_HOST     = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT     = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER     = os.getenv("IMAP_USER", "")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "")


def fetch_roblox_email_code(timeout=60, poll_interval=5):
    """
    Attend un email de vérification Roblox et en extrait le code 6 chiffres.
    Retourne le code (str) ou None si timeout.
    """
    if not IMAP_USER or not IMAP_PASSWORD:
        logger.error("[IMAP] IMAP_USER ou IMAP_PASSWORD non configuré")
        return None

    deadline  = time.time() + timeout
    last_uid  = _get_latest_uid()

    logger.info(f"[IMAP] 📬 Attente code Roblox (timeout={timeout}s, depuis UID={last_uid})...")

    while time.time() < deadline:
        time.sleep(poll_interval)
        code = _check_inbox_for_code(since_uid=last_uid)
        if code:
            logger.info(f"[IMAP] ✅ Code trouvé: {code}")
            return code
        remaining = int(deadline - time.time())
        logger.info(f"[IMAP] ⏳ Pas encore reçu... ({remaining}s restantes)")

    logger.warning("[IMAP] ⏱ Timeout — aucun code reçu")
    return None


def _connect():
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(IMAP_USER, IMAP_PASSWORD)
    mail.select("inbox")
    return mail


def _get_latest_uid():
    try:
        mail = _connect()
        _, data = mail.uid("search", None, "ALL")
        mail.logout()
        uids = data[0].split()
        return int(uids[-1]) if uids else 0
    except Exception as e:
        logger.warning(f"[IMAP] Impossible de lire le dernier UID: {e}")
        return 0


def _check_inbox_for_code(since_uid=0):
    try:
        mail = _connect()

        # Roblox envoie depuis noreply@roblox.com
        _, data = mail.uid("search", None, 'FROM "noreply@roblox.com" UNSEEN')
        uids = data[0].split()

        for uid in reversed(uids):
            if int(uid) <= since_uid:
                continue

            _, msg_data = mail.uid("fetch", uid, "(RFC822)")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            body = _extract_body(msg)
            code = _extract_code(body)

            if code:
                mail.uid("store", uid, "+FLAGS", "\\Seen")
                mail.logout()
                return code

        mail.logout()
        return None

    except Exception as e:
        logger.error(f"[IMAP] ❌ Erreur connexion/lecture: {e}")
        return None


def _extract_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                body += part.get_payload(decode=True).decode(errors="ignore")
            elif ctype == "text/html" and not body:
                body += part.get_payload(decode=True).decode(errors="ignore")
    else:
        body = msg.get_payload(decode=True).decode(errors="ignore")
    return body


def _extract_code(text):
    """
    Cherche un code de vérification 6 chiffres dans le texte.
    Gère les formats Roblox : 'Your code is: 123456', '[123456]', bold HTML, etc.
    """
    patterns = [
        r"(?:code|verification code|verify)[^\d]{0,40}(\d{6})",
        r"\[(\d{6})\]",
        r"<b>(\d{6})</b>",
        r"<strong>(\d{6})</strong>",
        r"(?<!\d)(\d{6})(?!\d)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None
