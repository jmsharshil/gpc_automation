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
import openpyxl
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

logger = logging.getLogger(__name__)

CHARACTER_LIMIT = 60_000

openai.api_key = getattr(settings, 'OPENAI_API_KEY', None)

class ChatListCreateAPIView(generics.ListCreateAPIView):
    serializer_class = ChatSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Chat.objects.filter(owner=self.request.user).order_by('-updated_at')

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

class ChatRetrieveAPIView(generics.RetrieveAPIView):
    serializer_class = ChatSerializer
    permission_classes = [permissions.IsAuthenticated, IsOwner]
    queryset = Chat.objects.all()
    
class MessageListAPIView(generics.ListAPIView):
    serializer_class = MessageSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        chat_id = self.kwargs['chat_pk']
        chat = get_object_or_404(Chat, pk=chat_id, owner=self.request.user)
        return chat.messages.all().order_by('created_at')
    
class SendMessageAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, chat_pk):
        # Make sure helper can see CHARACTER_LIMIT (works even if it’s already defined at module level)
        global CHARACTER_LIMIT  # allows extract_text_from_excel_bytes to read it

        user = request.user
        chat = get_object_or_404(Chat, pk=chat_pk, owner=user)

        user_text = request.data.get('content', '').strip()
        attachment = request.FILES.get('attachment')

        if not user_text and not attachment:
            return Response({'error': 'content or attachment is required'}, status=status.HTTP_400_BAD_REQUEST)

        # --- Extract attachment text (if any) ---
        attachment_name = ''
        attachment_content_type = ''
        extracted_text = ''
        try:
            if attachment:
                attachment_name = getattr(attachment, 'name', '')
                attachment_content_type = getattr(attachment, 'content_type', '')

                # read bytes safely
                file_bytes = attachment.read()
                # reset pointer (in case Django needs it later)
                try:
                    attachment.seek(0)
                except Exception:
                    pass

                lower = (attachment_name or '').lower()
                ct = (attachment_content_type or '').lower()

                def _is_spreadsheet(lower_name: str, content_type: str) -> bool:
                    # extensions
                    if lower_name.endswith(('.xlsx', '.xls', '.xlsm', '.xlsb', '.csv')):
                        return True
                    # common and odd spreadsheet MIME types
                    spreadsheet_mimes = {
                        'application/vnd.ms-excel',
                        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        'application/vnd.ms-excel.sheet.macroenabled.12',
                        'application/vnd.ms-excel.sheet.binary.macroenabled.12',
                        'text/csv',
                    }
                    if content_type in spreadsheet_mimes:
                        return True
                    # handle weird variants like "...spreadsheetml.sheet.main+xml"
                    if content_type.startswith('application/vnd.openxmlformats-officedocument.spreadsheetml'):
                        return True
                    # generic
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

                # TXT
                if lower.endswith('.txt') or (ct and ct.startswith('text/') and 'csv' not in ct):
                    try:
                        extracted_text = file_bytes.decode('utf-8')
                    except Exception:
                        try:
                            extracted_text = file_bytes.decode('latin-1')
                        except Exception:
                            extracted_text = ''

                # PDF
                elif lower.endswith('.pdf') or ct == 'application/pdf':
                    try:
                        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
                        pages = []
                        for p in reader.pages:
                            try:
                                pages.append(p.extract_text() or '')
                            except Exception:
                                pages.append('')
                        extracted_text = "\n\n".join(pages)
                    except Exception as e:
                        logger.exception("PDF parse failed: %s", e)
                        extracted_text = ''

                # --- IMPORTANT: spreadsheets BEFORE DOCX ---
                elif _is_spreadsheet(lower, ct):
                    try:
                        extracted_text = extract_text_from_excel_bytes(file_bytes, attachment_name)
                    except Exception as e:
                        logger.exception("Excel parse failed: %s", e)
                        extracted_text = ''

                # DOCX (tightened MIME check)
                elif _is_word(lower, ct):
                    try:
                        d = docx.Document(io.BytesIO(file_bytes))
                        extracted_text = "\n\n".join(p.text for p in d.paragraphs if p.text)
                    except Exception as e:
                        logger.exception("DOCX parse failed: %s", e)
                        extracted_text = ''

                # Unknown binary → small preview
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

        # Limit extracted text size to avoid huge payloads (adjust as needed)
        if extracted_text and len(extracted_text) > CHARACTER_LIMIT:
            extracted_text = Truncator(extracted_text).chars(CHARACTER_LIMIT, truncate='...')

        # --- Save user's message and attachment (so DB holds file) ---
        user_msg = Message.objects.create(
            chat=chat,
            role='user',
            content=user_text or (f"[Uploaded file: {attachment_name}]"),
            attachment=attachment if attachment else None,
            attachment_name=attachment_name,
            attachment_content_type=attachment_content_type,
        )

        # --- Prepare messages payload including extracted text ---
        system_prompt = chat.system_prompt or ''
        messages_payload = []
        if system_prompt:
            messages_payload.append({'role': 'system', 'content': system_prompt})

        recent_messages = chat.messages.all().order_by('-created_at')[:20][::-1]
        for m in recent_messages:
            base_content = m.content or ''
            if m.attachment:
                att_note = f"[Attachment: {m.attachment_name} | content-type: {m.attachment_content_type}]"
                # attach extracted_text only for the message we just saved
                if m.pk == user_msg.pk and extracted_text:
                    att_note = att_note + "\n\n" + extracted_text
                messages_payload.append({'role': m.role, 'content': (base_content + "\n\n" + att_note).strip()})
            else:
                messages_payload.append({'role': m.role, 'content': base_content})

        # If user sent an attachment and we couldn't inject it above (edge cases), add a synthetic message
        if attachment and extracted_text and not any(
            (msg.get('role') == 'user' and f"[Attachment content from {attachment_name}]" in msg.get('content', ''))
            for msg in messages_payload
        ):
            messages_payload.append({
                'role': 'user',
                'content': f"[Attachment content from {attachment_name}]\n\n{extracted_text}"
            })

        # --- Logging for debugging: inspect what we will send to OpenAI ---
        try:
            logger.debug("OpenAI messages_payload keys: %s", [m.get('role') for m in messages_payload])
        except Exception:
            pass

        # Model + params (same as your existing logic)
        user_setting = getattr(user, 'openai_setting', None)
        model = request.data.get('model') or (user_setting.default_model if user_setting else 'gpt-4o')
        try:
            temperature = float(request.data.get('temperature') or (user_setting.temperature if user_setting else 0.2))
        except Exception:
            temperature = 0.2
        try:
            max_tokens = int(request.data.get('max_tokens') or (user_setting.max_tokens if user_setting else 1024))
        except Exception:
            max_tokens = 1024

        try:
            client = openai.OpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))
            resp = client.chat.completions.create(
                model=model,
                messages=messages_payload,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            assistant_text = resp.choices[0].message.content
            usage = resp.usage.model_dump() if hasattr(resp.usage, 'model_dump') else {}

            assistant_msg = Message.objects.create(
                chat=chat,
                role='assistant',
                content=assistant_text,
                metadata={'openai_usage': usage}
            )

            serializer = MessageSerializer(assistant_msg, context={'request': request})
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.exception("OpenAI call failed: %s", e)
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        
def extract_text_from_excel_bytes(file_bytes, filename='file'):
    import io, pandas as pd, openpyxl
    parts = []

    # Try pandas first (best for multiple sheets)
    try:
        ext = (filename or '').lower()
        engine = None
        if ext.endswith('.xls'):
            # Needs xlrd==1.2.0
            engine = 'xlrd'
        elif ext.endswith('.xlsb'):
            # Needs pyxlsb
            engine = 'pyxlsb'
        # .xlsx/.xlsm → openpyxl (engine=None lets pandas pick openpyxl if installed)

        excel_data = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, engine=engine)
        for sheet_name, df in excel_data.items():
            df_small = df.iloc[:20, :20]  # keep it sane
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

    # Fallback: openpyxl for xlsx-like
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

    # Fallback: maybe it's CSV
    try:
        import pandas as pd
        df = pd.read_csv(io.BytesIO(file_bytes), nrows=200)
        header = "\t".join(map(str, df.columns))
        rows = ["\t".join("" if pd.isna(v) else str(v) for v in r) for r in df.head(50).itertuples(index=False, name=None)]
        text = f"--- CSV ---\n{header}\n" + ("\n".join(rows) if rows else "(no rows)")
        if len(text) > CHARACTER_LIMIT:
            text = text[:CHARACTER_LIMIT-3] + '...'
        return text
    except Exception:
        pass

    # Last resort: tiny utf-8/latin-1 peek
    try:
        return file_bytes[:10240].decode('utf-8')
    except Exception:
        try:
            return file_bytes[:10240].decode('latin-1')
        except Exception:
            return ''
     

class DeleteChatAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, chat_pk):
        """Delete a specific chat and all its related messages"""
        user = request.user
        chat = get_object_or_404(Chat, pk=chat_pk, owner=user)

        # Delete chat (cascade deletes messages if related_name is set properly)
        chat_title = chat.title if hasattr(chat, 'title') else f"Chat {chat.pk}"
        chat.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)
        
# class BulkDeleteChatsAPIView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def delete(self, request):
#         """Delete multiple chats by IDs"""
#         chat_ids = request.data.get('chat_ids', [])
#         if not chat_ids or not isinstance(chat_ids, list):
#             return Response({'error': 'chat_ids (list) is required'}, status=status.HTTP_400_BAD_REQUEST)

#         deleted_count, _ = Chat.objects.filter(owner=request.user, pk__in=chat_ids).delete()
#         return Response({'deleted': deleted_count}, status=status.HTTP_204_NO_CONTENT)        
        
# Simple endpoint to update/get user OpenAI settings
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