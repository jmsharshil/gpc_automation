from django.shortcuts import get_object_or_404
from rest_framework import generics, status, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView
from django.conf import settings
from .models import Chat, Message, UserOpenAISetting
from .serializers import ChatSerializer, MessageSerializer, UserOpenAISettingSerializer
from .permissions import IsOwner
import openai
from django.utils.text import Truncator
import io
import PyPDF2
import docx
import openpyxl
import logging
from django.core.files.uploadedfile import InMemoryUploadedFile, TemporaryUploadedFile
import pandas as pd
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django.db import transaction
from django.utils import timezone
from django.http import HttpResponse
from django.core.files.base import ContentFile
from django.http import StreamingHttpResponse

logger = logging.getLogger(__name__)

CHARACTER_LIMIT = 8000  # You increased this

openai.api_key = getattr(settings, 'OPENAI_API_KEY', None)


# ====================== HELPER: EXTRACT TEXT FROM EXCEL ======================
def extract_text_from_excel_bytes(file_bytes, filename='file'):
    import io, pandas as pd, openpyxl
    parts = []

    try:
        ext = (filename or '').lower()
        engine = None
        if ext.endswith('.xls'):
            engine = 'xlrd'
        elif ext.endswith('.xlsb'):
            engine = 'pyxlsb'

        excel_data = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, engine=engine)
        for sheet_name, df in excel_data.items():
            df_small = df.iloc[:20, :20]
            header = "\t".join(map(str, df_small.columns))
            rows = ["\t".join("" if pd.isna(v) else str(v) for v in row) for row in df_small.itertuples(index=False, name=None)]
            parts.append(f"--- Sheet: {sheet_name} ---\n{header}\n" + ("\n".join(rows) if rows else "(no rows)"))
        text = "\n\n".join(parts)
        if len(text) > CHARACTER_LIMIT:
            text = text[:CHARACTER_LIMIT-3] + '...'
        if text.strip():
            return text
    except Exception:
        pass

    try:
        wb = openpyxl.load_workbook(filename=io.BytesIO(file_bytes), read_only=True, data_only=True)
        for ws in wb.worksheets:
            lines, max_rows, max_cols = [], 50, 50
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= max_rows: break
                lines.append("\t".join("" if c is None else str(c) for c in (row[:max_cols])))
            parts.append(f"--- Sheet: {ws.title} ---\n" + ("\n".join(lines) if lines else "(no rows)"))
        text = "\n\n".join(parts)
        if len(text) > CHARACTER_LIMIT:
            text = text[:CHARACTER_LIMIT-3] + '...'
        if text.strip():
            return text
    except Exception:
        pass

    try:
        df = pd.read_csv(io.BytesIO(file_bytes), nrows=200)
        header = "\t".join(map(str, df.columns))
        rows = ["\t".join("" if pd.isna(v) else str(v) for v in r) for r in df.head(50).itertuples(index=False, name=None)]
        text = f"--- CSV ---\n{header}\n" + ("\n".join(rows) if rows else "(no rows)")
        if len(text) > CHARACTER_LIMIT:
            text = text[:CHARACTER_LIMIT-3] + '...'
        return text
    except Exception:
        pass

    try:
        return file_bytes[:10240].decode('utf-8')
    except Exception:
        try:
            return file_bytes[:10240].decode('latin-1')
        except Exception:
            return ''


# ====================== CHAT LIST / CREATE ======================
class ChatListCreateAPIView(generics.ListCreateAPIView):
    serializer_class = ChatSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Chat.objects.filter(owner=self.request.user).order_by('-updated_at')

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


# ====================== CHAT RETRIEVE ======================
class ChatRetrieveAPIView(generics.RetrieveAPIView):
    serializer_class = ChatSerializer
    permission_classes = [permissions.IsAuthenticated, IsOwner]
    queryset = Chat.objects.all()


# ====================== MESSAGE LIST =====================, UserOpenAISettingAPIView ======================
class MessageListAPIView(generics.ListAPIView):
    serializer_class = MessageSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        chat_id = self.kwargs['chat_pk']
        chat = get_object_or_404(Chat, pk=chat_id, owner=self.request.user)
        return chat.messages.all().order_by('created_at')

def trim_messages_by_chars(messages, max_chars=12000):
    total = 0
    trimmed = []

    # Start from latest messages
    for msg in reversed(messages):
        content = msg.get("content", "")
        total += len(content)
        if total > max_chars:
            break
        trimmed.insert(0, msg)

    return trimmed


# ====================== SEND MESSAGE (MAIN) ======================
class SendMessageAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, chat_pk):
        global CHARACTER_LIMIT
        user = request.user
        chat = get_object_or_404(Chat, pk=chat_pk, owner=user)

        user_text = request.data.get('content', '').strip()
        attachment = request.FILES.get('attachment')

        if not user_text and not attachment:
            return Response({'error': 'content or attachment is required'}, status=status.HTTP_400_BAD_REQUEST)

        # ✅ ADD THIS BLOCK HERE
        MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB

        if attachment and attachment.size > MAX_UPLOAD_SIZE:
            return Response(
                {"error": "File too large. Max allowed size is 10MB."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # --- Extract attachment text ---
        attachment_name = ''
        attachment_content_type = ''
        extracted_text = ''
        try:
            if attachment:
                attachment_name = getattr(attachment, 'name', '')
                attachment_content_type = getattr(attachment, 'content_type', '')

                file_bytes = attachment.read()

                # Rebuild clean file object for saving (IMPORTANT for Azure)
                attachment = ContentFile(file_bytes, name=attachment_name)

                lower = (attachment_name or '').lower()
                ct = (attachment_content_type or '').lower()

                def _is_spreadsheet(lower_name: str, content_type: str) -> bool:
                    if lower_name.endswith(('.xlsx', '.xls', '.xlsm', '.xlsb', '.csv')):
                        return True
                    spreadsheet_mimes = {
                        'application/vnd.ms-excel',
                        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        'application/vnd.ms-excel.sheet.macroenabled.12',
                        'application/vnd.ms-excel.sheet.binary.macroenabled.12',
                        'text/csv',
                    }
                    if content_type in spreadsheet_mimes:
                        return True
                    if content_type.startswith('application/vnd.openxmlformats-officedocument.spreadsheetml'):
                        return True
                    if 'spreadsheet' in content_type or content_type.endswith('/csv'):
                        return True
                    return False

                def _is_word(lower_name: str, content_type: str) -> bool:
                    if lower_name.endswith(('.docx', '.doc')):
                        return True
                    word_mimes = {
                        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                        'application/msword',
                    }
                    return content_type in word_mimes or content_type.startswith(
                        'application/vnd.openxmlformats-officedocument.wordprocessingml'
                    )

                if lower.endswith('.txt') or (ct and ct.startswith('text/') and 'csv' not in ct):
                    try:
                        extracted_text = file_bytes.decode('utf-8')
                    except Exception:
                        try:
                            extracted_text = file_bytes.decode('latin-1')
                        except Exception:
                            extracted_text = ''

                elif lower.endswith('.pdf') or ct == 'application/pdf':
                    try:
                        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
                        pages = []
                        MAX_PDF_PAGES = 5  # VERY IMPORTANT (fast + safe)

                        for i, p in enumerate(reader.pages):
                            if i >= MAX_PDF_PAGES:
                                break
                            try:
                                pages.append(p.extract_text() or '')
                            except Exception:
                                pages.append('')
                        extracted_text = "\n\n".join(pages)
                    except Exception as e:
                        logger.exception("PDF parse failed: %s", e)
                        extracted_text = ''

                elif _is_spreadsheet(lower, ct):
                    try:
                        extracted_text = extract_text_from_excel_bytes(file_bytes, attachment_name)
                    except Exception as e:
                        logger.exception("Excel parse failed: %s", e)
                        extracted_text = ''

                elif _is_word(lower, ct):
                    try:
                        d = docx.Document(io.BytesIO(file_bytes))
                        extracted_text = "\n\n".join(p.text for p in d.paragraphs if p.text)
                    except Exception as e:
                        logger.exception("DOCX parse failed: %s", e)
                        extracted_text = ''

                else:
                    try:
                        extracted_text = file_bytes[:10240].decode('utf-8')
                    except Exception:
                        try:
                            extracted_text = file_bytes[:10240].decode('latin-1')
                        except Exception:
                            extracted_text = ''
        except Exception:
            logger.exception("Attachment extraction failed")
            extracted_text = ''

        MAX_CHAR_FOR_GPT = 6000  # ~2000 tokens safe

        if extracted_text:
            extracted_text = extracted_text[:MAX_CHAR_FOR_GPT]

        # --- Save user message ---
        user_msg = Message.objects.create(
            chat=chat,
            role='user',
            content=user_text or (f"[Uploaded file: {attachment_name}]"),
            attachment=attachment if attachment else None,
            attachment_name=attachment_name,
            attachment_content_type=attachment_content_type,
        )

        # --- Build messages_payload ---
        system_prompt = chat.system_prompt or ''
        messages_payload = []
        if system_prompt:
            messages_payload.append({'role': 'system', 'content': system_prompt})

        recent_messages = chat.messages.all().order_by('-created_at')[:6][::-1]
        for m in recent_messages:
            base_content = m.content or ''
            if m.attachment:
                att_note = f"[Attachment: {m.attachment_name} | content-type: {m.attachment_content_type}]"
                if m.pk == user_msg.pk and extracted_text:
                    att_note = att_note + "\n\n" + extracted_text
                messages_payload.append({'role': m.role, 'content': (base_content + "\n\n" + att_note).strip()})
            else:
                messages_payload.append({'role': m.role, 'content': base_content})

        if attachment and extracted_text and not any(
            (msg.get('role') == 'user' and f"[Attachment content from {attachment_name}]" in msg.get('content', ''))
            for msg in messages_payload
        ):
            messages_payload.append({
                'role': 'user',
                'content': f"[Attachment content from {attachment_name}]\n\n{extracted_text}"
            })

        logger.debug("OpenAI messages_payload keys: %s", [m.get('role') for m in messages_payload])
        messages_payload = trim_messages_by_chars(messages_payload, max_chars=12000)

        # --- Model, temp, max_tokens ---
        user_setting = getattr(user, 'openai_setting', None)
        model = (
            request.data.get('model') or
            (user_setting.default_model if user_setting else None) or
            "gpt-5"  # <<<--- FORCED TO GPT-5
        )
        max_tokens = int(request.data.get('max_tokens') or getattr(user_setting, 'max_tokens', 8000))
        temperature = float(request.data.get('temperature') or getattr(user_setting, 'temperature', 0.7))

        logger.debug("Sending to OpenAI → model=%s temp=%s max_output_tokens=%s", model, temperature, max_tokens)

        try:
            from openai import OpenAI
            client = OpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))

            def event_stream():
                full_text = ""
                usage_data = {}

                with client.responses.stream(
                    model=model,
                    input=messages_payload,
                    instructions=system_prompt or None,
                    tools=[{"type": "web_search"}],
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                ) as stream:

                    for event in stream:
                        if event.type == "response.output_text.delta":
                            delta = event.delta
                            full_text += delta
                            yield delta  # 🔥 send chunk immediately

                        elif event.type == "response.completed":
                            response = stream.get_final_response()
                            if hasattr(response, "usage") and response.usage:
                                usage_data = (
                                    response.usage.model_dump()
                                    if hasattr(response.usage, "model_dump")
                                    else {}
                                )

                # ✅ Save after stream finishes
                Message.objects.create(
                    chat=chat,
                    role='assistant',
                    content=full_text,
                    metadata={'openai_usage': usage_data}
                )

            return StreamingHttpResponse(
                event_stream(),
                content_type="text/plain",
            )

        except Exception as e:
            logger.exception("OpenAI streaming failed: %s", e)
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

# ====================== EDIT & RESEND ======================
class EditAndResendAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def patch(self, request, chat_pk, message_pk):
        user = request.user
        chat = get_object_or_404(Chat, pk=chat_pk, owner=user)

        new_content = (request.data.get('content') or request.GET.get('content') or '').strip()
        if not new_content:
            return Response({'error': 'content is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            msg = Message.objects.get(pk=message_pk, chat=chat)
        except Message.DoesNotExist:
            return Response({'detail': 'No Message matches the given query.'}, status=status.HTTP_404_NOT_FOUND)

        if msg.role == 'assistant':
            user_msg = chat.messages.filter(role='user', created_at__lt=msg.created_at).order_by('-created_at').first()
            if not user_msg:
                return Response({'detail': 'No preceding user message found.'}, status=status.HTTP_404_NOT_FOUND)
            msg = user_msg
        elif msg.role != 'user':
            return Response({'detail': 'Message must be a user message.'}, status=status.HTTP_400_BAD_REQUEST)

        original_content = msg.content or ''
        with transaction.atomic():
            meta = msg.metadata or {}
            meta.setdefault('edits', []).append({
                'original': original_content,
                'edited_at': timezone.now().isoformat(),
                'editor_id': user.id,
            })
            msg.content = new_content
            msg.metadata = meta
            msg.edited = True
            msg.edited_at = timezone.now()
            msg.save()

            # Rebuild payload
            system_prompt = chat.system_prompt or ''
            messages_payload = []
            if system_prompt:
                messages_payload.append({'role': 'system', 'content': system_prompt})

            recent_messages = chat.messages.all().order_by('-created_at')[:20][::-1]
            for m in recent_messages:
                content = m.content or ''
                if m.attachment:
                    att_note = f"[Attachment: {m.attachment_name} | content-type: {m.attachment_content_type}]"
                    messages_payload.append({'role': m.role, 'content': (content + "\n\n" + att_note).strip()})
                else:
                    messages_payload.append({'role': m.role, 'content': content})

            # Model & params
            user_setting = getattr(user, 'openai_setting', None)
            model = (
                request.data.get('model') or
                (user_setting.default_model if user_setting else None) or
                "gpt-5"
            )
            max_tokens = int(request.data.get('max_tokens') or getattr(user_setting, 'max_tokens', 8000))
            temperature = float(request.data.get('temperature') or getattr(user_setting, 'temperature', 0.7))

            logger.debug("Edit call → model=%s temp=%s max=%s", model, temperature, max_tokens)

            try:
                from openai import OpenAI
                client = OpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))

                def event_stream():
                    full_text = ""
                    usage_data = {}

                    with client.responses.stream(
                        model=model,
                        input=messages_payload,
                        instructions=system_prompt or None,
                        tools=[{"type": "web_search"}],
                        temperature=temperature,
                        max_output_tokens=max_tokens,
                    ) as stream:

                        for event in stream:
                            if event.type == "response.output_text.delta":
                                delta = event.delta
                                full_text += delta
                                yield delta

                            elif event.type == "response.completed":
                                response = stream.get_final_response()
                                if hasattr(response, "usage") and response.usage:
                                    usage_data = (
                                        response.usage.model_dump()
                                        if hasattr(response.usage, "model_dump")
                                        else {}
                                    )

                    # ✅ Save after stream completes
                    Message.objects.create(
                        chat=chat,
                        role='assistant',
                        content=full_text,
                        metadata={
                            "openai_usage": usage_data,
                            "replaced_by_edit_of": msg.pk
                        }
                    )

                    # After stream ends → save full assistant message
                    assistant_msg = Message.objects.create(
                        chat=chat,
                        role='assistant',
                        content=full_text,
                    )

                return StreamingHttpResponse(
                    event_stream(),
                    content_type="text/plain"
                )

            except Exception as e:
                logger.exception("OpenAI streaming failed: %s", e)
                return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
                usage = resp.usage.model_dump() if hasattr(resp.usage, 'model_dump') else {}

                assistant_msg = chat.messages.filter(role='assistant', created_at__gt=msg.created_at).order_by('created_at').first()
                if assistant_msg:
                    assistant_msg.content = assistant_text
                    meta = assistant_msg.metadata or {}
                    meta['openai_usage'] = usage
                    meta['replaced_by_edit_of'] = msg.pk
                    assistant_msg.metadata = meta
                    assistant_msg.updated_at = timezone.now()
                    assistant_msg.save()
                else:
                    assistant_msg = Message.objects.create(
                        chat=chat,
                        role='assistant',
                        content=assistant_text,
                        metadata={'openai_usage': usage, 'replaced_by_edit_of': msg.pk}
                    )

                serializer = MessageSerializer(assistant_msg, context={'request': request})
                return Response(serializer.data, status=status.HTTP_200_OK)

            except Exception as e:
                logger.exception("OpenAI edit call failed: %s", e)
                return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)


# ====================== DELETE CHAT ======================
class DeleteChatAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, chat_pk):
        user = request.user
        chat = get_object_or_404(Chat, pk=chat_pk, owner=user)
        chat.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ====================== USER OPENAI SETTINGS ======================
class UserOpenAISettingAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        setting, _ = UserOpenAISetting.objects.get_or_create(user=request.user)
        return Response(UserOpenAISettingSerializer(setting).data)

    def post(self, request):
        setting, _ = UserOpenAISetting.objects.get_or_create(user=request.user)
        serializer = UserOpenAISettingSerializer(setting, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)
class ExportLatestAssistantMessageAPIView(APIView):
    # permission_classes = [IsAuthenticated, IsOwner]

    def post(self, request, chat_pk):
        chat = get_object_or_404(Chat, pk=chat_pk, owner=request.user)

        assistant_msg = (
            chat.messages
            .filter(role='assistant')
            .order_by('-created_at')
            .first()
        )

        if not assistant_msg or not assistant_msg.content:
            return Response(
                {"error": "No assistant response found"},
                status=400
            )

        # Create DOCX in memory
        doc = docx.Document()
        doc.add_heading("GPT Response", level=1)
        doc.add_paragraph(assistant_msg.content)

        buffer = io.BytesIO()
        doc.save(buffer)
        buffer.seek(0)

        response = HttpResponse(
            buffer.getvalue(),
            content_type=(
                'application/vnd.openxmlformats-officedocument.'
                'wordprocessingml.document'
            )
        )
        response['Content-Disposition'] = (
            f'attachment; filename="chat_{chat.pk}_latest_response.docx"'
        )

        return response    