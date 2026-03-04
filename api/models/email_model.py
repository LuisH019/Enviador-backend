"""
EmailModel - Modelo de mensagem de email.
Reutilizado fielmente de Enviador_de_Email/models/email_model.py
"""
import os
import mimetypes
import re
from html import unescape
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders


class EmailModel:
    def __init__(self, sender_address: str, recipient_address: str, subject: str, body: str, attachments: list = None):
        """
        Class model for an email message.

        Args:
            sender_address (str): Email address of the sender.
            recipient_address (str): Email address of the recipient.
            subject (str): Subject of the email.
            body (str): Body content of the email (HTML format).
            attachments (list): List of file paths to attach to the email.
        """
        self.sender_address = sender_address
        self.recipient_address = recipient_address
        self.subject = subject
        self.body = body
        self.attachments = attachments or []

    @staticmethod
    def _html_to_text(value: str) -> str:
        if value is None:
            return ''

        text = str(value)
        text = re.sub(r'<\s*br\s*/?\s*>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'</\s*p\s*>', '\n\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', '', text)
        text = unescape(text)
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    @classmethod
    def _normalize_subject(cls, subject: str) -> str:
        plain_subject = '' if subject is None else str(subject)
        plain_subject = plain_subject.replace('\r', ' ').replace('\n', ' ')
        plain_subject = re.sub(r'\s+', ' ', plain_subject).strip()
        return plain_subject

    def create_message(self) -> EmailMessage:
        """
        Create an email message from the model's attributes.
        """
        print(f"[DEBUG] Creating message for {self.recipient_address} with {len(self.attachments)} attachments")
        
        subject_text = self._normalize_subject(self.subject)
        body_html = self.body or ''
        body_plain = self._html_to_text(body_html)

        # If there are attachments, use MIMEMultipart mixed + alternative
        if self.attachments:
            msg = MIMEMultipart()
            msg['Subject'] = subject_text
            msg['From'] = self.sender_address
            msg['To'] = self.recipient_address

            body_part = MIMEMultipart('alternative')
            body_part.attach(MIMEText(body_plain or '', 'plain', 'utf-8'))
            body_part.attach(MIMEText(body_html, 'html', 'utf-8'))
            msg.attach(body_part)
            
            # Adicionar anexos
            for att_idx, attachment in enumerate(self.attachments, 1):
                # If attachment is a file-like object (e.g., Django UploadedFile), read bytes and attach
                try:
                    if hasattr(attachment, 'read'):
                        # Resetar file pointer antes de ler
                        if hasattr(attachment, 'seek'):
                            try:
                                attachment.seek(0)
                            except Exception:
                                pass
                        
                        filename = getattr(attachment, 'name', 'attachment')
                        
                        data = attachment.read()
                        if data is None or len(data) == 0:
                            continue
                        
                        mime_type, _ = mimetypes.guess_type(filename)
                        if mime_type is None:
                            mime_type = 'application/octet-stream'
                        
                        main_type, sub_type = mime_type.split('/', 1)

                        part = MIMEBase(main_type, sub_type)
                        part.set_payload(data)
                        encoders.encode_base64(part)
                        part.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(filename)}"')
                        msg.attach(part)
                        print(f"[DEBUG] Attached file: {filename}, size: {len(data)}")
                        continue
                except Exception:
                    # Fall back to path-based handling
                    pass

                # Fallback: treat as filesystem path
                attachment_path = str(attachment)
                if os.path.isfile(attachment_path):
                    self._add_attachment(msg, attachment_path)
        else:
            # Sem anexos: incluir versões text/plain e text/html
            msg = EmailMessage()
            msg['Subject'] = subject_text
            msg['From'] = self.sender_address
            msg['To'] = self.recipient_address
            msg.set_content(body_plain or '', subtype='plain', charset='utf-8')
            msg.add_alternative(body_html, subtype='html', charset='utf-8')
        
        return msg
    
    def _add_attachment(self, msg: MIMEMultipart, file_path: str):
        """
        Adiciona um anexo à mensagem.
        
        Args:
            msg: Mensagem MIMEMultipart
            file_path: Caminho para o arquivo anexo
        """
        print(f"[DEBUG] Adding attachment from path: {file_path}")
        try:
            # Detectar tipo MIME do arquivo
            mime_type, _ = mimetypes.guess_type(file_path)
            if mime_type is None:
                mime_type = 'application/octet-stream'
            
            main_type, sub_type = mime_type.split('/', 1)
            
            # Ler arquivo
            with open(file_path, 'rb') as attachment:
                data = attachment.read()
                
                part = MIMEBase(main_type, sub_type)
                part.set_payload(data)
            
            # Codificar em base64
            encoders.encode_base64(part)
            
            # Adicionar cabeçalhos do anexo
            filename = os.path.basename(file_path)
            part.add_header(
                'Content-Disposition',
                f'attachment; filename= {filename}'
            )
            
            # Anexar à mensagem
            msg.attach(part)
            
        except Exception:
            print(f"[DEBUG] Failed to add attachment: {file_path}")
            pass
