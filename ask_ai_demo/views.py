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
#         user_msg = MessageDemo.objects.create(
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
#             from openai import OpenAI, AsyncOpenAI
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
#                 MessageDemo.objects.create(
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
#             msg = MessageDemo.objects.get(pk=message_pk, chat=chat)
#         except MessageDemo.DoesNotExist:
#             return Response({'detail': 'No Message matches the given query.'}, status=status.HTTP_404_NOT_FOUND)

#         if msg.role == 'assistant':
#             user_msg = chat.messages.filter(role='user', created_at__lt=msg.created_at).order_by('-created_at').first()
#             if not user_msg:
#                 return Response({'detail': 'No preceding user message found.'}, status=status.HTTP_404_NOT_FOUND)
#             msg = user_msg
#         elif msg.role != 'user':
#             return Response({'detail': 'Message must be a user MessageDemo.'}, status=status.HTTP_400_BAD_REQUEST)

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
#                     MessageDemo.objects.create(
#                         chat=chat,
#                         role='assistant',
#                         content=full_text,
#                         metadata={
#                             "openai_usage": usage_data,
#                             "replaced_by_edit_of": msg.pk
#                         }
#                     )

#                     # After stream ends → save full assistant message
#                     assistant_msg = MessageDemo.objects.create(
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
#                     assistant_msg = MessageDemo.objects.create(
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

import os
from typing import List, Dict
from django.shortcuts import get_object_or_404
from rest_framework import generics, status, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils.crypto import get_random_string
from .models import (
    ChatDemo, MessageDemo, UserOpenAISettingDemo, DocumentChunkDemo, DocumentProcessingDemo,
    BusinessContextMemoryDemo, BusinessDocumentDemo, BusinessDocumentChunkDemo
)
from .memory_utils import (
    trigger_memory_update_async,
    process_business_document_pipeline,
    get_relevant_business_chunks_for_query,
)
from .serializers import (
    ChatSerializer, MessageSerializer, UserOpenAISettingSerializer,
    ChatNameSerializer, BusinessContextMemorySerializer, BusinessDocumentSerializer
)
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
# [DOCGEN DISABLED] from .document_generator import parse_docgen_marker, generate_document_file, DOCGEN_PATTERN
from .prompts import (
    classify_intent, build_system_prompt, get_intent_max_tokens,
    RAG_SIMILARITY_THRESHOLD,
)
from .chat_agent import files_note, run_chat_agent


# logging.basicConfig(
#     stream=sys.stdout,
#     level=logging.DEBUG,
#     format='%(levelname)s %(name)s %(message)s',
#     force=True  # overrides any existing config
# )

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
    chunks_count, _ = _run_pdf_processing_single(chat, file_bytes, attachment_name)
    return chunks_count


def _run_pdf_processing_single(chat, file_bytes: bytes, attachment_name: str) -> tuple[int, int]:
    close_old_connections()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            process_pdf_for_rag(chat, file_bytes, attachment_name)
        )
    finally:
        loop.close()
        close_old_connections()


# ====================== HELPER: PDF PROCESSING FOR RAG ======================

async def process_pdf_for_rag(chat: ChatDemo, file_bytes: bytes, file_name: str) -> tuple[int, int]:
    """
    Process PDF/image and create embeddings for RAG.
    Returns: (Number of chunks created, total pages)
    """
    try:
        # Extract text — async parallel OCR for image-based pages
        pages_text = await extract_text_from_uploaded_file_async(file_name, file_bytes)
        total_pages = len(pages_text)

        # Create chunks
        chunks = split_into_chunks(pages_text)

        # Get embeddings (batch by 50 to manage API rate limits)
        client = OpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))

        chunk_batch_size = 50
        all_embedding_vectors = []

        for batch_idx in range(0, len(chunks), chunk_batch_size):
            batch_chunks = chunks[batch_idx:batch_idx + chunk_batch_size]
            batch_texts = [chunk['text'] for chunk in batch_chunks]

            try:
                response = client.embeddings.create(
                    model="text-embedding-3-small",
                    input=batch_texts,
                    encoding_format="float"
                )

                batch_embeddings = [item.embedding for item in response.data]
                all_embedding_vectors.extend(batch_embeddings)
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
            chunk_obj = DocumentChunkDemo(
                chat=chat,
                page_number=chunk['page_number'],
                chunk_index=chunk['chunk_index'],
                text=chunk['text'],
                embedding=embedding_json.encode('utf-8'),
                char_count=chunk['char_count'],
                embedding_model='text-embedding-3-small'
            )
            chunk_objects.append(chunk_obj)

        if chunk_objects:
            await sync_to_async(DocumentChunkDemo.objects.bulk_create)(chunk_objects, batch_size=100)

        # Update chat
        if len(chunks) > 0:
            chat.has_document = True
            await sync_to_async(chat.save)(update_fields=['has_document'])

        return len(chunks), total_pages

    except Exception as e:
        logger.exception("PDF processing failed for chat_id=%s file=%s: %s", chat.id, file_name, e)
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

def get_relevant_chunks_for_query(
    chat: ChatDemo,
    query_embedding: list,
    max_chunks: int = 5,
) -> tuple:
    """
    Find most relevant chunks using vectorized numpy (no per-row loop).

    Returns:
        (chunks: List[DocumentChunk], max_similarity: float)
        max_similarity is 0.0 when no chunks are found.
        Callers should compare max_similarity against RAG_SIMILARITY_THRESHOLD
        to decide whether to inject a RAG-insufficient warning into the prompt.
    """
    all_chunks = list(DocumentChunkDemo.objects.filter(chat=chat).only(
        'id', 'embedding', 'page_number', 'chunk_index', 'text'
    ))

    if not all_chunks:
        return [], 0.0

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
        return [], 0.0

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

    top_chunks = [valid_chunks[i] for i in top_indices]
    max_sim = float(similarities[top_indices[0]]) if len(top_indices) else 0.0

    return top_chunks, max_sim


# ====================== CHAT LIST / CREATE ======================
class ChatListCreateAPIView(generics.ListCreateAPIView):
    serializer_class = ChatSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ChatDemo.objects.all().order_by('-updated_at')

    def perform_create(self, serializer):
        user = resolve_request_user(self.request)
        if user is None:
            raise ValidationError({'error': 'No user available to assign chat owner.'})
        serializer.save(owner=user)


# ====================== CHAT RETRIEVE ======================
class ChatRetrieveAPIView(generics.RetrieveAPIView):
    serializer_class = ChatSerializer
    permission_classes = [permissions.IsAuthenticated, IsOwner]
    queryset = ChatDemo.objects.all()


# ====================== MESSAGE LIST ======================
class MessageListAPIView(generics.ListAPIView):
    serializer_class = MessageSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        chat_id = self.kwargs['chat_pk']
        chat = get_object_or_404(ChatDemo, pk=chat_id)
        return chat.messages_demo.all().order_by('created_at')


# ====================== SEND MESSAGE WITH RAG SUPPORT ======================
class SendMessageAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
 
    def post(self, request, chat_pk):
        chat = get_object_or_404(ChatDemo, pk=chat_pk)
        user = resolve_request_user(request, chat=chat)
 
        user_text = request.data.get('content', '').strip()
        attachments = request.FILES.getlist('attachment')

        first_attachment = attachments[0] if attachments else None

        attachment_name = (getattr(first_attachment, 'name', '') if first_attachment else '')

        attachment_content_type = (getattr(first_attachment, 'content_type', '') if first_attachment else '')
 
        allowed_ext = ('.pdf', '.docx', '.xlsx', '.xls', '.pptx', '.ppt', '.csv', '.txt', '.jpg', '.jpeg', '.png','.webp')
 
        # logger.info(
        #     "SendMessageAPIView received request chat_id=%s user_id=%s has_attachment=%s "
        #     "attachment_name=%s attachment_content_type=%s user_text_length=%s has_document=%s",
        #     chat.id, user.id, bool(attachment), attachment_name,
        #     attachment_content_type, len(user_text), chat.has_document,
        # )
 
        if not user_text and not attachments:
            return Response(
                {'error': 'content or attachment is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
 
        # --- Handle file upload for RAG ---
        if attachments:

            processed_files = []
            valid_files_data = []

            try:

                # Remove old chunks once
                DocumentChunkDemo.objects.filter(chat=chat).delete()

                for attachment in attachments:

                    attachment_name = attachment.name
                    attachment_content_type = attachment.content_type

                    if not attachment_name.lower().endswith(allowed_ext):
                        continue

                    if attachment.size > MAX_UPLOAD_SIZE:
                        continue

                    file_bytes = attachment.read()
                    attachment.seek(0)

                    MessageDemo.objects.create(
                        chat=chat,
                        role='user',
                        content=f"[Uploaded file: {attachment_name}]",
                        attachment=attachment,
                        attachment_name=attachment_name,
                        attachment_content_type=attachment_content_type,
                    )

                    valid_files_data.append((file_bytes, attachment_name))
                    processed_files.append(attachment_name)

                if valid_files_data:
                    chat.is_processing_document = True
                    chat.save(update_fields=['is_processing_document'])

                    doc_proc, _ = DocumentProcessingDemo.objects.get_or_create(chat=chat)
                    doc_proc.status = 'processing'
                    doc_proc.started_at = timezone.now()
                    doc_proc.error_message = ''
                    doc_proc.total_pages = 0
                    doc_proc.processed_pages = 0
                    doc_proc.total_chunks = 0
                    doc_proc.completed_at = None
                    doc_proc.save()

                    threading.Thread(
                        target=_process_attachments_in_background,
                        args=(chat.id, valid_files_data, bool(user_text)),
                        daemon=True
                    ).start()

                if not user_text:

                    completion_msg = MessageDemo.objects.create(
                        chat=chat,
                        role='assistant',
                        content=(
                            f'✅ Uploaded {len(processed_files)} file(s):\n'
                            + "\n".join(f"- {n}" for n in processed_files)
                            + "\n\n⏳ Processing and indexing your documents in the background. You can track progress or start asking questions/instructions."
                        ),
                    )

                    serializer = MessageSerializer(
                        completion_msg,
                        context={'request': request}
                    )

                    return Response(
                        serializer.data,
                        status=status.HTTP_201_CREATED
                    )
                chat.refresh_from_db()

            except Exception as e:

                logger.exception(
                    "File upload processing failed for chat_id=%s",
                    chat.id
                )

                error_msg = MessageDemo.objects.create(
                    chat=chat,
                    role='assistant',
                    content=f"❌ Failed to process file: {str(e)}",
                )

                serializer = MessageSerializer(
                    error_msg,
                    context={'request': request}
                )

                return Response(
                    serializer.data,
                    status=status.HTTP_400_BAD_REQUEST
                )
 
        # --- Handle regular chat / document Q&A ---
        existing_user_msg = MessageDemo.objects.filter(
            chat=chat, role='user'
        ).order_by('-created_at').first()
 
        if existing_user_msg and existing_user_msg.content == user_text:
            user_msg = existing_user_msg
        else:
            user_msg = MessageDemo.objects.create(
                chat=chat,
                role='user',
                content=user_text or f"[Uploaded file: {attachment_name}]",
                attachment=attachment if attachment else None,
                attachment_name=getattr(attachment, 'name', ''),
                attachment_content_type=getattr(attachment, 'content_type', ''),
            )
 
        # Build prompt for OpenAI
        user_setting = getattr(user, 'openai_setting', None)

        # --- Check for image attachment to provide multimodal vision ---
        image_exts = ('.png', '.jpg', '.jpeg', '.webp', '.gif')
        active_image_att = None
        if attachments:
            for att in attachments:
                if any(att.name.lower().endswith(ext) for ext in image_exts):
                    active_image_att = att
                    break
        elif user_msg and user_msg.attachment and any(user_msg.attachment_name.lower().endswith(ext) for ext in image_exts):
            active_image_att = user_msg.attachment

        has_image_attachment = False
        image_b64 = None
        image_content_type = "image/png"
        if active_image_att:
            try:
                import base64
                if hasattr(active_image_att, 'seek'):
                    active_image_att.seek(0)
                    img_data = active_image_att.read()
                    active_image_att.seek(0)
                else:
                    with active_image_att.open('rb') as _f:
                        img_data = _f.read()
                image_b64 = base64.b64encode(img_data).decode('ascii')
                image_content_type = getattr(active_image_att, 'content_type', '') or getattr(user_msg, 'attachment_content_type', '') or 'image/png'
                has_image_attachment = True
            except Exception as _img_err:
                logger.warning("Failed to prepare image base64 in SendMessageAPIView: %s", _img_err)

        # --- Task 2: Classify intent for adaptive prompting ---
        recent_history_for_classifier = list(
            chat.messages_demo.order_by('-created_at')[:4].values('role', 'content')
        )
        context_mem_str = getattr(user_setting, 'context_memory', "") if user_setting else ""
        business_mem_str = BusinessContextMemoryDemo.get_active_memory()
        combined_mem_for_classifier = ""
        if business_mem_str:
            combined_mem_for_classifier += f"[BUSINESS CONTEXT]\n{business_mem_str}\n\n"
        if context_mem_str:
            combined_mem_for_classifier += f"[USER CONTEXT]\n{context_mem_str}"

        intent, relevant_memory = classify_intent(
            user_message=user_text,
            recent_history=recent_history_for_classifier,
            context_memory=combined_mem_for_classifier.strip(),
            api_key=getattr(settings, 'OPENAI_API_KEY', None),
        )

        chat.refresh_from_db(fields=['is_processing_document', 'has_document'])
        if chat.is_processing_document and intent != 'document_generation':
            wait_msg = MessageDemo.objects.create(
                chat=chat,
                role='assistant',
                content="⏳ Still processing and indexing your uploaded document(s) in the background — please wait a few moments and ask again so I can answer using the full document context.",
            )
            serializer = MessageSerializer(wait_msg, context={'request': request})
            return Response(serializer.data, status=status.HTTP_200_OK)

        # --- Task 4: RAG context with similarity threshold ---
        rag_insufficient = False
        messages_payload = []

        has_business_docs = BusinessDocumentChunkDemo.objects.filter(
            business_document__business_memory__is_active=True
        ).exists()

        if chat.has_document or has_business_docs:
            try:
                emb_client = OpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))
                emb_response = emb_client.embeddings.create(
                    model="text-embedding-3-small",
                    input=[user_text],
                    encoding_format="float"
                )
                query_embedding = emb_response.data[0].embedding
                max_chunks = getattr(user_setting, 'max_context_chunks', 5)

                # 1. Chat-specific RAG (ChatGPT Token Budget Threshold)
                if chat.has_document:
                    total_chat_chunks = DocumentChunkDemo.objects.filter(chat=chat).count()
                    if total_chat_chunks <= 30:
                        all_chunks = DocumentChunkDemo.objects.filter(chat=chat).order_by('page_number', 'chunk_index')
                        context_parts = [f"[Page {c.page_number}]\n{c.text}" for c in all_chunks]
                        rag_context = "\n\n" + "=" * 50 + "\n\n".join(context_parts)
                        messages_payload.append({
                            'role': 'system',
                            'content': f"🔗 Document Content (Complete Document):\n{rag_context}"
                        })
                    else:
                        relevant_chunks, max_sim = get_relevant_chunks_for_query(chat, query_embedding, max_chunks=8)
                        is_generic_query = any(k in user_text.lower() for k in [
                            'explain', 'summarize', 'what is', 'what are', 'tell me about', 'overview', 'review', 'read this', 'this image', 'screenshot'
                        ]) or len(user_text.strip().split()) <= 4

                        if relevant_chunks and (max_sim >= RAG_SIMILARITY_THRESHOLD or is_generic_query):
                            context_parts = [f"[Page {chunk.page_number}]\n{chunk.text}" for chunk in relevant_chunks]
                            rag_context = "\n\n" + "=" * 50 + "\n\n".join(context_parts)
                            messages_payload.append({
                                'role': 'system',
                                'content': f"🔗 You are answering based on the following document excerpts:\n{rag_context}"
                            })
                        elif not has_image_attachment:
                            rag_insufficient = True
                            logger.info(
                                "RAG context weak for chat_id=%s max_sim=%.3f threshold=%.3f",
                                chat.id, max_sim, RAG_SIMILARITY_THRESHOLD
                            )

                # 2. Business-level RAG (Organization Knowledge Base)
                if has_business_docs:
                    b_chunks, b_max_sim = get_relevant_business_chunks_for_query(query_embedding, max_chunks=3)
                    if b_chunks and b_max_sim >= RAG_SIMILARITY_THRESHOLD:
                        b_context_parts = [
                            f"[{c.business_document.name} - Page {c.page_number}]\n{c.text}"
                            for c in b_chunks
                        ]
                        b_rag_context = "\n\n" + "=" * 50 + "\n\n".join(b_context_parts)
                        messages_payload.append({
                            'role': 'system',
                            'content': f"🏢 Organization Knowledge Base Excerpts:\n{b_rag_context}"
                        })

            except Exception as e:
                logger.error("RAG retrieval failed: %s", e)


        # --- Task 2: Build intent-adaptive system prompt ---
        intent_system_prompt = build_system_prompt(intent, rag_insufficient=rag_insufficient)

        # Preserve any custom chat-level system prompt (from DB) as a prefix
        custom_chat_prompt = chat.system_prompt or ""
        if custom_chat_prompt:
            intent_system_prompt = custom_chat_prompt + "\n\n" + intent_system_prompt

        # Inject business-level context memory (shared for all users)
        business_mem_str = BusinessContextMemoryDemo.get_active_memory()
        if business_mem_str:
            intent_system_prompt += f"\n\n[BUSINESS CONTEXT MEMORY (Organization Rules & Ground Truth)]\n{business_mem_str}\n"

        # Inject long-term user context memory if relevant
        if relevant_memory:
            intent_system_prompt += f"\n\n[USER CONTEXT MEMORY (Past Facts & Preferences)]\n{relevant_memory}\n"

        if has_image_attachment:
            intent_system_prompt += (
                "\n\n[USER IMAGE ATTACHMENT: An image/screenshot is provided directly in the user message. "
                "Visually examine the image content to answer questions, explain code or UI, and provide detailed insights accurately.]\n"
            )

        messages_payload.insert(0, {'role': 'system', 'content': intent_system_prompt})

        # Conversation history
        recent_messages = chat.messages_demo.all().order_by('-created_at')[:10][::-1]
        for m in recent_messages:
            if m.pk != user_msg.pk:
                messages_payload.append({'role': m.role, 'content': m.content or ''})

        if has_image_attachment and image_b64:
            user_msg_content = [
                {"type": "text", "text": user_text or "Please explain and analyze this uploaded image."}
            ]
            user_msg_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image_content_type};base64,{image_b64}"
                }
            })
            messages_payload.append({'role': 'user', 'content': user_msg_content})
        else:
            messages_payload.append({'role': 'user', 'content': user_text})

        # --- Task 1+3: Model params with reasoning_effort and per-intent max_tokens ---
        model = (
            request.data.get('model') or
            (user_setting.default_model if user_setting else None) or
            "gpt-4o"
        )
        user_max_tokens = int(request.data.get('max_tokens') or getattr(user_setting, 'max_tokens', 2000))
        max_tokens = get_intent_max_tokens(intent, user_max_tokens)
        temperature = float(request.data.get('temperature') or getattr(user_setting, 'temperature', 0.7))
        is_reasoning = getattr(user_setting, 'is_reasoning_model', lambda: False)()
        reasoning_effort = getattr(user_setting, 'reasoning_effort', 'medium')

        logger.debug(
            "Sending to OpenAI → model=%s intent=%s max_tokens=%s reasoning=%s",
            model, intent, max_tokens, is_reasoning
        )
 
        try:
            client = OpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))

            # Task 1: Use reasoning_effort for reasoning-tier models, temperature otherwise.
            call_kwargs: dict = {
                "model": model,
                "messages": messages_payload,
                "max_tokens": max_tokens,
            }
            if is_reasoning:
                call_kwargs["reasoning_effort"] = reasoning_effort
            else:
                call_kwargs["temperature"] = temperature

            resp = client.chat.completions.create(**call_kwargs)

            assistant_text = resp.choices[0].message.content or ""
            finish_reason = resp.choices[0].finish_reason
            usage = {
                'input_tokens': resp.usage.prompt_tokens,
                'output_tokens': resp.usage.completion_tokens,
                'total_tokens': resp.usage.total_tokens,
            }

            if finish_reason == "length":
                logger.warning(
                    "Response truncated (finish_reason=length) for chat_id=%s intent=%s "
                    "max_tokens=%d — consider increasing max_tokens.",
                    chat.id, intent, max_tokens
                )

            assistant_msg = MessageDemo.objects.create(
                chat=chat,
                role='assistant',
                content=assistant_text,
                # Task 2: store intent in metadata for debugging/analytics
                metadata={'openai_usage': usage, 'intent': intent, 'finish_reason': finish_reason},
            )
            
            # TRIGGER MEMORY UPDATE
            trigger_memory_update_async(user.id, chat.id)
 
            serializer = MessageSerializer(assistant_msg, context={'request': request})
            return Response(serializer.data, status=status.HTTP_201_CREATED)
 
        except Exception as e:
            logger.exception("OpenAI call failed: %s", e)
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)


# ====================== DELETE CHAT ======================
class DeleteChatAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, chat_pk):
        chat = get_object_or_404(ChatDemo, pk=chat_pk)
        chat.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ====================== USER OPENAI SETTINGS ======================
class UserOpenAISettingAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = resolve_request_user(request)
        setting, _ = UserOpenAISettingDemo.objects.get_or_create(user=user)
        return Response(UserOpenAISettingSerializer(setting).data)

    def post(self, request):
        user = resolve_request_user(request)
        setting, _ = UserOpenAISettingDemo.objects.get_or_create(user=user)
        serializer = UserOpenAISettingSerializer(setting, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


# ====================== BUSINESS CONTEXT MEMORY ======================
class BusinessContextMemoryAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        """
        Returns the active business-level context memory with attached documents.
        If none exists yet, creates and returns a default instance.
        """
        memory_id = request.query_params.get('id')
        if memory_id:
            memory = get_object_or_404(BusinessContextMemoryDemo, pk=memory_id)
        else:
            memory = BusinessContextMemoryDemo.objects.filter(is_active=True).first()
            if not memory:
                memory = BusinessContextMemoryDemo.objects.first()
            if not memory:
                user = resolve_request_user(request)
                memory = BusinessContextMemoryDemo.objects.create(
                    title="Company Business Memory",
                    content="",
                    is_active=True,
                    updated_by=user if user and user.is_authenticated else None,
                )
        return Response(BusinessContextMemorySerializer(memory, context={'request': request}).data)

    def post(self, request):
        """
        Updates or creates the business-level context memory.
        Supports:
        - Direct text (title, content)
        - Document uploads (document, file, documents, files) for Option 3 (Hybrid RAG):
          1. Chunks & generates embeddings for semantic vector search across all chats.
          2. Distills key organization rules & facts into memory content via LLM.
        """
        user = resolve_request_user(request)
        memory_id = request.data.get('id') or request.query_params.get('id')
        if memory_id:
            memory = get_object_or_404(BusinessContextMemoryDemo, pk=memory_id)
        else:
            memory = BusinessContextMemoryDemo.objects.filter(is_active=True).first() or BusinessContextMemoryDemo.objects.first()
            if not memory:
                memory = BusinessContextMemoryDemo.objects.create(
                    title=request.data.get('title') or "Company Business Memory",
                    content="",
                    is_active=True,
                    updated_by=user if user and user.is_authenticated else None,
                )

        # 1. Handle uploaded document(s)
        uploaded_files = []
        for key in ['document', 'file']:
            if key in request.FILES:
                uploaded_files.append(request.FILES[key])
        for key in ['documents', 'files']:
            if key in request.FILES:
                uploaded_files.extend(request.FILES.getlist(key))

        processed_docs = []
        for f in uploaded_files:
            try:
                doc = process_business_document_pipeline(
                    business_memory=memory,
                    file_obj=f,
                    uploaded_by=user if user and user.is_authenticated else None
                )
                processed_docs.append(doc.name)
            except Exception as e:
                logger.error(f"Error processing business document {f.name}: {e}")

        # 2. Handle manual text / updates
        manual_content = request.data.get('content')
        if manual_content:
            manual_str = str(manual_content).strip()
            if processed_docs:
                if manual_str and manual_str not in memory.content:
                    memory.content = (memory.content + "\n\n" + manual_str).strip()
            else:
                memory.content = manual_str

        title = request.data.get('title')
        if title:
            memory.title = title

        if 'is_active' in request.data:
            val = request.data['is_active']
            memory.is_active = val in (True, 'true', 'True', 1, '1')

        memory.updated_by = user if user and user.is_authenticated else None
        memory.save()

        return Response(
            BusinessContextMemorySerializer(memory, context={'request': request}).data,
            status=status.HTTP_200_OK
        )

    def put(self, request):
        return self.post(request)


class BusinessDocumentDeleteAPIView(APIView):
    """
    Deletes an uploaded business document and all its associated RAG chunks.
    """
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, document_id):
        doc = get_object_or_404(BusinessDocumentDemo, pk=document_id)
        doc_name = doc.name
        doc.delete()
        return Response(
            {"detail": f"Business document '{doc_name}' and all associated RAG chunks deleted successfully."},
            status=status.HTTP_200_OK
        )



# ====================== DOCUMENT PROCESSING STATUS ======================
class DocumentProcessingStatusAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, chat_pk):
        chat = get_object_or_404(ChatDemo, pk=chat_pk)
        
        try:
            doc_processing = getattr(chat, 'document_processing_demo', None)
            if doc_processing is None:
                doc_processing = DocumentProcessingDemo.objects.filter(chat=chat).first()

            if not doc_processing:
                return Response({'status': 'none', 'is_processing': chat.is_processing_document}, status=status.HTTP_200_OK)

            return Response({
                'status': doc_processing.status,
                'total_pages': doc_processing.total_pages,
                'processed_pages': doc_processing.processed_pages,
                'total_chunks': doc_processing.total_chunks,
                'error_message': doc_processing.error_message,
                'started_at': doc_processing.started_at,
                'completed_at': doc_processing.completed_at,
                'is_processing': chat.is_processing_document,
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.exception("Failed to get document processing status for chat_id=%s: %s", chat.id, e)
            return Response({'status': 'none', 'error': str(e)}, status=status.HTTP_200_OK)


import threading
from django.db import close_old_connections

MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB — adjust as needed


def _process_attachments_in_background(chat_id, files_data, has_user_prompt: bool = False):
    """
    Background worker thread to extract text, OCR if needed, chunk, embed, and store
    document chunks for RAG in the database without blocking the HTTP request.
    files_data: list of (file_bytes, attachment_name)
    has_user_prompt: if True, skips posting the 'You can now ask questions' message
                     because the streaming chat view will immediately answer the user prompt.
    """
    close_old_connections()
    chat = None
    doc_processing = None
    try:
        chat = ChatDemo.objects.get(pk=chat_id)
        chat.is_processing_document = True
        chat.save(update_fields=['is_processing_document'])

        doc_processing, _ = DocumentProcessingDemo.objects.get_or_create(chat=chat)
        doc_processing.status = 'processing'
        doc_processing.started_at = timezone.now()
        doc_processing.error_message = ''
        doc_processing.total_pages = 0
        doc_processing.processed_pages = 0
        doc_processing.total_chunks = 0
        doc_processing.completed_at = None
        doc_processing.save()

        total_chunks = 0
        total_pages = 0

        # Process multiple files concurrently to reduce total wait time
        if len(files_data) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(4, len(files_data))) as executor:
                futures = [
                    executor.submit(_run_pdf_processing_single, chat, fb, name)
                    for fb, name in files_data
                ]
                for f in futures:
                    try:
                        chunks_count, pages_count = f.result()
                        total_chunks += chunks_count
                        total_pages += pages_count
                        doc_processing.total_pages = total_pages
                        doc_processing.processed_pages = total_pages
                        doc_processing.total_chunks = total_chunks
                        doc_processing.save(update_fields=['total_pages', 'processed_pages', 'total_chunks'])
                    except Exception as fe:
                        logger.error("Parallel file processing error: %s", fe)
        else:
            for file_bytes, attachment_name in files_data:
                chunks_count, pages_count = _run_pdf_processing_single(chat, file_bytes, attachment_name)
                total_chunks += chunks_count
                total_pages += pages_count

                doc_processing.total_pages = total_pages
                doc_processing.processed_pages = total_pages
                doc_processing.total_chunks = total_chunks
                doc_processing.save(update_fields=['total_pages', 'processed_pages', 'total_chunks'])

        doc_processing.status = 'completed'
        doc_processing.completed_at = timezone.now()
        doc_processing.save()

        chat.has_document = total_chunks > 0
        chat.is_processing_document = False
        chat.save(update_fields=['has_document', 'is_processing_document'])

        # Only post generic upload message if the user did NOT supply a prompt
        if not has_user_prompt:
            file_list_str = "\n".join(f"- {name}" for _, name in files_data)
            if total_chunks > 0:
                MessageDemo.objects.create(
                    chat=chat,
                    role='assistant',
                    content=f"✅ {len(files_data)} file(s) processed and indexed ({total_chunks} chunks):\n{file_list_str}\n\nYou can now ask any questions about these documents!",
                )
            else:
                MessageDemo.objects.create(
                    chat=chat,
                    role='assistant',
                    content=f"⚠️ Uploaded file(s) processed, but no extractable text was found:\n{file_list_str}",
                )

    except Exception as e:
        logger.exception("Background document processing failed for chat_id=%s: %s", chat_id, e)
        if doc_processing is None:
            doc_processing = DocumentProcessingDemo.objects.filter(chat_id=chat_id).first()
        if doc_processing:
            doc_processing.status = 'failed'
            doc_processing.error_message = str(e)
            doc_processing.completed_at = timezone.now()
            doc_processing.save()

        ChatDemo.objects.filter(pk=chat_id).update(is_processing_document=False)
        chat = ChatDemo.objects.filter(pk=chat_id).first()
        if chat and not has_user_prompt:
            MessageDemo.objects.create(
                chat=chat,
                role='assistant',
                content=f"❌ Failed to process document(s): {type(e).__name__}: {str(e)}",
            )
    finally:
        close_old_connections()


def _assemble_prompt_and_rag_sync(
    chat,
    user_text,
    user_msg_pk,
    intent,
    relevant_memory,
    user_setting,
    query_embedding,
    has_image_attachment,
    image_b64,
    image_content_type,
    prev_generated_file_name,
):
    """
    Synchronously queries the database and assembles the messages_payload.
    Safe to execute in sync_to_async worker thread without SynchronousOnlyOperation errors.
    """
    rag_insufficient = False
    messages_payload = []

    has_business_docs = BusinessDocumentChunkDemo.objects.filter(
        business_document__business_memory__is_active=True
    ).exists()

    chat.refresh_from_db(fields=['has_document'])

    if (chat.has_document or has_business_docs) and query_embedding:
        try:
            max_chunks = getattr(user_setting, 'max_context_chunks', 5)

            # 1. Chat-specific RAG (ChatGPT Token Budget Threshold)
            if chat.has_document:
                total_chat_chunks = DocumentChunkDemo.objects.filter(chat=chat).count()
                if total_chat_chunks <= 30:
                    all_chunks = DocumentChunkDemo.objects.filter(chat=chat).order_by('page_number', 'chunk_index')
                    context_parts = [f"[Page {c.page_number}]\n{c.text}" for c in all_chunks]
                    rag_context = "\n\n" + "=" * 50 + "\n\n".join(context_parts)
                    messages_payload.append({
                        'role': 'system',
                        'content': f"🔗 Document Content (Complete Document):\n{rag_context}"
                    })
                else:
                    relevant_chunks, max_sim = get_relevant_chunks_for_query(chat, query_embedding, max_chunks=8)
                    is_generic_query = any(k in (user_text or '').lower() for k in [
                        'explain', 'summarize', 'what is', 'what are', 'tell me about', 'overview', 'review', 'read this', 'this image', 'screenshot'
                    ]) or len((user_text or '').strip().split()) <= 4

                    if relevant_chunks and (max_sim >= RAG_SIMILARITY_THRESHOLD or is_generic_query):
                        context_parts = [f"[Page {c.page_number}]\n{c.text}" for c in relevant_chunks]
                        rag_context = "\n\n" + "=" * 50 + "\n\n".join(context_parts)
                        messages_payload.append({
                            'role': 'system',
                            'content': f"🔗 Document excerpts:\n{rag_context}"
                        })
                    elif not has_image_attachment:
                        rag_insufficient = True
                        logger.info(
                            "RAG context weak in streaming view for chat_id=%s max_sim=%.3f",
                            chat.id, max_sim,
                        )

            # 2. Business-level RAG (Organization Knowledge Base)
            if has_business_docs:
                b_chunks, b_max_sim = get_relevant_business_chunks_for_query(query_embedding, max_chunks=3)
                if b_chunks and b_max_sim >= RAG_SIMILARITY_THRESHOLD:
                    b_context_parts = [
                        f"[{c.business_document.name} - Page {c.page_number}]\n{c.text}"
                        for c in b_chunks
                    ]
                    b_rag_context = "\n\n" + "=" * 50 + "\n\n".join(b_context_parts)
                    messages_payload.append({
                        'role': 'system',
                        'content': f"🏢 Organization Knowledge Base Excerpts:\n{b_rag_context}"
                    })

        except Exception as e:
            logger.error("RAG retrieval failed in streaming view: %s", e)

    # Build intent-adaptive system prompt
    intent_system_prompt = build_system_prompt(intent, rag_insufficient=rag_insufficient)
    custom_chat_prompt = chat.system_prompt or ""
    if custom_chat_prompt:
        intent_system_prompt = custom_chat_prompt + "\n\n" + intent_system_prompt
    business_mem_str = BusinessContextMemoryDemo.get_active_memory()
    if business_mem_str:
        intent_system_prompt += f"\n\n[BUSINESS CONTEXT MEMORY (Organization Rules & Ground Truth)]\n{business_mem_str}\n"
    if relevant_memory:
        intent_system_prompt += f"\n\n[USER CONTEXT MEMORY (Past Facts & Preferences)]\n{relevant_memory}\n"

    if has_image_attachment:
        intent_system_prompt += (
            "\n\n[USER IMAGE ATTACHMENT: An image/screenshot is provided directly in the user message. "
            "Visually examine the image content to answer questions, explain code or UI, and provide detailed insights accurately.]\n"
        )

    # Files note for sandbox
    att_names = []
    tpl_report = ""
    try:
        recent_att_msgs = list(
            chat.messages_demo.exclude(attachment='').exclude(attachment__isnull=True).order_by('-created_at')[:5]
        )
        for _am in recent_att_msgs:
            _n = os.path.basename(_am.attachment_name or '')
            if _n and _n not in att_names:
                att_names.append(_n)
                if _n.lower().endswith('.pptx') and not tpl_report:
                    try:
                        from .pptx_quality import describe_template
                        with _am.attachment.open('rb') as _fh:
                            tpl_report = describe_template(_fh.read(), _n)
                    except Exception as _te:
                        logger.warning("Could not build template report for %s: %s", _n, _te)
    except Exception as _e:
        logger.warning("Failed to collect recent attachment names: %s", _e)

    if prev_generated_file_name:
        _pn = os.path.basename(prev_generated_file_name)
        if _pn not in att_names:
            att_names.append(_pn)
    intent_system_prompt += files_note(att_names) + tpl_report

    messages_payload.insert(0, {'role': 'system', 'content': intent_system_prompt})

    # Conversation history
    recent_messages = list(chat.messages_demo.all().order_by('-created_at')[:10])
    for m in reversed(recent_messages):
        if m.pk != user_msg_pk:
            messages_payload.append({'role': m.role, 'content': m.content or ''})

    if has_image_attachment and image_b64:
        messages_payload.append({
            'role': 'user',
            'content': [
                {"type": "text", "text": user_text or "Please analyze and explain this uploaded image."},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{image_content_type};base64,{image_b64}"
                    }
                }
            ]
        })
    else:
        messages_payload.append({'role': 'user', 'content': user_text})

    return messages_payload


# ====================== STREAMING CHAT (SSE) ======================
class StreamingChatAPIView(APIView):
    """
    POST /api/v1/chats/<chat_pk>/messages/stream/
    The complete assistant message is saved to the DB once the stream finishes.
    """
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, chat_pk):
        chat = get_object_or_404(ChatDemo, pk=chat_pk)
        user = resolve_request_user(request, chat=chat)

        user_text = request.data.get('content', '').strip()
        attachments = request.FILES.getlist('attachment')
        auto_generated_prompt = False

        first_attachment = attachments[0] if attachments else None

        if not user_text and not attachments:
            return Response(
                {'error': 'content or attachment is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        attachment_name = (getattr(first_attachment, 'name', '') if first_attachment else '')
        attachment_content_type = (getattr(first_attachment, 'content_type', '') if first_attachment else '')
        allowed_ext = ('.pdf', '.docx', '.xlsx', '.xls', '.pptx', '.ppt', '.csv', '.txt', '.jpg', '.jpeg', '.png', '.webp')

        # --- Handle file upload for RAG (non-blocking background processing) ---
        if attachments:
            try:
                # Delete old chunks ONCE
                DocumentChunkDemo.objects.filter(chat=chat).delete()

                queued_files = []
                rejected_files = []
                valid_files_data = []

                for attachment in attachments:
                    name = attachment.name
                    content_type = attachment.content_type

                    if not name.lower().endswith(allowed_ext):
                        continue

                    if attachment.size > MAX_UPLOAD_SIZE:
                        rejected_files.append(
                            f"{name} ({attachment.size // 1024 // 1024}MB > "
                            f"{MAX_UPLOAD_SIZE // 1024 // 1024}MB limit)"
                        )
                        continue

                    file_bytes = attachment.read()
                    attachment.seek(0)
                    MessageDemo.objects.create(
                        chat=chat,
                        role='user',
                        content=f"[Uploaded file: {name}]",
                        attachment=attachment,
                        attachment_name=name,
                        attachment_content_type=content_type,
                    )

                    valid_files_data.append((file_bytes, name))
                    queued_files.append(name)

                if rejected_files:
                    MessageDemo.objects.create(
                        chat=chat,
                        role='assistant',
                        content=(
                            "⚠️ Some file(s) were too large and were skipped:\n"
                            + "\n".join(rejected_files)
                        ),
                    )

                if valid_files_data:
                    chat.is_processing_document = True
                    chat.save(update_fields=['is_processing_document'])

                    doc_proc, _ = DocumentProcessingDemo.objects.get_or_create(chat=chat)
                    doc_proc.status = 'processing'
                    doc_proc.started_at = timezone.now()
                    doc_proc.error_message = ''
                    doc_proc.total_pages = 0
                    doc_proc.processed_pages = 0
                    doc_proc.total_chunks = 0
                    doc_proc.completed_at = None
                    doc_proc.save()

                    threading.Thread(
                        target=_process_attachments_in_background,
                        args=(chat.id, valid_files_data, bool(user_text)),
                        daemon=True
                    ).start()

                if not user_text:
                    if queued_files:
                        completion_msg = MessageDemo.objects.create(
                            chat=chat,
                            role='assistant',
                            content=(
                                f"✅ Uploaded {len(queued_files)} file(s):\n"
                                + "\n".join(f"- {n}" for n in queued_files)
                                + "\n\n⏳ Processing and indexing your documents in the background. You can track progress or start asking questions/instructions."
                            ),
                        )
                    else:
                        completion_msg = MessageDemo.objects.create(
                            chat=chat,
                            role='assistant',
                            content="⚠️ No valid files to process.",
                        )
                    serializer = MessageSerializer(completion_msg, context={'request': request})
                    return Response(serializer.data, status=status.HTTP_201_CREATED)

            except Exception as e:
                logger.exception("Failed during file upload preparation for chat_id=%s: %s", chat.id, e)
                error_msg = MessageDemo.objects.create(
                    chat=chat,
                    role='assistant',
                    content=f"❌ Failed to process file upload: {type(e).__name__}: {str(e)}",
                )
                serializer = MessageSerializer(error_msg, context={'request': request})
                return Response(serializer.data, status=status.HTTP_400_BAD_REQUEST)

        # --- Save user message ---
        existing_user_msg = MessageDemo.objects.filter(
            chat=chat, role='user'
        ).order_by('-created_at').first()

        if existing_user_msg and existing_user_msg.content == user_text:
            user_msg = existing_user_msg
        else:
            user_msg = MessageDemo.objects.create(
                chat=chat,
                role='user',
                content=user_text or f"[Uploaded file: {attachment_name}]",
                attachment=first_attachment if first_attachment else None,
                attachment_name=attachment_name,
                attachment_content_type=attachment_content_type,
                metadata={'auto_generated': True} if auto_generated_prompt else {},
            )

        # ── Look up previously generated file so the sandbox can pre-load it ──
        prev_generated_file_path = None
        prev_generated_file_name = None
        try:
            for _prev_msg in chat.messages_demo.filter(role='assistant').order_by('-created_at')[:20]:
                _pmeta = _prev_msg.metadata or {}
                if _pmeta.get('generated_file_path') and _pmeta.get('generated_file_name'):
                    prev_generated_file_path = _pmeta['generated_file_path']
                    prev_generated_file_name = _pmeta['generated_file_name']
                    break
        except Exception as _e:
            logger.warning("Failed to look up prev generated file: %s", _e)

        # --- Build messages_payload with intent-adaptive prompt + RAG ---
        user_setting = getattr(user, 'openai_setting', None)

        # --- Check for image attachment to provide multimodal vision ---
        image_exts = ('.png', '.jpg', '.jpeg', '.webp', '.gif')
        active_image_att = None
        if attachments:
            for att in attachments:
                if any(att.name.lower().endswith(ext) for ext in image_exts):
                    active_image_att = att
                    break
        elif user_msg and user_msg.attachment and any(user_msg.attachment_name.lower().endswith(ext) for ext in image_exts):
            active_image_att = user_msg.attachment

        has_image_attachment = False
        image_b64 = None
        image_content_type = "image/png"
        if active_image_att:
            try:
                import base64
                if hasattr(active_image_att, 'seek'):
                    active_image_att.seek(0)
                    img_data = active_image_att.read()
                    active_image_att.seek(0)
                else:
                    with active_image_att.open('rb') as _f:
                        img_data = _f.read()
                image_b64 = base64.b64encode(img_data).decode('ascii')
                image_content_type = getattr(active_image_att, 'content_type', '') or getattr(user_msg, 'attachment_content_type', '') or 'image/png'
                has_image_attachment = True
            except Exception as _img_err:
                logger.warning("Failed to prepare image base64 in StreamingChatAPIView: %s", _img_err)

        # Task 2: Classify intent and extract relevant memory for adaptive prompting
        recent_history_for_classifier = list(
            chat.messages_demo.order_by('-created_at')[:4].values('role', 'content')
        )
        context_mem_str = getattr(user_setting, 'context_memory', "") if user_setting else ""
        business_mem_str = BusinessContextMemoryDemo.get_active_memory()
        combined_mem_for_classifier = ""
        if business_mem_str:
            combined_mem_for_classifier += f"[BUSINESS CONTEXT]\n{business_mem_str}\n\n"
        if context_mem_str:
            combined_mem_for_classifier += f"[USER CONTEXT]\n{context_mem_str}"

        intent, relevant_memory = classify_intent(
            user_message=user_text,
            recent_history=recent_history_for_classifier,
            context_memory=combined_mem_for_classifier.strip(),
            api_key=getattr(settings, 'OPENAI_API_KEY', None),
        )

        # Task 1+3: Model params with reasoning_effort + per-intent max_tokens
        model = (
            request.data.get('model') or
            (user_setting.default_model if user_setting else None) or
            "gpt-4o"
        )
        user_max_tokens = int(request.data.get('max_tokens') or getattr(user_setting, 'max_tokens', 2000))
        # The code tool is always available, so always reserve the (larger) document budget.
        max_tokens = max(
            get_intent_max_tokens(intent, user_max_tokens),
            get_intent_max_tokens('document_generation', user_max_tokens),
        )
        temperature = float(request.data.get('temperature') or getattr(user_setting, 'temperature', 0.8))
        is_reasoning = (
            getattr(user_setting, 'is_reasoning_model', lambda: False)()
            or str(model).startswith(('o1', 'o3', 'o4', 'gpt-5'))
        )
        reasoning_effort = getattr(user_setting, 'reasoning_effort', 'medium')

        logger.debug(
            "Streaming to OpenAI → model=%s intent=%s max_tokens=%s reasoning=%s",
            model, intent, max_tokens, is_reasoning,
        )

        # --- SSE generator with document generation support ---
        async def event_stream():
            import time
            import asyncio
            import base64 as _b64_module
            from asgiref.sync import sync_to_async
            from openai import AsyncOpenAI
            from django.core.files.storage import default_storage as _default_storage
            from ask_ai_demo.sse_events import (
                sse, sse_token, sse_done, sse_error, sse_status,
                TOKEN, STATUS, TOOL_CALL, SANDBOX_START, SANDBOX_PROGRESS,
                SANDBOX_COMPLETE, DOCUMENT_START, DOCUMENT_COMPLETE,
                RETRY, ERROR, FINAL, DONE,
            )

            # Tracks the files generated during this stream session
            generated_files_meta = []

            # ------------------------------------------------------------------
            # Step 1: Background indexing wait loop with keep-alive heartbeats
            # Never timeout (prevents 504) and never drop user prompt!
            # ------------------------------------------------------------------
            await sync_to_async(chat.refresh_from_db)(fields=['is_processing_document', 'has_document'])
            if chat.is_processing_document and intent != 'document_generation':
                yield sse(STATUS, {"stage": "indexing", "message": "⏳ Processing and indexing your uploaded document(s)..."})
                wait_start = time.time()
                last_reported_pages = -1

                while True:
                    await asyncio.sleep(1.5)
                    await sync_to_async(chat.refresh_from_db)(fields=['is_processing_document', 'has_document'])
                    if not chat.is_processing_document:
                        break

                    doc_proc = await sync_to_async(DocumentProcessingDemo.objects.filter(chat=chat).first)()
                    if doc_proc:
                        if doc_proc.status == 'failed':
                            err = doc_proc.error_message or "Document processing failed"
                            yield sse_error(f"❌ Document indexing failed: {err}")
                            yield sse_done()
                            return

                        p_done = doc_proc.processed_pages
                        p_total = doc_proc.total_pages
                        if p_done != last_reported_pages and p_done > 0:
                            last_reported_pages = p_done
                            msg = f"⏳ Indexing documents ({p_done}/{p_total or '...'} pages)..." if p_total else f"⏳ Indexing documents ({p_done} pages)..."
                            yield sse(STATUS, {"stage": "indexing", "message": msg})
                        else:
                            yield sse(STATUS, {"stage": "heartbeat", "message": "⏳ Indexing in progress..."})

                    if time.time() - wait_start > 300:
                        yield sse(STATUS, {"stage": "warning", "message": "⚠️ Document indexing took longer than expected. Proceeding with analysis..."})
                        break

                yield sse(STATUS, {"stage": "indexing_done", "message": "✅ Documents indexed. Analyzing and generating answer..."})

            # ------------------------------------------------------------------
            # Step 2: Build messages_payload now that all chunks exist in DB
            # ------------------------------------------------------------------
            client = AsyncOpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))

            query_embedding = None
            has_docs = await sync_to_async(
                lambda: chat.has_document or BusinessDocumentChunkDemo.objects.filter(
                    business_document__business_memory__is_active=True
                ).exists()
            )()

            if has_docs and user_text:
                try:
                    emb_response = await client.embeddings.create(
                        model="text-embedding-3-small",
                        input=[user_text],
                        encoding_format="float",
                    )
                    query_embedding = emb_response.data[0].embedding
                except Exception as e:
                    logger.error("Embedding creation failed in streaming view: %s", e)

            messages_payload = await sync_to_async(_assemble_prompt_and_rag_sync)(
                chat=chat,
                user_text=user_text,
                user_msg_pk=user_msg.pk,
                intent=intent,
                relevant_memory=relevant_memory,
                user_setting=user_setting,
                query_embedding=query_embedding,
                has_image_attachment=has_image_attachment,
                image_b64=image_b64,
                image_content_type=image_content_type,
                prev_generated_file_name=prev_generated_file_name,
            )

            # ------------------------------------------------------------------
            # Input files are loaded LAZILY (only if the model runs code) and the
            # generated file is saved through save_file().  Everything else
            # (model loop, sandbox, retries) lives in chat_agent.py.
            # ------------------------------------------------------------------
            async def load_input_files():
                def _read_all():
                    found = {}
                    try:
                        recent = (
                            chat.messages_demo.exclude(attachment='').exclude(attachment__isnull=True)
                            .order_by('-created_at')[:5]
                        )
                        for am in recent:
                            n = os.path.basename(am.attachment_name or '')
                            if not n or n in found:
                                continue
                            try:
                                with am.attachment.open('rb') as fh:
                                    found[n] = fh.read()
                            except Exception as e:
                                logger.warning("Failed to load attachment %s: %s", n, e)
                    except Exception as e:
                        logger.warning("Failed to collect recent attachments: %s", e)
                    if prev_generated_file_path and prev_generated_file_name:
                        n = os.path.basename(prev_generated_file_name)
                        try:
                            with _default_storage.open(prev_generated_file_path) as fh:
                                found.setdefault(n, fh.read())
                        except Exception as e:
                            logger.warning("Could not load previous file '%s': %s", prev_generated_file_path, e)
                    return found
                return await sync_to_async(_read_all)()

            async def save_file(name, data):
                import uuid
                from django.core.files.base import ContentFile
                safe_name = os.path.basename(name) or "document"
                # The unique ID goes in a FOLDER, so the file name itself stays clean:
                # generated_docs/956d4731/ABC_Healthcare_Presentation.pptx
                path = await sync_to_async(_default_storage.save)(
                    f"generated_docs/{uuid.uuid4().hex[:8]}/{safe_name}", ContentFile(data)
                )
                media_url = getattr(settings, 'MEDIA_URL', '/media/')
                return {"path": path, "url": f"{media_url}{path}"}

            full_text = []
            client = AsyncOpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))

            try:
                # Responses API: max_output_tokens ALSO covers reasoning tokens, so reasoning models get headroom.
                base_kwargs = {
                    "model": model,
                    "max_output_tokens": max_tokens + (
                        getattr(settings, 'REASONING_TOKEN_HEADROOM', 8000) if is_reasoning else 0
                    ),
                }
                if is_reasoning:
                    base_kwargs["reasoning"] = {"effort": reasoning_effort or "medium"}
                else:
                    base_kwargs["temperature"] = temperature

                agent_result = {}
                async for ev in run_chat_agent(
                    client=client,
                    base_kwargs=base_kwargs,
                    messages=messages_payload,
                    load_input_files=load_input_files,
                    sandbox_url=getattr(settings, 'AZURE_SANDBOX_START_URL', None),
                    save_file=save_file,
                    result=agent_result,
                    text_sink=full_text,
                    # The MODEL decides whether a question needs web search (no user switch).
                    web_search=getattr(settings, 'WEB_SEARCH_ENABLED', True),
                    web_search_options=getattr(settings, 'WEB_SEARCH_OPTIONS', None),
                    max_tool_calls=getattr(settings, 'WEB_SEARCH_MAX_CALLS', 3),
                    store=getattr(settings, 'OPENAI_STORE_RESPONSES', False),
                ):
                    yield ev

                assistant_text = "".join(full_text)
                finish_reason = agent_result.get("finish")
                generated_files_meta = agent_result.get("files", [])

                if finish_reason == "length":
                    logger.warning(
                        "Stream truncated (finish_reason=length) chat_id=%s intent=%s max_tokens=%d",
                        chat.id, intent, max_tokens
                    )
                    yield sse(STATUS, {
                        "stage": "warning",
                        "message": "⚠️ Response was cut off due to length. Please ask me to continue.",
                    })

                # Save the assistant message (include download link in content so it's
                # preserved in chat history and visible on page reload)
                saved_content = assistant_text
                if generated_files_meta:
                    _media_url = getattr(settings, 'MEDIA_URL', '/media/')
                    for file_meta in generated_files_meta:
                        _dl_url = f"{_media_url}{file_meta['path']}"
                        _dl_link = (
                            f"\n\n📄 **Your document is ready!** "
                            f"[⬇ Download {file_meta['filename']}]({_dl_url})"
                        )
                        saved_content = (saved_content.rstrip() + _dl_link)

                last_generated_file_meta = generated_files_meta[-1] if generated_files_meta else None

                await MessageDemo.objects.acreate(
                    chat=chat,
                    role='assistant',
                    content=saved_content,
                    metadata={
                        'streaming': True,
                        'intent': intent,
                        'finish_reason': finish_reason,
                        # ✅ Persist generated file info so the next request can pre-load it
                        **(
                            {
                                'generated_file_path': last_generated_file_meta['path'],
                                'generated_file_name': last_generated_file_meta['filename'],
                            }
                            if last_generated_file_meta else {}
                        ),
                    },
                )
                
                # TRIGGER MEMORY UPDATE
                await sync_to_async(trigger_memory_update_async)(user.id, chat.id)

            except Exception as e:
                logger.exception(
                    "Streaming OpenAI call failed for chat_id=%s: %s", chat.id, e
                )
                yield sse(ERROR, {"message": str(e)})

            finally:
                # Emit the complete AI text as a final event so the frontend
                # can use it as a single source of truth (useful on reconnect).
                final_text = "".join(full_text) if full_text else ""
                if final_text:
                    yield sse(FINAL, {"content": final_text})
                yield sse_done()

        response = StreamingHttpResponse(
            event_stream(),
            content_type='text/event-stream',
        )
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        return response

    
class EditAndResendAPIView(APIView):

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def patch(self, request, chat_pk, message_pk):
        user = request.user

        chat = get_object_or_404(
            ChatDemo,
            pk=chat_pk,
            owner=user
        )

        new_content = (
            request.data.get("content")
            or request.GET.get("content")
            or ""
        ).strip()

        if not new_content:
            return Response(
                {"error": "content is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            msg = MessageDemo.objects.get(
                pk=message_pk,
                chat=chat
            )
        except MessageDemo.DoesNotExist:
            return Response(
                {"detail": "Message not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # If assistant message supplied, find preceding user message
        if msg.role == "assistant":
            user_msg = (
                chat.messages_demo.filter(
                    role="user",
                    created_at__lt=msg.created_at
                )
                .order_by("-created_at")
                .first()
            )

            if not user_msg:
                return Response(
                    {"detail": "No preceding user message found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            msg = user_msg

        elif msg.role != "user":
            return Response(
                {"detail": "Message must be a user message."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:

            with transaction.atomic():

                # Save edit history
                metadata = msg.metadata or {}

                metadata.setdefault("edits", []).append({
                    "original": msg.content,
                    "edited_at": timezone.now().isoformat(),
                    "editor_id": user.id,
                })

                msg.content = new_content
                msg.metadata = metadata
                msg.edited = True
                msg.edited_at = timezone.now()
                msg.save()

                # ==================================================
                # IMPORTANT:
                # Delete everything after edited message
                # ==================================================
                MessageDemo.objects.filter(
                    chat=chat,
                    created_at__gt=msg.created_at
                ).delete()

            # ==================================================
            # Build payload only up to edited message (intent-adaptive)
            # ==================================================
            user_setting = getattr(user, 'openai_setting', None)

            # Task 2: Re-classify intent on the EDITED content (not the original)
            recent_history_for_classifier = list(
                chat.messages_demo
                .filter(created_at__lt=msg.created_at)
                .order_by('-created_at')[:4]
                .values('role', 'content')
            )
            context_mem_str = getattr(user_setting, 'context_memory', "") if user_setting else ""
            business_mem_str = BusinessContextMemoryDemo.get_active_memory()
            combined_mem_for_classifier = ""
            if business_mem_str:
                combined_mem_for_classifier += f"[BUSINESS CONTEXT]\n{business_mem_str}\n\n"
            if context_mem_str:
                combined_mem_for_classifier += f"[USER CONTEXT]\n{context_mem_str}"

            intent, relevant_memory = classify_intent(
                user_message=new_content,
                recent_history=recent_history_for_classifier,
                context_memory=combined_mem_for_classifier.strip(),
                api_key=getattr(settings, 'OPENAI_API_KEY', None),
            )

            # Task 2: Build intent-adaptive system prompt
            intent_system_prompt = build_system_prompt(intent)
            custom_chat_prompt = chat.system_prompt or ""
            if custom_chat_prompt:
                intent_system_prompt = custom_chat_prompt + "\n\n" + intent_system_prompt
            business_mem_str = BusinessContextMemoryDemo.get_active_memory()
            if business_mem_str:
                intent_system_prompt += f"\n\n[BUSINESS CONTEXT MEMORY (Organization Rules & Ground Truth)]\n{business_mem_str}\n"
            if relevant_memory:
                intent_system_prompt += f"\n\n[USER CONTEXT MEMORY (Past Facts & Preferences)]\n{relevant_memory}\n"

            messages_payload = [
                {"role": "system", "content": intent_system_prompt}
            ]

            conversation_messages = (
                chat.messages_demo
                .filter(created_at__lte=msg.created_at)
                .order_by("created_at")
            )

            for m in conversation_messages:
                content = m.content or ""

                if m.attachment:
                    attachment_note = (
                        f"[Attachment: {m.attachment_name} | "
                        f"content-type: {m.attachment_content_type}]"
                    )

                    content = (
                        f"{content}\n\n{attachment_note}"
                    ).strip()

                messages_payload.append({
                    "role": m.role,
                    "content": content
                })

            # Task 1+3: reasoning_effort + per-intent max_tokens
            model = (
                request.data.get("model")
                or getattr(user_setting, "default_model", None)
                or "gpt-4o"
            )

            user_max_tokens = int(
                request.data.get("max_tokens")
                or getattr(user_setting, "max_tokens", 2000)
            )
            max_tokens = get_intent_max_tokens(intent, user_max_tokens)

            temperature = float(
                request.data.get("temperature")
                or getattr(user_setting, "temperature", 0.7)
            )
            is_reasoning = getattr(user_setting, 'is_reasoning_model', lambda: False)()
            reasoning_effort = getattr(user_setting, 'reasoning_effort', 'medium')

            logger.info(
                "Edit & Resend -> chat=%s model=%s intent=%s max_tokens=%d",
                chat.id, model, intent, max_tokens
            )

            client = AsyncOpenAI(
                api_key=getattr(
                    settings,
                    "OPENAI_API_KEY",
                    None
                )
            )

            async def event_stream():
                full_text = []

                try:
                    # Task 1: conditionally send reasoning_effort or temperature
                    call_kwargs: dict = {
                        "model": model,
                        "messages": messages_payload,
                        "max_tokens": max_tokens,
                        "stream": True,
                    }
                    if is_reasoning:
                        call_kwargs["reasoning_effort"] = reasoning_effort
                    else:
                        call_kwargs["temperature"] = temperature

                    stream = await client.chat.completions.create(**call_kwargs)

                    async for chunk in stream:
                        delta = (
                            chunk.choices[0].delta
                            if chunk.choices
                            else None
                        )

                        if delta and delta.content:
                            text_piece = delta.content
                            full_text.append(text_piece)
                            escaped = text_piece.replace("\n", "\\n")
                            yield f"data: {escaped}\n\n"

                    assistant_text = "".join(full_text)

                    await MessageDemo.objects.acreate(
                        chat=chat,
                        role="assistant",
                        content=assistant_text,
                        metadata={
                            "streaming": True,
                            "edited_from_message": msg.pk,
                            "intent": intent,
                        }
                    )

                    # TRIGGER MEMORY UPDATE
                    await sync_to_async(trigger_memory_update_async)(user.id, chat.id)

                except Exception as e:
                    logger.exception(
                        "Edit & resend streaming failed: %s",
                        e
                    )
                    yield f"data: [ERROR] {str(e)}\n\n"

                finally:
                    yield "data: [DONE]\n\n"

            response = StreamingHttpResponse(
                event_stream(),
                content_type="text/event-stream"
            )

            response["Cache-Control"] = "no-cache"
            response["X-Accel-Buffering"] = "no"

            return response

        except Exception as e:
            logger.exception(
                "Edit and resend failed: %s",
                e
            )

            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

class ChatNameListAPIView(generics.ListAPIView):
    serializer_class = ChatNameSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ChatDemo.objects.filter(
            owner=self.request.user
        ).order_by('-updated_at').only('id', 'title', 'created_at', 'updated_at')
        
class ClearChatSessionAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, chat_pk):
        chat = get_object_or_404(
            ChatDemo,
            pk=chat_pk,
            owner=request.user
        )

        deleted_count, _ = MessageDemo.objects.filter(
            chat=chat
        ).delete()

        # Optional: clear document chunks too
        chat.has_document = False
        chat.save(update_fields=['has_document'])

        return Response({
            "success": True,
            "message": "Chat history cleared successfully.",
            "deleted_messages": deleted_count
        }, status=status.HTTP_200_OK)

class UpdateGlobalOpenAISettingsAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        """
        Returns the current global OpenAI settings.
        Assumes all users have the same settings.
        """
        setting = UserOpenAISettingDemo.objects.first()

        if not setting:
            return Response(
                {"detail": "No OpenAI settings found."},
                status=status.HTTP_404_NOT_FOUND
            )

        return Response({
            "default_model": setting.default_model,
            "temperature": setting.temperature,
            "max_tokens": setting.max_tokens,
            "use_rag_for_documents": setting.use_rag_for_documents,
            "max_context_chunks": setting.max_context_chunks,
            "total_users": UserOpenAISettingDemo.objects.count()
        })

    def post(self, request):
        update_data = {}
        fields = [
            "default_model",
            "temperature",
            "max_tokens",
            "use_rag_for_documents",
            "max_context_chunks",
        ]
        for field in fields:
            if field in request.data:
                update_data[field] = request.data[field]

        if not update_data:
            return Response(
                {"detail": "No fields supplied."},
                status=status.HTTP_400_BAD_REQUEST
            )

        User = get_user_model()
        existing_user_ids = set(
            UserOpenAISettingDemo.objects.values_list('user_id', flat=True)
        )
        missing_users = list(User.objects.exclude(id__in=existing_user_ids))
        backfilled_count = len(missing_users)

        if missing_users:
            UserOpenAISettingDemo.objects.bulk_create(
                [UserOpenAISettingDemo(user=u) for u in missing_users],
                ignore_conflicts=True,
            )

        updated = UserOpenAISettingDemo.objects.update(**update_data)

        return Response({
            "success": True,
            "updated_users": updated,
            "updated_fields": update_data,
            "backfilled_users": backfilled_count,
        })
