# from django.shortcuts import get_object_or_404
# from rest_framework import generics, status, permissions
# from rest_framework.decorators import api_view, permission_classes
# from rest_framework.response import Response
# from rest_framework.views import APIView
# from django.conf import settings
# from .models import Chat, Message, UserOpenAISetting
# from .serializers import ChatSerializer, MessageSerializer, UserOpenAISettingSerializer, ChatNameSerializer
# from .permissions import IsOwner
# import openai
# from django.utils.text import Truncator
# import io
# import PyPDF2
# import docx
# import openpyxl
# import logging
# from django.core.files.uploadedfile import InMemoryUploadedFile, TemporaryUploadedFile
# import pandas as pd
# from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
# from django.db import transaction
# from django.utils import timezone
# from django.http import HttpResponse
# from django.core.files.base import ContentFile
# from django.http import StreamingHttpResponse

# logger = logging.getLogger(__name__)

# CHARACTER_LIMIT = 8000  # You increased this

# openai.api_key = getattr(settings, 'OPENAI_API_KEY', None)


# # ====================== HELPER: EXTRACT TEXT FROM EXCEL ======================
# def extract_text_from_excel_bytes(file_bytes, filename='file'):
#     import io, pandas as pd, openpyxl
#     parts = []

#     try:
#         ext = (filename or '').lower()
#         engine = None
#         if ext.endswith('.xls'):
#             engine = 'xlrd'
#         elif ext.endswith('.xlsb'):
#             engine = 'pyxlsb'

#         excel_data = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, engine=engine)
#         for sheet_name, df in excel_data.items():
#             df_small = df.iloc[:20, :20]
#             header = "\t".join(map(str, df_small.columns))
#             rows = ["\t".join("" if pd.isna(v) else str(v) for v in row) for row in df_small.itertuples(index=False, name=None)]
#             parts.append(f"--- Sheet: {sheet_name} ---\n{header}\n" + ("\n".join(rows) if rows else "(no rows)"))
#         text = "\n\n".join(parts)
#         if len(text) > CHARACTER_LIMIT:
#             text = text[:CHARACTER_LIMIT-3] + '...'
#         if text.strip():
#             return text
#     except Exception:
#         pass

#     try:
#         wb = openpyxl.load_workbook(filename=io.BytesIO(file_bytes), read_only=True, data_only=True)
#         for ws in wb.worksheets:
#             lines, max_rows, max_cols = [], 50, 50
#             for i, row in enumerate(ws.iter_rows(values_only=True)):
#                 if i >= max_rows: break
#                 lines.append("\t".join("" if c is None else str(c) for c in (row[:max_cols])))
#             parts.append(f"--- Sheet: {ws.title} ---\n" + ("\n".join(lines) if lines else "(no rows)"))
#         text = "\n\n".join(parts)
#         if len(text) > CHARACTER_LIMIT:
#             text = text[:CHARACTER_LIMIT-3] + '...'
#         if text.strip():
#             return text
#     except Exception:
#         pass

#     try:
#         df = pd.read_csv(io.BytesIO(file_bytes), nrows=200)
#         header = "\t".join(map(str, df.columns))
#         rows = ["\t".join("" if pd.isna(v) else str(v) for v in r) for r in df.head(50).itertuples(index=False, name=None)]
#         text = f"--- CSV ---\n{header}\n" + ("\n".join(rows) if rows else "(no rows)")
#         if len(text) > CHARACTER_LIMIT:
#             text = text[:CHARACTER_LIMIT-3] + '...'
#         return text
#     except Exception:
#         pass

#     try:
#         return file_bytes[:10240].decode('utf-8')
#     except Exception:
#         try:
#             return file_bytes[:10240].decode('latin-1')
#         except Exception:
#             return ''


# # ====================== CHAT LIST / CREATE ======================
# class ChatListCreateAPIView(generics.ListCreateAPIView):
#     serializer_class = ChatSerializer
#     permission_classes = [permissions.IsAuthenticated]

#     def get_queryset(self):
#         return Chat.objects.filter(owner=self.request.user).order_by('-updated_at')

#     def perform_create(self, serializer):
#         serializer.save(owner=self.request.user)


# # ====================== CHAT RETRIEVE ======================
# class ChatRetrieveAPIView(generics.RetrieveAPIView):
#     serializer_class = ChatSerializer
#     permission_classes = [permissions.IsAuthenticated, IsOwner]
#     queryset = Chat.objects.all()


# # ====================== MESSAGE LIST =====================, UserOpenAISettingAPIView ======================
# class MessageListAPIView(generics.ListAPIView):
#     serializer_class = MessageSerializer
#     permission_classes = [permissions.IsAuthenticated]

#     def get_queryset(self):
#         chat_id = self.kwargs['chat_pk']
#         chat = get_object_or_404(Chat, pk=chat_id, owner=self.request.user)
#         return chat.messages.all().order_by('created_at')

# def trim_messages_by_chars(messages, max_chars=12000):
#     total = 0
#     trimmed = []

#     # Start from latest messages
#     for msg in reversed(messages):
#         content = msg.get("content", "")
#         total += len(content)
#         if total > max_chars:
#             break
#         trimmed.insert(0, msg)

#     return trimmed


# # ====================== SEND MESSAGE (MAIN) ======================
# class SendMessageAPIView(APIView):
#     permission_classes = [permissions.IsAuthenticated]
#     parser_classes = [MultiPartParser, FormParser, JSONParser]

#     def post(self, request, chat_pk):
#         global CHARACTER_LIMIT
#         user = request.user
#         chat = get_object_or_404(Chat, pk=chat_pk, owner=user)

#         user_text = request.data.get('content', '').strip()
#         attachment = request.FILES.get('attachment')

#         if not user_text and not attachment:
#             return Response({'error': 'content or attachment is required'}, status=status.HTTP_400_BAD_REQUEST)

#         # ✅ ADD THIS BLOCK HERE
#         MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB

#         if attachment and attachment.size > MAX_UPLOAD_SIZE:
#             return Response(
#                 {"error": "File too large. Max allowed size is 10MB."},
#                 status=status.HTTP_400_BAD_REQUEST
#             )

#         # --- Extract attachment text ---
#         attachment_name = ''
#         attachment_content_type = ''
#         extracted_text = ''
#         try:
#             if attachment:
#                 attachment_name = getattr(attachment, 'name', '')
#                 attachment_content_type = getattr(attachment, 'content_type', '')

#                 file_bytes = attachment.read()

#                 # Rebuild clean file object for saving (IMPORTANT for Azure)
#                 attachment = ContentFile(file_bytes, name=attachment_name)

#                 lower = (attachment_name or '').lower()
#                 ct = (attachment_content_type or '').lower()

#                 def _is_spreadsheet(lower_name: str, content_type: str) -> bool:
#                     if lower_name.endswith(('.xlsx', '.xls', '.xlsm', '.xlsb', '.csv')):
#                         return True
#                     spreadsheet_mimes = {
#                         'application/vnd.ms-excel',
#                         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
#                         'application/vnd.ms-excel.sheet.macroenabled.12',
#                         'application/vnd.ms-excel.sheet.binary.macroenabled.12',
#                         'text/csv',
#                     }
#                     if content_type in spreadsheet_mimes:
#                         return True
#                     if content_type.startswith('application/vnd.openxmlformats-officedocument.spreadsheetml'):
#                         return True
#                     if 'spreadsheet' in content_type or content_type.endswith('/csv'):
#                         return True
#                     return False

#                 def _is_word(lower_name: str, content_type: str) -> bool:
#                     if lower_name.endswith(('.docx', '.doc')):
#                         return True
#                     word_mimes = {
#                         'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
#                         'application/msword',
#                     }
#                     return content_type in word_mimes or content_type.startswith(
#                         'application/vnd.openxmlformats-officedocument.wordprocessingml'
#                     )

#                 if lower.endswith('.txt') or (ct and ct.startswith('text/') and 'csv' not in ct):
#                     try:
#                         extracted_text = file_bytes.decode('utf-8')
#                     except Exception:
#                         try:
#                             extracted_text = file_bytes.decode('latin-1')
#                         except Exception:
#                             extracted_text = ''

#                 elif lower.endswith('.pdf') or ct == 'application/pdf':
#                     try:
#                         reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
#                         pages = []
#                         MAX_PDF_PAGES = 5  # VERY IMPORTANT (fast + safe)

#                         for i, p in enumerate(reader.pages):
#                             if i >= MAX_PDF_PAGES:
#                                 break
#                             try:
#                                 pages.append(p.extract_text() or '')
#                             except Exception:
#                                 pages.append('')
#                         extracted_text = "\n\n".join(pages)
#                     except Exception as e:
#                         logger.exception("PDF parse failed: %s", e)
#                         extracted_text = ''

#                 elif _is_spreadsheet(lower, ct):
#                     try:
#                         extracted_text = extract_text_from_excel_bytes(file_bytes, attachment_name)
#                     except Exception as e:
#                         logger.exception("Excel parse failed: %s", e)
#                         extracted_text = ''

#                 elif _is_word(lower, ct):
#                     try:
#                         d = docx.Document(io.BytesIO(file_bytes))
#                         extracted_text = "\n\n".join(p.text for p in d.paragraphs if p.text)
#                     except Exception as e:
#                         logger.exception("DOCX parse failed: %s", e)
#                         extracted_text = ''

#                 else:
#                     try:
#                         extracted_text = file_bytes[:10240].decode('utf-8')
#                     except Exception:
#                         try:
#                             extracted_text = file_bytes[:10240].decode('latin-1')
#                         except Exception:
#                             extracted_text = ''
#         except Exception:
#             logger.exception("Attachment extraction failed")
#             extracted_text = ''

#         MAX_CHAR_FOR_GPT = 6000  # ~2000 tokens safe

#         if extracted_text:
#             extracted_text = extracted_text[:MAX_CHAR_FOR_GPT]

#         # --- Save user message ---
#         user_msg = Message.objects.create(
#             chat=chat,
#             role='user',
#             content=user_text or (f"[Uploaded file: {attachment_name}]"),
#             attachment=attachment if attachment else None,
#             attachment_name=attachment_name,
#             attachment_content_type=attachment_content_type,
#         )

#         # --- Build messages_payload ---
#         system_prompt = chat.system_prompt or ''
#         messages_payload = []
#         if system_prompt:
#             messages_payload.append({'role': 'system', 'content': system_prompt})

#         recent_messages = chat.messages.all().order_by('-created_at')[:6][::-1]
#         for m in recent_messages:
#             base_content = m.content or ''
#             if m.attachment:
#                 att_note = f"[Attachment: {m.attachment_name} | content-type: {m.attachment_content_type}]"
#                 if m.pk == user_msg.pk and extracted_text:
#                     att_note = att_note + "\n\n" + extracted_text
#                 messages_payload.append({'role': m.role, 'content': (base_content + "\n\n" + att_note).strip()})
#             else:
#                 messages_payload.append({'role': m.role, 'content': base_content})

#         if attachment and extracted_text and not any(
#             (msg.get('role') == 'user' and f"[Attachment content from {attachment_name}]" in msg.get('content', ''))
#             for msg in messages_payload
#         ):
#             messages_payload.append({
#                 'role': 'user',
#                 'content': f"[Attachment content from {attachment_name}]\n\n{extracted_text}"
#             })

#         logger.debug("OpenAI messages_payload keys: %s", [m.get('role') for m in messages_payload])
#         messages_payload = trim_messages_by_chars(messages_payload, max_chars=12000)

#         # --- Model, temp, max_tokens ---
#         user_setting = getattr(user, 'openai_setting', None)
#         model = (
#             request.data.get('model') or
#             (user_setting.default_model if user_setting else None) or
#             "gpt-5"  # <<<--- FORCED TO GPT-5
#         )
#         max_tokens = int(request.data.get('max_tokens') or getattr(user_setting, 'max_tokens', 8000))
#         temperature = float(request.data.get('temperature') or getattr(user_setting, 'temperature', 0.7))

#         logger.debug("Sending to OpenAI → model=%s temp=%s max_output_tokens=%s", model, temperature, max_tokens)

#         try:
#             from openai import OpenAI
#             client = OpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))

#             def event_stream():
#                 full_text = ""
#                 usage_data = {}

#                 with client.responses.stream(
#                     model=model,
#                     input=messages_payload,
#                     instructions=system_prompt or None,
#                     tools=[{"type": "web_search"}],
#                     temperature=temperature,
#                     max_output_tokens=max_tokens,
#                 ) as stream:

#                     for event in stream:
#                         if event.type == "response.output_text.delta":
#                             delta = event.delta
#                             full_text += delta
#                             yield delta  # 🔥 send chunk immediately

#                         elif event.type == "response.completed":
#                             response = stream.get_final_response()
#                             if hasattr(response, "usage") and response.usage:
#                                 usage_data = (
#                                     response.usage.model_dump()
#                                     if hasattr(response.usage, "model_dump")
#                                     else {}
#                                 )

#                 # ✅ Save after stream finishes
#                 Message.objects.create(
#                     chat=chat,
#                     role='assistant',
#                     content=full_text,
#                     metadata={'openai_usage': usage_data}
#                 )

#             return StreamingHttpResponse(
#                 event_stream(),
#                 content_type="text/plain",
#             )

#         except Exception as e:
#             logger.exception("OpenAI streaming failed: %s", e)
#             return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

# # ====================== EDIT & RESEND ======================
# class EditAndResendAPIView(APIView):
#     permission_classes = [permissions.IsAuthenticated]
#     parser_classes = [MultiPartParser, FormParser, JSONParser]

#     def patch(self, request, chat_pk, message_pk):
#         user = request.user
#         chat = get_object_or_404(Chat, pk=chat_pk, owner=user)

#         new_content = (request.data.get('content') or request.GET.get('content') or '').strip()
#         if not new_content:
#             return Response({'error': 'content is required'}, status=status.HTTP_400_BAD_REQUEST)

#         try:
#             msg = Message.objects.get(pk=message_pk, chat=chat)
#         except Message.DoesNotExist:
#             return Response({'detail': 'No Message matches the given query.'}, status=status.HTTP_404_NOT_FOUND)

#         if msg.role == 'assistant':
#             user_msg = chat.messages.filter(role='user', created_at__lt=msg.created_at).order_by('-created_at').first()
#             if not user_msg:
#                 return Response({'detail': 'No preceding user message found.'}, status=status.HTTP_404_NOT_FOUND)
#             msg = user_msg
#         elif msg.role != 'user':
#             return Response({'detail': 'Message must be a user message.'}, status=status.HTTP_400_BAD_REQUEST)

#         original_content = msg.content or ''
#         with transaction.atomic():
#             meta = msg.metadata or {}
#             meta.setdefault('edits', []).append({
#                 'original': original_content,
#                 'edited_at': timezone.now().isoformat(),
#                 'editor_id': user.id,
#             })
#             msg.content = new_content
#             msg.metadata = meta
#             msg.edited = True
#             msg.edited_at = timezone.now()
#             msg.save()

#             # Rebuild payload
#             system_prompt = chat.system_prompt or ''
#             messages_payload = []
#             if system_prompt:
#                 messages_payload.append({'role': 'system', 'content': system_prompt})

#             recent_messages = chat.messages.all().order_by('-created_at')[:20][::-1]
#             for m in recent_messages:
#                 content = m.content or ''
#                 if m.attachment:
#                     att_note = f"[Attachment: {m.attachment_name} | content-type: {m.attachment_content_type}]"
#                     messages_payload.append({'role': m.role, 'content': (content + "\n\n" + att_note).strip()})
#                 else:
#                     messages_payload.append({'role': m.role, 'content': content})

#             # Model & params
#             user_setting = getattr(user, 'openai_setting', None)
#             model = (
#                 request.data.get('model') or
#                 (user_setting.default_model if user_setting else None) or
#                 "gpt-5"
#             )
#             max_tokens = int(request.data.get('max_tokens') or getattr(user_setting, 'max_tokens', 8000))
#             temperature = float(request.data.get('temperature') or getattr(user_setting, 'temperature', 0.7))

#             logger.debug("Edit call → model=%s temp=%s max=%s", model, temperature, max_tokens)

#             try:
#                 from openai import OpenAI
#                 client = OpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))

#                 def event_stream():
#                     full_text = ""
#                     usage_data = {}

#                     with client.responses.stream(
#                         model=model,
#                         input=messages_payload,
#                         instructions=system_prompt or None,
#                         tools=[{"type": "web_search"}],
#                         temperature=temperature,
#                         max_output_tokens=max_tokens,
#                     ) as stream:

#                         for event in stream:
#                             if event.type == "response.output_text.delta":
#                                 delta = event.delta
#                                 full_text += delta
#                                 yield delta

#                             elif event.type == "response.completed":
#                                 response = stream.get_final_response()
#                                 if hasattr(response, "usage") and response.usage:
#                                     usage_data = (
#                                         response.usage.model_dump()
#                                         if hasattr(response.usage, "model_dump")
#                                         else {}
#                                     )

#                     # ✅ Save after stream completes
#                     Message.objects.create(
#                         chat=chat,
#                         role='assistant',
#                         content=full_text,
#                         metadata={
#                             "openai_usage": usage_data,
#                             "replaced_by_edit_of": msg.pk
#                         }
#                     )

#                     # After stream ends → save full assistant message
#                     assistant_msg = Message.objects.create(
#                         chat=chat,
#                         role='assistant',
#                         content=full_text,
#                     )

#                 return StreamingHttpResponse(
#                     event_stream(),
#                     content_type="text/plain"
#                 )

#             except Exception as e:
#                 logger.exception("OpenAI streaming failed: %s", e)
#                 return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
#                 usage = resp.usage.model_dump() if hasattr(resp.usage, 'model_dump') else {}

#                 assistant_msg = chat.messages.filter(role='assistant', created_at__gt=msg.created_at).order_by('created_at').first()
#                 if assistant_msg:
#                     assistant_msg.content = assistant_text
#                     meta = assistant_msg.metadata or {}
#                     meta['openai_usage'] = usage
#                     meta['replaced_by_edit_of'] = msg.pk
#                     assistant_msg.metadata = meta
#                     assistant_msg.updated_at = timezone.now()
#                     assistant_msg.save()
#                 else:
#                     assistant_msg = Message.objects.create(
#                         chat=chat,
#                         role='assistant',
#                         content=assistant_text,
#                         metadata={'openai_usage': usage, 'replaced_by_edit_of': msg.pk}
#                     )

#                 serializer = MessageSerializer(assistant_msg, context={'request': request})
#                 return Response(serializer.data, status=status.HTTP_200_OK)

#             except Exception as e:
#                 logger.exception("OpenAI edit call failed: %s", e)
#                 return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)


# # ====================== DELETE CHAT ======================
# class DeleteChatAPIView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def delete(self, request, chat_pk):
#         user = request.user
#         chat = get_object_or_404(Chat, pk=chat_pk, owner=user)
#         chat.delete()
#         return Response(status=status.HTTP_204_NO_CONTENT)


# # ====================== USER OPENAI SETTINGS ======================
# class UserOpenAISettingAPIView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request):
#         setting, _ = UserOpenAISetting.objects.get_or_create(user=request.user)
#         return Response(UserOpenAISettingSerializer(setting).data)

#     def post(self, request):
#         setting, _ = UserOpenAISetting.objects.get_or_create(user=request.user)
#         serializer = UserOpenAISettingSerializer(setting, data=request.data, partial=True)
#         serializer.is_valid(raise_exception=True)
#         serializer.save()
#         return Response(serializer.data)
# class ExportLatestAssistantMessageAPIView(APIView):
#     # permission_classes = [IsAuthenticated, IsOwner]

#     def post(self, request, chat_pk):
#         chat = get_object_or_404(Chat, pk=chat_pk, owner=request.user)

#         assistant_msg = (
#             chat.messages
#             .filter(role='assistant')
#             .order_by('-created_at')
#             .first()
#         )

#         if not assistant_msg or not assistant_msg.content:
#             return Response(
#                 {"error": "No assistant response found"},
#                 status=400
#             )

#         # Create DOCX in memory
#         doc = docx.Document()
#         doc.add_heading("GPT Response", level=1)
#         doc.add_paragraph(assistant_msg.content)

#         buffer = io.BytesIO()
#         doc.save(buffer)
#         buffer.seek(0)

#         response = HttpResponse(
#             buffer.getvalue(),
#             content_type=(
#                 'application/vnd.openxmlformats-officedocument.'
#                 'wordprocessingml.document'
#             )
#         )
#         response['Content-Disposition'] = (
#             f'attachment; filename="chat_{chat.pk}_latest_response.docx"'
#         )

#         return response    
    
# class ChatNameListAPIView(generics.ListAPIView):
#     serializer_class = ChatNameSerializer
#     permission_classes = [permissions.IsAuthenticated]

#     def get_queryset(self):
#         return Chat.objects.filter(
#             owner=self.request.user
#         ).order_by('-updated_at').only('id', 'title', 'created_at', 'updated_at')


from typing import List, Dict
from django.shortcuts import get_object_or_404
from rest_framework import generics, status, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils.crypto import get_random_string
from .models import Chat, Message, UserOpenAISetting, DocumentChunk, DocumentProcessing
from .serializers import ChatSerializer, MessageSerializer, UserOpenAISettingSerializer, ChatNameSerializer
from .permissions import IsOwner
from .rag_utils import (
    extract_text_from_uploaded_file_async, split_into_chunks
)
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
import numpy as np
import asyncio
from asgiref.sync import sync_to_async
import json
from openai import OpenAI
import sys
from django.http import StreamingHttpResponse
import time
import concurrent.futures


logging.basicConfig(
    stream=sys.stdout,
    level=logging.DEBUG,
    format='%(levelname)s %(name)s %(message)s',
    force=True  # overrides any existing config
)

logger = logging.getLogger(__name__)
logger.info("🔥 VIEWS.PY LOADED — logger name: %s", __name__)


def resolve_request_user(request, chat=None):
    if getattr(request, 'user', None) and request.user.is_authenticated:
        return request.user
    if chat is not None and getattr(chat, 'owner_id', None):
        return chat.owner

    user_model = get_user_model()
    user = user_model.objects.order_by('id').first()
    if user is not None:
        return user

    username = f"temp_chat_user_{get_random_string(12)}"
    user = user_model.objects.create_user(username=username, password=None, is_active=True)
    user.set_unusable_password()
    user.save(update_fields=['password'])
    return user

CHARACTER_LIMIT = 100_000
openai.api_key = getattr(settings, 'OPENAI_API_KEY', None)

def _run_pdf_processing(chat, file_bytes: bytes, attachment_name: str) -> int:
    """
    Runs async PDF processing in an isolated thread with its own event loop.
    Safe to call from any sync Django view regardless of IocpProactor state (Windows).
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            process_pdf_for_rag(chat, file_bytes, attachment_name)
        )
    finally:
        loop.close()
        
# ====================== HELPER: PDF PROCESSING FOR RAG ======================

async def process_pdf_for_rag(chat: Chat, file_bytes: bytes, file_name: str) -> int:
    """
    Process PDF/image and create embeddings for RAG.
    Returns: Number of chunks created
    """
    doc_processing = None
    try:
        logger.info(
            "Starting PDF processing for chat_id=%s pdf_bytes=%s",
            chat.id,
            len(file_bytes) if file_bytes is not None else 0,
        )
 
        # Extract text — async parallel OCR for image-based pages
        pages_text = await extract_text_from_uploaded_file_async(file_name, file_bytes)
 
        page_text_lengths = [len(text or "") for text in pages_text.values()]
        logger.info(
            "Extracted PDF text for chat_id=%s pages=%s non_empty_pages=%s total_chars=%s",
            chat.id,
            len(pages_text),
            sum(1 for length in page_text_lengths if length > 0),
            sum(page_text_lengths),
        )
 
        # Create chunks
        logger.info(
            "Creating chunks for chat_id=%s chunk_size=%s overlap=%s",
            chat.id,
            1000,
            200,
        )
        chunks = split_into_chunks(pages_text)
        if chunks:
            logger.info(
                "Created chunks for chat_id=%s chunk_count=%s first_chunk=%s last_chunk=%s",
                chat.id,
                len(chunks),
                {
                    "page_number": chunks[0]["page_number"],
                    "chunk_index": chunks[0]["chunk_index"],
                    "char_count": chunks[0]["char_count"],
                },
                {
                    "page_number": chunks[-1]["page_number"],
                    "chunk_index": chunks[-1]["chunk_index"],
                    "char_count": chunks[-1]["char_count"],
                },
            )
        else:
            logger.warning("No chunks were created for chat_id=%s", chat.id)
 
        # Update document processing status
        doc_processing, _ = await sync_to_async(DocumentProcessing.objects.get_or_create)(
            chat=chat, defaults={'status': 'processing', 'started_at': timezone.now()}
        )
        logger.info(
            "DocumentProcessing ready for chat_id=%s processing_id=%s status=%s",
            chat.id,
            doc_processing.id,
            doc_processing.status,
        )
        doc_processing.total_pages = len(pages_text)
        await sync_to_async(doc_processing.save)()
 
        # Get embeddings (batch by 50 to manage API rate limits)
        client = OpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))
 
        chunk_batch_size = 50
        all_embedding_vectors = []
 
        for batch_idx in range(0, len(chunks), chunk_batch_size):
            batch_chunks = chunks[batch_idx:batch_idx + chunk_batch_size]
            batch_texts = [chunk['text'] for chunk in batch_chunks]
            logger.info(
                "Embedding chunk batch for chat_id=%s batch_number=%s batch_size=%s chunk_index_range=%s-%s",
                chat.id,
                batch_idx // chunk_batch_size + 1,
                len(batch_chunks),
                batch_chunks[0]['chunk_index'] if batch_chunks else None,
                batch_chunks[-1]['chunk_index'] if batch_chunks else None,
            )
 
            try:
                response = client.embeddings.create(
                    model="text-embedding-3-small",
                    input=batch_texts,
                    encoding_format="float"
                )
 
                batch_embeddings = [item.embedding for item in response.data]
                all_embedding_vectors.extend(batch_embeddings)
 
                logger.info(
                    "Embedded chunk batch for chat_id=%s batch_number=%s embedded_count=%s total_embeddings=%s",
                    chat.id,
                    batch_idx // chunk_batch_size + 1,
                    len(batch_embeddings),
                    len(all_embedding_vectors),
                )
            except Exception as e:
                logger.exception(
                    "Embedding batch failed for chat_id=%s batch_number=%s batch_size=%s",
                    chat.id,
                    batch_idx // chunk_batch_size + 1,
                    len(batch_chunks),
                )
                raise
 
        # Save chunks to database
        chunk_objects = []
        for chunk, embedding_vector in zip(chunks, all_embedding_vectors):
            embedding_json = json.dumps(embedding_vector)
            chunk_obj = DocumentChunk(
                chat=chat,
                page_number=chunk['page_number'],
                chunk_index=chunk['chunk_index'],
                text=chunk['text'],
                embedding=embedding_json.encode('utf-8'),
                char_count=chunk['char_count'],
                embedding_model='text-embedding-3-small'
            )
            chunk_objects.append(chunk_obj)
 
        logger.info(
            "Saving chunk objects for chat_id=%s chunk_object_count=%s",
            chat.id,
            len(chunk_objects),
        )
        await sync_to_async(DocumentChunk.objects.filter(chat=chat).delete)()
        await sync_to_async(DocumentChunk.objects.bulk_create)(chunk_objects, batch_size=100)
        logger.info(
            "Saved document chunks for chat_id=%s saved_count=%s",
            chat.id,
            len(chunk_objects),
        )
 
        # Update processing status
        doc_processing.status = 'completed'
        doc_processing.total_chunks = len(chunks)
        doc_processing.completed_at = timezone.now()
        await sync_to_async(doc_processing.save)()
 
        # Update chat
        chat.has_document = True
        await sync_to_async(chat.save)()
        logger.info(
            "Completed PDF processing for chat_id=%s total_pages=%s total_chunks=%s",
            chat.id,
            len(pages_text),
            len(chunks),
        )
 
        return len(chunks)
 
    except Exception as e:
        logger.exception("PDF processing failed for chat_id=%s: %s", chat.id, e)
        if doc_processing is not None:
            doc_processing.status = 'failed'
            doc_processing.error_message = str(e)
            await sync_to_async(doc_processing.save)()
        raise


# def get_relevant_chunks_for_query(chat: Chat, query_embedding: list, max_chunks: int = 5) -> List[Dict]:
#     """
#     Find most relevant chunks for a query using semantic search.
#     """
#     # Get all chunks for this chat
#     all_chunks = DocumentChunk.objects.filter(chat=chat).order_by('chunk_index')
    
#     if not all_chunks.exists():
#         return []
    
#     # Calculate similarities
#     similarities = []
    
#     for chunk in all_chunks:
#         try:
#             chunk_embedding = json.loads(bytes(chunk.embedding).decode('utf-8'))
            
#             # Cosine similarity
#             query_arr = np.array(query_embedding)
#             chunk_arr = np.array(chunk_embedding)
            
#             dot_product = np.dot(query_arr, chunk_arr)
#             norm_query = np.linalg.norm(query_arr)
#             norm_chunk = np.linalg.norm(chunk_arr)
            
#             if norm_query > 0 and norm_chunk > 0:
#                 similarity = float(dot_product / (norm_query * norm_chunk))
#                 similarities.append((chunk, similarity))
#         except Exception as e:
#             logger.warning(f"Similarity calculation failed for chunk {chunk.id}: {e}")
#             continue
    
#     # Sort by similarity and get top k
#     similarities.sort(key=lambda x: x[1], reverse=True)
#     top_chunks = [chunk for chunk, _ in similarities[:max_chunks]]
    
#     return top_chunks

def get_relevant_chunks_for_query(chat: Chat, query_embedding: list, max_chunks: int = 5) -> List[Dict]:
    """
    Find most relevant chunks using vectorized numpy (no per-row loop).
    """
    all_chunks = list(DocumentChunk.objects.filter(chat=chat).only('id', 'embedding', 'page_number', 'chunk_index', 'text'))
    
    if not all_chunks:
        return []

    # Deserialize all embeddings at once
    matrix = []
    valid_chunks = []
    for chunk in all_chunks:
        try:
            vec = json.loads(bytes(chunk.embedding).decode('utf-8'))
            matrix.append(vec)
            valid_chunks.append(chunk)
        except Exception:
            continue

    if not matrix:
        return []

    max_chunks = min(max_chunks, len(valid_chunks))

    # Vectorized cosine similarity — one matrix operation instead of a loop
    matrix_np = np.array(matrix, dtype=np.float32)          # shape (N, D)
    query_np  = np.array(query_embedding, dtype=np.float32)  # shape (D,)

    dot_products = matrix_np @ query_np
    norms = np.linalg.norm(matrix_np, axis=1) * np.linalg.norm(query_np)
    similarities = np.where(norms > 0, dot_products / norms, 0.0)

    # Get top-k indices without sorting everything
    top_indices = np.argpartition(similarities, -max_chunks)[-max_chunks:]
    top_indices = top_indices[np.argsort(similarities[top_indices])[::-1]]

    return [valid_chunks[i] for i in top_indices]


# ====================== CHAT LIST / CREATE ======================
class ChatListCreateAPIView(generics.ListCreateAPIView):
    serializer_class = ChatSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Chat.objects.all().order_by('-updated_at')

    def perform_create(self, serializer):
        user = resolve_request_user(self.request)
        if user is None:
            raise ValidationError({'error': 'No user available to assign chat owner.'})
        serializer.save(owner=user)


# ====================== CHAT RETRIEVE ======================
class ChatRetrieveAPIView(generics.RetrieveAPIView):
    serializer_class = ChatSerializer
    permission_classes = [permissions.IsAuthenticated, IsOwner]
    queryset = Chat.objects.all()


# ====================== MESSAGE LIST ======================
class MessageListAPIView(generics.ListAPIView):
    serializer_class = MessageSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        chat_id = self.kwargs['chat_pk']
        chat = get_object_or_404(Chat, pk=chat_id)
        return chat.messages.all().order_by('created_at')


# ====================== SEND MESSAGE WITH RAG SUPPORT ======================
class SendMessageAPIView(APIView):
    permission_classes = [permissions.AllowAny]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
 
    def post(self, request, chat_pk):
        chat = get_object_or_404(Chat, pk=chat_pk)
        user = resolve_request_user(request, chat=chat)
 
        user_text = request.data.get('content', '').strip()
        attachment = request.FILES.get('attachment')
        attachment_name = getattr(attachment, 'name', '') if attachment else ''
        attachment_content_type = getattr(attachment, 'content_type', '') if attachment else ''
 
        allowed_ext = ('.pdf', '.jpg', '.jpeg', '.png', '.webp')
 
        logger.info(
            "SendMessageAPIView received request chat_id=%s user_id=%s has_attachment=%s "
            "attachment_name=%s attachment_content_type=%s user_text_length=%s has_document=%s",
            chat.id, user.id, bool(attachment), attachment_name,
            attachment_content_type, len(user_text), chat.has_document,
        )
 
        if not user_text and not attachment:
            return Response(
                {'error': 'content or attachment is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
 
        # --- Handle file upload for RAG ---
        if attachment_name.lower().endswith(allowed_ext):
            file_bytes = attachment.read()
            logger.info(
                "Read uploaded file bytes for chat_id=%s attachment_name=%s byte_count=%s",
                chat.id, attachment_name, len(file_bytes),
            )
 
            # Save user message
            user_msg = Message.objects.create(
                chat=chat,
                role='user',
                content=user_text if user_text else f"[Uploaded file: {attachment_name}]",
                attachment=attachment,
                attachment_name=attachment_name,
                attachment_content_type=attachment_content_type,
            )
            logger.info(
                "Created user message for chat_id=%s message_id=%s content=%s",
                chat.id, user_msg.id, user_msg.content[:80],
            )
 
            # Process file in isolated thread (fixes Windows IocpProactor conflict)
            try:
                logger.info("Starting PDF processing for chat_id=%s", chat.id)
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        _run_pdf_processing, chat, file_bytes, attachment_name
                    )
                    num_chunks = future.result()
 
                logger.info(
                    "PDF processing completed for chat_id=%s chunk_count=%s",
                    chat.id, num_chunks,
                )
 
                if not user_text:
                    completion_msg = Message.objects.create(
                        chat=chat,
                        role='assistant',
                        content=f'✅ File "{attachment_name}" processed ({num_chunks} chunks). You can now ask questions about it.',
                    )
                    serializer = MessageSerializer(completion_msg, context={'request': request})
                    return Response(serializer.data, status=status.HTTP_201_CREATED)
 
            except Exception as e:
                logger.exception(
                    "File upload processing failed for chat_id=%s attachment_name=%s: %s",
                    chat.id, attachment_name, e,
                )
                error_msg = Message.objects.create(
                    chat=chat,
                    role='assistant',
                    content=f"❌ Failed to process file: {str(e)}",
                )
                serializer = MessageSerializer(error_msg, context={'request': request})
                return Response(serializer.data, status=status.HTTP_400_BAD_REQUEST)
 
        # --- Handle regular chat / document Q&A ---
        existing_user_msg = Message.objects.filter(
            chat=chat, role='user'
        ).order_by('-created_at').first()
 
        if existing_user_msg and existing_user_msg.content == user_text:
            user_msg = existing_user_msg
        else:
            user_msg = Message.objects.create(
                chat=chat,
                role='user',
                content=user_text or f"[Uploaded file: {attachment_name}]",
                attachment=attachment if attachment else None,
                attachment_name=getattr(attachment, 'name', ''),
                attachment_content_type=getattr(attachment, 'content_type', ''),
            )
 
        # Build prompt for OpenAI
        system_prompt = chat.system_prompt or ""
        messages_payload = []
 
        if system_prompt:
            messages_payload.append({'role': 'system', 'content': system_prompt})
 
        # RAG context
        if chat.has_document:
            try:
                client = OpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))
 
                response = client.embeddings.create(
                    model="text-embedding-3-small",
                    input=[user_text],
                    encoding_format="float"
                )
                query_embedding = response.data[0].embedding
 
                user_setting = getattr(user, 'openai_setting', None)
                max_chunks = getattr(user_setting, 'max_context_chunks', 5)
                relevant_chunks = get_relevant_chunks_for_query(chat, query_embedding, max_chunks)
 
                if relevant_chunks:
                    context_parts = []
                    for chunk in relevant_chunks:
                        context_parts.append(f"[Page {chunk.page_number}]\n{chunk.text}")
 
                    rag_context = "\n\n" + "=" * 50 + "\n\n".join(context_parts)
 
                    if messages_payload and messages_payload[0]['role'] == 'system':
                        messages_payload[0]['content'] += (
                            f"\n\n🔗 You are answering based on the following document excerpts:\n{rag_context}"
                        )
                    else:
                        messages_payload.insert(0, {
                            'role': 'system',
                            'content': f"Answer ONLY based on the following document:\n{rag_context}",
                        })
 
            except Exception as e:
                logger.error("RAG retrieval failed: %s", e)
 
        # Conversation history
        recent_messages = chat.messages.all().order_by('-created_at')[:10][::-1]
        for m in recent_messages:
            if m.pk != user_msg.pk:
                messages_payload.append({'role': m.role, 'content': m.content or ''})
 
        messages_payload.append({'role': 'user', 'content': user_text})
 
        # Model params
        user_setting = getattr(user, 'openai_setting', None)
        model = (
            request.data.get('model') or
            (user_setting.default_model if user_setting else None) or
            "gpt-4o"
        )
        max_tokens = int(request.data.get('max_tokens') or getattr(user_setting, 'max_tokens', 2000))
        temperature = float(request.data.get('temperature') or getattr(user_setting, 'temperature', 0.7))
 
        logger.debug("Sending to OpenAI → model=%s temp=%s max_tokens=%s", model, temperature, max_tokens)
 
        try:
            client = OpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))
 
            resp = client.chat.completions.create(
                model=model,
                messages=messages_payload,
                temperature=temperature,
                max_tokens=max_tokens,
            )
 
            assistant_text = resp.choices[0].message.content or ""
            usage = {
                'input_tokens': resp.usage.prompt_tokens,
                'output_tokens': resp.usage.completion_tokens,
                'total_tokens': resp.usage.total_tokens,
            }
 
            logger.info("OpenAI response: %s", usage)
 
            assistant_msg = Message.objects.create(
                chat=chat,
                role='assistant',
                content=assistant_text,
                metadata={'openai_usage': usage},
            )
 
            serializer = MessageSerializer(assistant_msg, context={'request': request})
            return Response(serializer.data, status=status.HTTP_201_CREATED)
 
        except Exception as e:
            logger.exception("OpenAI call failed: %s", e)
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)


# ====================== DELETE CHAT ======================
class DeleteChatAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, chat_pk):
        chat = get_object_or_404(Chat, pk=chat_pk)
        chat.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ====================== USER OPENAI SETTINGS ======================
class UserOpenAISettingAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = resolve_request_user(request)
        setting, _ = UserOpenAISetting.objects.get_or_create(user=user)
        return Response(UserOpenAISettingSerializer(setting).data)

    def post(self, request):
        user = resolve_request_user(request)
        setting, _ = UserOpenAISetting.objects.get_or_create(user=user)
        serializer = UserOpenAISettingSerializer(setting, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


# ====================== DOCUMENT PROCESSING STATUS ======================
class DocumentProcessingStatusAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, chat_pk):
        chat = get_object_or_404(Chat, pk=chat_pk)
        
        try:
            doc_processing = chat.document_processing
            return Response({
                'status': doc_processing.status,
                'total_pages': doc_processing.total_pages,
                'processed_pages': doc_processing.processed_pages,
                'total_chunks': doc_processing.total_chunks,
                'error_message': doc_processing.error_message,
                'completed_at': doc_processing.completed_at,
            })
        except DocumentProcessing.DoesNotExist:
            return Response({'status': 'none'}, status=status.HTTP_404_NOT_FOUND)


# ====================== STREAMING CHAT (SSE) ======================
class StreamingChatAPIView(APIView):
    """
    POST /api/v1/chats/<chat_pk>/messages/stream/
    The complete assistant message is saved to the DB once the stream finishes.
    """
    permission_classes = [permissions.AllowAny]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
 
    def post(self, request, chat_pk):
        chat = get_object_or_404(Chat, pk=chat_pk)
        user = resolve_request_user(request, chat=chat)
 
        user_text = request.data.get('content', '').strip()
        attachment = request.FILES.get('attachment')
 
        if not user_text and not attachment:
            return Response(
                {'error': 'content or attachment is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
 
        attachment_name = getattr(attachment, 'name', '') if attachment else ''
        attachment_content_type = getattr(attachment, 'content_type', '') if attachment else ''
        allowed_ext = ('.pdf', '.jpg', '.jpeg', '.png', '.webp')
 
        # --- Handle file upload for RAG (non-streaming path) ---
        if attachment_name.lower().endswith(allowed_ext):
            file_bytes = attachment.read()
            user_msg = Message.objects.create(
                chat=chat,
                role='user',
                content=user_text if user_text else f"[Uploaded file: {attachment_name}]",
                attachment=attachment,
                attachment_name=attachment_name,
                attachment_content_type=attachment_content_type,
            )
 
            try:
                # Process in isolated thread (fixes Windows IocpProactor conflict)
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        _run_pdf_processing, chat, file_bytes, attachment_name
                    )
                    num_chunks = future.result()
 
                if not user_text:
                    completion_msg = Message.objects.create(
                        chat=chat,
                        role='assistant',
                        content=f'✅ File "{attachment_name}" processed ({num_chunks} chunks). You can now ask questions about it.',
                    )
                    serializer = MessageSerializer(completion_msg, context={'request': request})
                    return Response(serializer.data, status=status.HTTP_201_CREATED)
 
            except Exception as e:
                logger.exception("File upload processing failed for streaming view: %s", e)
                error_msg = Message.objects.create(
                    chat=chat,
                    role='assistant',
                    content=f"❌ Failed to process file: {str(e)}",
                )
                serializer = MessageSerializer(error_msg, context={'request': request})
                return Response(serializer.data, status=status.HTTP_400_BAD_REQUEST)
 
        # --- Save user message ---
        existing_user_msg = Message.objects.filter(
            chat=chat, role='user'
        ).order_by('-created_at').first()
 
        if existing_user_msg and existing_user_msg.content == user_text:
            user_msg = existing_user_msg
        else:
            user_msg = Message.objects.create(
                chat=chat,
                role='user',
                content=user_text or f"[Uploaded file: {attachment_name}]",
                attachment=attachment if attachment else None,
                attachment_name=attachment_name,
                attachment_content_type=attachment_content_type,
            )
 
        # --- Build messages_payload with RAG ---
        system_prompt = chat.system_prompt or ""
        messages_payload = []
 
        if system_prompt:
            messages_payload.append({'role': 'system', 'content': system_prompt})
 
        # RAG context
        if chat.has_document:
            try:
                client = OpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))
                response = client.embeddings.create(
                    model="text-embedding-3-small",
                    input=[user_text],
                    encoding_format="float",
                )
                query_embedding = response.data[0].embedding
                user_setting = getattr(user, 'openai_setting', None)
                max_chunks = getattr(user_setting, 'max_context_chunks', 5)
                relevant_chunks = get_relevant_chunks_for_query(chat, query_embedding, max_chunks)
 
                if relevant_chunks:
                    context_parts = [f"[Page {c.page_number}]\n{c.text}" for c in relevant_chunks]
                    rag_context = "\n\n" + "=" * 50 + "\n\n".join(context_parts)
                    if messages_payload and messages_payload[0]['role'] == 'system':
                        messages_payload[0]['content'] += f"\n\n🔗 Document excerpts:\n{rag_context}"
                    else:
                        messages_payload.insert(0, {
                            'role': 'system',
                            'content': f"Answer ONLY based on the following document:\n{rag_context}",
                        })
            except Exception as e:
                logger.error("RAG retrieval failed in streaming view: %s", e)
 
        # Conversation history
        recent_messages = chat.messages.all().order_by('-created_at')[:10][::-1]
        for m in recent_messages:
            if m.pk != user_msg.pk:
                messages_payload.append({'role': m.role, 'content': m.content or ''})
 
        messages_payload.append({'role': 'user', 'content': user_text})
 
        # Model params
        user_setting = getattr(user, 'openai_setting', None)
        model = (
            request.data.get('model') or
            (user_setting.default_model if user_setting else None) or
            "gpt-4o"
        )
        max_tokens = int(request.data.get('max_tokens') or getattr(user_setting, 'max_tokens', 2000))
        temperature = float(request.data.get('temperature') or getattr(user_setting, 'temperature', 0.7))
 
        logger.debug(
            "Streaming to OpenAI → model=%s temp=%s max_tokens=%s",
            model, temperature, max_tokens,
        )
 
        # --- SSE generator ---
        def event_stream():
            full_text = []
            client = OpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))
 
            try:
                with client.chat.completions.create(
                    model=model,
                    messages=messages_payload,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                ) as stream:
                    for chunk in stream:
                        delta = chunk.choices[0].delta if chunk.choices else None
                        if delta and delta.content:
                            text_piece = delta.content
                            full_text.append(text_piece)
                            escaped = text_piece.replace('\n', '\\n')
                            yield f"data: {escaped}\n\n"
 
                # Stream finished — save to DB
                assistant_text = "".join(full_text)
                Message.objects.create(
                    chat=chat,
                    role='assistant',
                    content=assistant_text,
                    metadata={'streaming': True},
                )
                logger.info(
                    "Streaming complete for chat_id=%s chars=%s",
                    chat.id, len(assistant_text),
                )
 
            except Exception as e:
                logger.exception(
                    "Streaming OpenAI call failed for chat_id=%s: %s", chat.id, e
                )
                yield f"data: [ERROR] {str(e)}\n\n"
 
            finally:
                yield "data: [DONE]\n\n"
 
        response = StreamingHttpResponse(
            event_stream(),
            content_type='text/event-stream',
        )
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        return response
class ChatNameListAPIView(generics.ListAPIView):
    serializer_class = ChatNameSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Chat.objects.filter(
            owner=self.request.user
        ).order_by('-updated_at').only('id', 'title', 'created_at', 'updated_at')