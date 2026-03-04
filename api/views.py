import json
import re
from django.http import JsonResponse, HttpResponseBadRequest
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from .whatsapp_api import WhatsAppAPI
from .webhook_handler import log_webhook_event, parse_webhook_event
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.http import JsonResponse
import threading
from apps.auth_app.models import AccountSettings, GmailSender, WhatsAppSender, WhatsAppTemplate
import logging

# job manager for background runs
from .services import job_manager


logger = logging.getLogger(__name__)


def _is_masked_secret(value) -> bool:
    """Retorna True para placeholders mascarados comuns enviados pelo frontend."""
    if value is None:
        return False

    text = str(value).strip()
    if not text:
        return False

    # Ex.: ********, ••••••••, ●●●●●●●●
    allowed_mask_chars = {'*', '•', '●', '·', '•'}
    if len(text) >= 4 and all(ch in allowed_mask_chars for ch in text):
        return True

    # Placeholders textuais comuns
    lowered = text.lower()
    return lowered in {
        'masked',
        'hidden',
        'not_changed',
        'unchanged',
        'keep',
        'keep_current',
    }


def _sanitize_email_credentials(payload: dict) -> dict:
    """Normaliza credenciais de e-mail para evitar uso acidental de placeholders."""
    app_password = payload.get('app_password')

    if isinstance(app_password, str):
        payload['app_password'] = app_password.strip()
        app_password = payload['app_password']

    if _is_masked_secret(app_password):
        payload['app_password'] = ''

    return payload


def _apply_account_settings_fallback(payload: dict, user):
    """Aplica fallback de credenciais por canal usando AccountSettings do usuário."""
    channel = payload.get('channel', 'email')
    if channel not in ('email', 'whatsapp'):
        return payload

    try:
        account_settings = AccountSettings.objects.get(user=user)
    except AccountSettings.DoesNotExist:
        return payload

    if channel == 'email':
        if not payload.get('email_sender') and account_settings.gmail_sender_email:
            payload['email_sender'] = account_settings.gmail_sender_email
        if not payload.get('app_password') and account_settings.gmail_app_password:
            payload['app_password'] = account_settings.gmail_app_password
        return payload

    if not payload.get('whatsapp_access_token') and account_settings.whatsapp_access_token:
        payload['whatsapp_access_token'] = account_settings.whatsapp_access_token
    if not payload.get('whatsapp_phone_number_id') and account_settings.whatsapp_phone_number_id:
        payload['whatsapp_phone_number_id'] = account_settings.whatsapp_phone_number_id
    if not payload.get('whatsapp_business_id') and account_settings.whatsapp_business_id:
        payload['whatsapp_business_id'] = account_settings.whatsapp_business_id
    if not payload.get('phone_number') and account_settings.whatsapp_phone_number:
        payload['phone_number'] = account_settings.whatsapp_phone_number
    if not payload.get('whatsapp_templates') and account_settings.whatsapp_templates:
        payload['whatsapp_templates'] = account_settings.whatsapp_templates
    return payload


def _extract_template_variables(template_content: str):
    return list(dict.fromkeys(re.findall(r"\{([^{}]+)\}", template_content or "")))


def _resolve_whatsapp_template_messages(payload: dict, user):
    sender_id = payload.get('whatsapp_sender_id')
    if not sender_id:
        return None, JsonResponse({'error': 'whatsapp_sender_id is required for WhatsApp channel'}, status=400)

    template_title = payload.get('whatsapp_template_title')
    if not template_title:
        return None, JsonResponse({'error': 'whatsapp_template_title is required for WhatsApp channel'}, status=400)

    contact_column = payload.get('contact_column')
    if not contact_column:
        return None, JsonResponse({'error': 'contact_column is required'}, status=400)

    rows = payload.get('rows', [])
    if not isinstance(rows, list) or not rows:
        return None, JsonResponse({'error': 'rows is required'}, status=400)

    try:
        sender = WhatsAppSender.objects.get(id=sender_id, user=user)
    except WhatsAppSender.DoesNotExist:
        return None, JsonResponse({'error': 'WhatsApp sender not found'}, status=404)

    try:
        template = WhatsAppTemplate.objects.get(sender=sender, title=template_title)
    except WhatsAppTemplate.DoesNotExist:
        return None, JsonResponse({'error': 'WhatsApp template not found for this sender'}, status=404)

    variables = _extract_template_variables(template.content)
    mapping_list = payload.get('whatsapp_template_variables', [])
    mapping_by_variable = {}
    if isinstance(mapping_list, list):
        for item in mapping_list:
            if isinstance(item, dict) and item.get('variable'):
                mapping_by_variable[item.get('variable')] = item

    for variable in variables:
        mapping = mapping_by_variable.get(variable)
        if not mapping:
            return None, JsonResponse(
                {'error': f'Missing mapping for variable "{variable}"', 'variable': variable},
                status=400
            )

        mode = mapping.get('mode')
        if mode not in ('column', 'fixed'):
            return None, JsonResponse(
                {'error': f'Invalid mode for variable "{variable}". Use "column" or "fixed"'},
                status=400
            )

        if mode == 'column' and not mapping.get('column'):
            return None, JsonResponse(
                {'error': f'column is required for variable "{variable}" when mode=column'},
                status=400
            )

        if mode == 'fixed' and (mapping.get('value') is None or str(mapping.get('value')).strip() == ''):
            return None, JsonResponse(
                {'error': f'value is required for variable "{variable}" when mode=fixed'},
                status=400
            )

    resolved_messages = []
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            return None, JsonResponse({'error': f'Row {idx} is invalid. Expected object.'}, status=400)

        recipient_value = row.get(contact_column)
        if recipient_value is None or str(recipient_value).strip() == '':
            return None, JsonResponse({'error': f'Missing recipient in row {idx} for column "{contact_column}"'}, status=400)

        message = template.content
        for variable in variables:
            mapping = mapping_by_variable[variable]
            if mapping.get('mode') == 'fixed':
                value = str(mapping.get('value', ''))
            else:
                source_column = mapping.get('column')
                if source_column not in row:
                    return None, JsonResponse(
                        {'error': f'Missing column "{source_column}" for variable "{variable}" in row {idx}'},
                        status=400
                    )
                value = str(row.get(source_column, ''))
                if value.strip() == '':
                    return None, JsonResponse(
                        {'error': f'Empty value in column "{source_column}" for variable "{variable}" in row {idx}'},
                        status=400
                    )

            message = message.replace(f'{{{variable}}}', value)

        resolved_messages.append({
            'recipient': str(recipient_value).strip(),
            'message': message,
        })

    payload['phone_number'] = sender.phone_number
    payload['whatsapp_access_token'] = sender.get_access_token()
    payload['whatsapp_phone_number_id'] = sender.phone_number_id
    payload['whatsapp_business_id'] = sender.business_id
    payload['resolved_messages'] = resolved_messages
    payload['message'] = template.content

    return payload, None


def _resolve_email_sender_payload(payload: dict, user):
    sender_id = payload.get('sender_id')
    if not sender_id:
        return payload, None

    try:
        gmail_sender = GmailSender.objects.get(id=sender_id, user=user)
    except GmailSender.DoesNotExist:
        return None, JsonResponse({'error': 'Gmail sender not found'}, status=404)

    payload['email_sender'] = gmail_sender.sender_email
    try:
        # Com sender_id, a fonte de verdade da credencial é o remetente salvo.
        # Isso evita uso acidental de fallback (AccountSettings) desatualizado.
        payload['app_password'] = gmail_sender.get_app_password()
    except Exception:
        return None, JsonResponse({'error': 'Unable to decrypt app password for sender_id'}, status=400)

    return payload, None


@require_http_methods(["GET"])
@permission_classes([AllowAny])
def health_view(request):
    return JsonResponse({"status": "ok"})


@require_http_methods(["POST"])
@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def send_email_view(request):
    """
    Endpoint para enviar emails.
    
    POST /api/send-email/
    
    {
        "email_sender": "seu.email@gmail.com",
        "app_password": "sua_senha_de_app",
        "subject": "Assunto do email",
        "message": "<html>Corpo do email</html>",
        "rows": [
            {"email": "destino1@example.com", "file": "caminho_arquivo_1"},
            {"email": "destino2@example.com", "file": "caminho_arquivo_2"}
        ],
        "contact_column": "email",
        "file_column": "file",
        "attach_to_all": false
    }
    """
    user = request.user
    
    # Parse payload from JSON or multipart form data
    if request.content_type and request.content_type.startswith('multipart/'):
        payload_raw = request.POST.get('payload')
        if not payload_raw:
            return HttpResponseBadRequest('Missing payload in multipart form')
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            return HttpResponseBadRequest('Invalid JSON in payload field')
        
        files_bytes = {}
        for key in request.FILES:
            file_list = request.FILES.getlist(key)
            files_bytes[key] = []
            for f in file_list:
                f.seek(0)
                content = f.read()
                files_bytes[key].append({
                    'name': f.name,
                    'content': content,
                    'size': len(content)
                })
        
        attachment_names = [f.name for f in request.FILES.getlist(list(request.FILES.keys())[0]) if request.FILES]
        payload['attachment_names'] = attachment_names
        payload['_files'] = files_bytes
    else:
        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return HttpResponseBadRequest('Invalid JSON')

    payload['channel'] = 'email'
    payload = _sanitize_email_credentials(payload)
    payload = _apply_account_settings_fallback(payload, request.user)
    payload = _sanitize_email_credentials(payload)
    payload, sender_error = _resolve_email_sender_payload(payload, request.user)
    if sender_error is not None:
        return sender_error

    credential_source = 'sender_id' if payload.get('sender_id') else 'payload_or_account_settings'
    logger.info(
        "[SEND_EMAIL] credential_source=%s sender_id=%s email_sender=%s app_password_len=%s",
        credential_source,
        payload.get('sender_id'),
        payload.get('email_sender'),
        len(payload.get('app_password') or ''),
    )

    # Validate email-specific required fields
    email_sender = payload.get('email_sender')
    app_password = payload.get('app_password')
    subject = payload.get('subject', '')
    message = payload.get('message', '')
    rows = payload.get('rows', [])
    contact_column = payload.get('contact_column', '')
    
    if not email_sender:
        return JsonResponse({"error": "email_sender is required"}, status=400)
    if not app_password:
        return JsonResponse({"error": "app_password is required"}, status=400)
    if not subject:
        return JsonResponse({"error": "subject is required"}, status=400)
    if not message:
        return JsonResponse({"error": "message is required"}, status=400)
    if not contact_column:
        return JsonResponse({"error": "contact_column is required"}, status=400)
    if not rows:
        return JsonResponse({"error": "rows is required"}, status=400)
    
    
    # Call email service
    try:
        from .services.email_service import EmailService
        response = EmailService.send(payload)
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        return JsonResponse({
            "error": f"Erro ao processar envio: {str(e)}",
            "status": "error"
        }, status=500)
    
    return JsonResponse(response, status=202)


@require_http_methods(["POST"])
@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def send_whatsapp_view(request):
    """
    Endpoint para enviar mensagens WhatsApp.
    
    POST /api/send-whatsapp/
    
    {
        "phone_number": "5541997393566",
        "message": "Sua mensagem aqui",
        "rows": [
            {"telefone": "5541999999999"},
            {"telefone": "5541888888888"}
        ],
        "contact_column": "telefone",
        "file_column": null,
        "attach_to_all": false
    }
    """
    origin = request.META.get('HTTP_ORIGIN', 'NO_ORIGIN')
    user = request.user
    
    # Parse payload from JSON or multipart form data
    if request.content_type and request.content_type.startswith('multipart/'):
        payload_raw = request.POST.get('payload')
        if not payload_raw:
            return HttpResponseBadRequest('Missing payload in multipart form')
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            return HttpResponseBadRequest('Invalid JSON in payload field')
        
        files_bytes = {}
        for key in request.FILES:
            file_list = request.FILES.getlist(key)
            files_bytes[key] = []
            for f in file_list:
                f.seek(0)
                content = f.read()
                files_bytes[key].append({
                    'name': f.name,
                    'content': content,
                    'size': len(content)
                })
        
        payload['_files'] = files_bytes
    else:
        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return HttpResponseBadRequest('Invalid JSON')

    payload['channel'] = 'whatsapp'
    payload = _apply_account_settings_fallback(payload, request.user)

    if payload.get('whatsapp_sender_id') or payload.get('whatsapp_template_title'):
        prepared_payload, error_response = _resolve_whatsapp_template_messages(payload, request.user)
        if error_response is not None:
            return error_response
        payload = prepared_payload
        from .services.whatsapp_service import WhatsAppService
        response = WhatsAppService.send(payload)
        return JsonResponse(response, status=202)

    # Validate WhatsApp-specific required fields
    phone_number = payload.get('phone_number')
    message = payload.get('message', '')
    rows = payload.get('rows', [])
    contact_column = payload.get('contact_column', '')
    
    if not phone_number:
        return JsonResponse({"error": "phone_number is required"}, status=400)
    if not message:
        return JsonResponse({"error": "message is required"}, status=400)
    if not contact_column:
        return JsonResponse({"error": "contact_column is required"}, status=400)
    if not rows:
        return JsonResponse({"error": "rows is required"}, status=400)
    
    # Call WhatsApp service
    try:
        from .services.whatsapp_service import WhatsAppService
        response = WhatsAppService.send(payload)
    except Exception as e:
        return JsonResponse({
            "error": f"Erro ao processar envio: {str(e)}",
            "status": "error"
        }, status=500)
    
    return JsonResponse(response, status=202)


@require_http_methods(["POST"])
@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def send_view(request):
    """
    Endpoint genérico para enviar emails ou mensagens WhatsApp (DEPRECATED - usar send-email ou send-whatsapp)
    
    POST /api/send/
    
    {
        "channel": "email" ou "whatsapp",
        ...
    }
    """
    origin = request.META.get('HTTP_ORIGIN', 'NO_ORIGIN')
    user = request.user
    
    # Accept JSON or multipart/form-data (with files). If multipart, payload must be in a 'payload' form field as JSON
    if request.content_type and request.content_type.startswith('multipart/'):
        payload_raw = request.POST.get('payload')
        if not payload_raw:
            return HttpResponseBadRequest('Missing payload in multipart form')
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            return HttpResponseBadRequest('Invalid JSON in payload field')
        # Collect all uploaded files (support multiple keys / multiple files)
        files = []
        for key in request.FILES:
            file_list = request.FILES.getlist(key)
            files.extend(file_list)
        attachment_names = [f.name for f in files]
        payload['attachment_names'] = attachment_names
        payload['_files'] = request.FILES  # pass files for services if needed
    else:
        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return HttpResponseBadRequest('Invalid JSON')
    
    channel = payload.get('channel', 'email')
    if channel not in ('email', 'whatsapp'):
        return JsonResponse({'error': 'Invalid channel'}, status=400)

    payload = _apply_account_settings_fallback(payload, request.user)

    if channel == 'email':
        payload, sender_error = _resolve_email_sender_payload(payload, request.user)
        if sender_error is not None:
            return sender_error

    if channel == 'whatsapp' and (payload.get('whatsapp_sender_id') or payload.get('whatsapp_template_title')):
        prepared_payload, error_response = _resolve_whatsapp_template_messages(payload, request.user)
        if error_response is not None:
            return error_response
        payload = prepared_payload

    # Validate required fields from the new payload structure
    rows = payload.get('rows', [])
    contact_column = payload.get('contact_column', '')
    message = payload.get('message', '')
    subject = payload.get('subject', '')
    file_column = payload.get('file_column', '')
    attach_to_all = payload.get('attach_to_all', False)
    # attachment_names is set in multipart handling; use the one from payload
    if 'attachment_names' not in payload:
        payload['attachment_names'] = []

    if not contact_column:
        return JsonResponse({"error": "contact_column is required"}, status=400)
    if not message and channel == 'whatsapp' and not payload.get('resolved_messages'):
        return JsonResponse({"error": "message is required for WhatsApp"}, status=400)
    if not subject and channel == 'email':
        return JsonResponse({"error": "subject is required for Email"}, status=400)
    if not rows:
        return JsonResponse({"error": "rows is required"}, status=400)
    
    # Validate channel-specific authentication
    if channel == 'email':
        email_sender = payload.get('email_sender')
        app_password = payload.get('app_password')
        if not email_sender:
            return JsonResponse({"error": "email_sender is required for email channel"}, status=400)
        if not app_password:
            return JsonResponse({"error": "app_password is required for email channel"}, status=400)
    else:  # whatsapp
        phone_number = payload.get('phone_number')
        if not phone_number:
            return JsonResponse({"error": "phone_number is required for WhatsApp channel"}, status=400)

    # Delegate to service implementations
    from .services.email_service import EmailService
    from .services.whatsapp_service import WhatsAppService

    if channel == 'email':
        response = EmailService.send(payload)
    else:
        response = WhatsAppService.send(payload)

    
    return JsonResponse(response, status=202)



@require_http_methods(["POST"])
@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def whatsapp_test_view(request):
    """
    Endpoint para testar conexão com API do WhatsApp/Meta
    
    POST /api/whatsapp/test/
    
    Body (opcional):
    {
        "phone_number": "5541997393566",
        "template_name": "jaspers_market_plain_text_v1",
        "language_code": "en_US"
    }
    """
    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        data = {}
    
    phone_number = data.get('phone_number', '5541997393566')
    template_name = data.get('template_name', 'jaspers_market_plain_text_v1')
    language_code = data.get('language_code', 'en_US')
    
    result = WhatsAppAPI.send_template_message(
        to_number=phone_number,
        template_name=template_name,
        language_code=language_code
    )
    
    status_code = 200 if result.get('success') else 400
    return JsonResponse(result, status=status_code)


@require_http_methods(["POST"])
@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def whatsapp_webhook_view(request):
    """
    Webhook endpoint to receive WhatsApp events from Meta
    
    POST /api/whatsapp/webhook/
    
    Events:
    - messages: incoming messages
    - statuses: message delivery status updates
    """
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    



@require_http_methods(["POST"])
@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def jobs_start_view(request):
    """Start a background send job (returns job_id)."""
    # Accept JSON or multipart/form-data same as send endpoints
    if request.content_type and request.content_type.startswith('multipart/'):
        payload_raw = request.POST.get('payload')
        if not payload_raw:
            return HttpResponseBadRequest('Missing payload in multipart form')
        
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            return HttpResponseBadRequest('Invalid JSON in payload field')
        
        files_bytes = {}
        print(f"[DEBUG] request.FILES keys: {list(request.FILES.keys())}")
        print(f"[DEBUG] request.FILES type: {type(request.FILES)}")
        for key in request.FILES:
            file_list = request.FILES.getlist(key)
            print(f"[DEBUG] Processing key '{key}': {len(file_list)} file(s)")
            files_bytes[key] = []
            for f in file_list:
                print(f"[DEBUG] File object: {f}, type: {type(f)}, name: {f.name}, size: {f.size}")
                f.seek(0)
                content = f.read()
                files_bytes[key].append({
                    'name': f.name,
                    'content': content,
                    'size': len(content)
                })
        
        print(f"[DEBUG] files_bytes keys: {list(files_bytes.keys())}")
        for key in files_bytes:
            print(f"[DEBUG] files_bytes['{key}'] has {len(files_bytes[key])} items")
            for i, item in enumerate(files_bytes[key]):
                print(f"[DEBUG]   Item {i}: name={item.get('name')}, size={item.get('size')}")
        
        payload['_files'] = files_bytes
    else:
        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return HttpResponseBadRequest('Invalid JSON')

    payload = _apply_account_settings_fallback(payload, request.user)

    channel = payload.get('channel', 'email')
    if channel not in ('email', 'whatsapp'):
        return JsonResponse({'error': 'Invalid channel'}, status=400)

    if channel == 'email':
        sender_id = payload.get('sender_id')
        if sender_id:
            try:
                gmail_sender = GmailSender.objects.get(id=sender_id, user=request.user)
            except GmailSender.DoesNotExist:
                return JsonResponse({'error': 'Gmail sender not found'}, status=404)

            payload['email_sender'] = gmail_sender.sender_email
            if not payload.get('app_password'):
                try:
                    payload['app_password'] = gmail_sender.get_app_password()
                except Exception:
                    return JsonResponse({'error': 'Unable to decrypt app password for sender_id'}, status=400)

        if not payload.get('email_sender'):
            return JsonResponse({'error': 'email_sender is required for email channel'}, status=400)
        if not payload.get('app_password'):
            return JsonResponse({'error': 'app_password is required for email channel'}, status=400)
        if not payload.get('subject'):
            return JsonResponse({'error': 'subject is required for email channel'}, status=400)
        if not payload.get('rows'):
            return JsonResponse({'error': 'rows is required'}, status=400)
        if not payload.get('contact_column'):
            return JsonResponse({'error': 'contact_column is required'}, status=400)

    if channel == 'whatsapp':
        prepared_payload, error_response = _resolve_whatsapp_template_messages(payload, request.user)
        if error_response is not None:
            return error_response
        payload = prepared_payload

    owner_email = request.user.email
    
    job_id = job_manager.create_job(payload, owner_email)
    
    # start background thread
    job_manager.run_job_in_thread(job_id)

    return JsonResponse({'job_id': job_id, 'status': 'started'}, status=202)


@require_http_methods(["GET"])
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def jobs_status_view(request, job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        return JsonResponse({'error': 'job not found'}, status=404)
    # ensure only owner can see or keep simple for now
    return JsonResponse({
        'job_id': job['job_id'],
        'state': job['state'],
        'total': job['total'],
        'processed': job['processed'],
        'success': job['success'],
        'failed': job['failed'],
        'items': job['items'][-50:],
        'error': job['error'],
        'created_at': job['created_at'],
        'updated_at': job['updated_at']
    })


@require_http_methods(["POST"])
@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def jobs_cancel_view(request, job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        return JsonResponse({'error': 'job not found'}, status=404)
    job_manager.cancel_job(job_id)
    return JsonResponse({'job_id': job_id, 'status': 'canceled'})
    
    # Log the event
    log_webhook_event(data)
    
    # Parse the event
    events = parse_webhook_event(data)
    
    if events:
        print(f"[WEBHOOK] {len(events)} evento(s) processado(s)")
        for event in events:
            print(f"[WEBHOOK] Tipo: {event.get('type')}")
            if event.get('type') == 'message':
                print(f"  - De: {event.get('from')}")
                print(f"  - Mensagem: {event.get('text')}")
            elif event.get('type') == 'status_update':
                print(f"  - Status: {event.get('status')}")
                if event.get('error'):
                    print(f"  - Erro: {event['error'].get('message')}")
    
    print(f"{'='*80}\n")
    
    # Always return 200 to acknowledge receipt
    return JsonResponse({'status': 'received'}, status=200)


@require_http_methods(["GET"])
@csrf_exempt
@api_view(['GET'])
@permission_classes([AllowAny])
def whatsapp_webhook_verify_view(request):
    """
    Webhook verification endpoint
    Meta calls this to verify the webhook URL
    
    GET /api/whatsapp/webhook/?hub.mode=subscribe&hub.challenge=xxx&hub.verify_token=xxx
    """
    mode = request.GET.get('hub.mode')
    challenge = request.GET.get('hub.challenge')
    verify_token = request.GET.get('hub.verify_token')
    
    
    # You should set this in your environment
    expected_token = 'seu_token_de_verificacao_aqui'
    
    if mode == 'subscribe' and verify_token == expected_token:
        print(f"[WEBHOOK VERIFY] Token valido! Webhook verificado.")
        return JsonResponse(challenge, status=200, safe=False)
    else:
        print(f"[WEBHOOK VERIFY] Token invalido!")
        return JsonResponse({'error': 'Unauthorized'}, status=403)


@require_http_methods(["POST"])
@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def whatsapp_setup_view(request):
    """
    Setup WhatsApp phone number
    
    POST /api/whatsapp/setup/
    
    Body:
    {
        "waba_id": "your_waba_id"
    }
    """
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    
    waba_id = data.get('waba_id')
    
    if not waba_id:
        return JsonResponse({'error': 'waba_id is required'}, status=400)
    
    print(f"\n[SETUP] Iniciando configuracao do WhatsApp...")
    print(f"[SETUP] WABA ID: {waba_id}")
    
    result = WhatsAppAPI.setup_phone_number(waba_id)
    
    status_code = 200 if all(r.get('success') for r in result['steps'].values()) else 400
    return JsonResponse(result, status=status_code)
