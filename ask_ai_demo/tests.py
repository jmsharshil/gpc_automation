# """
# crm_chat/tests.py
# =================
# Tests covering:
#   - Intent classification returns one of the four valid labels (Task 2)
#   - Per-intent max_tokens selection (Task 3)
#   - RAG insufficient-context flag triggers correct prompt branch (Task 4)

# All OpenAI calls are mocked — no live API calls are made.
# """
# from unittest.mock import MagicMock, patch

# from django.test import TestCase

# from .prompts import (
#     VALID_INTENTS,
#     RAG_SIMILARITY_THRESHOLD,
#     INTENT_MAX_TOKENS,
#     build_system_prompt,
#     classify_intent,
#     get_intent_max_tokens,
#     RAG_INSUFFICIENT_PROMPT,
#     CORE_SYSTEM_PROMPT,
# )


# # ---------------------------------------------------------------------------
# # Helper: build a fake OpenAI completion response for the classifier
# # ---------------------------------------------------------------------------
# def _make_openai_response(content: str):
#     """Return a minimal mock that looks like openai.ChatCompletion."""
#     choice = MagicMock()
#     choice.message.content = content
#     resp = MagicMock()
#     resp.choices = [choice]
#     return resp


# # ---------------------------------------------------------------------------
# # Task 2: Intent classification
# # ---------------------------------------------------------------------------
# class IntentClassificationTests(TestCase):

#     def _classify(self, user_message: str, mock_response_text: str) -> str:
#         """Helper that patches the OpenAI client and runs classify_intent."""
#         with patch("openai.OpenAI") as MockOpenAI:
#             mock_client = MagicMock()
#             MockOpenAI.return_value = mock_client
#             mock_client.chat.completions.create.return_value = _make_openai_response(mock_response_text)
#             return classify_intent(user_message=user_message, api_key="test-key")

#     def test_code_intent(self):
#         """A code snippet request should return 'code'."""
#         result = self._classify(
#             "Write me a Python function to parse JSON from an API response",
#             "code"
#         )
#         self.assertEqual(result, "code")

#     def test_document_generation_intent(self):
#         """A file-generation request should return 'document_generation'."""
#         result = self._classify(
#             "Generate an Excel LBO model with five year projections and download it",
#             "document_generation"
#         )
#         self.assertEqual(result, "document_generation")

#     def test_financial_analysis_intent(self):
#         """A valuation question should return 'financial_analysis'."""
#         result = self._classify(
#             "What is the current EV/EBITDA multiple for SaaS companies in the mid-market?",
#             "financial_analysis"
#         )
#         self.assertEqual(result, "financial_analysis")

#     def test_general_intent(self):
#         """A casual question should return 'general'."""
#         result = self._classify(
#             "What is the capital of France?",
#             "general"
#         )
#         self.assertEqual(result, "general")

#     def test_unknown_label_falls_back_to_general(self):
#         """If the model returns an unrecognised label, fall back to 'general'."""
#         result = self._classify("Hello", "foobar")
#         self.assertEqual(result, "general")

#     def test_all_valid_intents_are_accepted(self):
#         """Every label in VALID_INTENTS should be returned unchanged."""
#         for label in VALID_INTENTS:
#             result = self._classify("test", label)
#             self.assertEqual(result, label)

#     def test_classifier_error_falls_back_to_general(self):
#         """If the OpenAI call raises, fall back gracefully to 'general'."""
#         with patch("openai.OpenAI") as MockOpenAI:
#             mock_client = MagicMock()
#             MockOpenAI.return_value = mock_client
#             mock_client.chat.completions.create.side_effect = Exception("network error")
#             result = classify_intent(user_message="hello", api_key="test-key")
#         self.assertEqual(result, "general")


# # ---------------------------------------------------------------------------
# # Task 3: Per-intent max_tokens selection
# # ---------------------------------------------------------------------------
# class PerIntentMaxTokensTests(TestCase):

#     def test_document_generation_floor(self):
#         """document_generation must always use at least its intent floor."""
#         floor = INTENT_MAX_TOKENS["document_generation"]  # 12000
#         self.assertGreaterEqual(floor, 12000)
#         result = get_intent_max_tokens("document_generation", user_max_tokens=1024)
#         self.assertEqual(result, floor)

#     def test_financial_analysis_floor(self):
#         """financial_analysis must always use at least its intent floor."""
#         floor = INTENT_MAX_TOKENS["financial_analysis"]  # 6000
#         result = get_intent_max_tokens("financial_analysis", user_max_tokens=1024)
#         self.assertEqual(result, floor)

#     def test_user_value_raises_floor(self):
#         """A user max_tokens value above the intent floor should win."""
#         floor = INTENT_MAX_TOKENS["code"]  # 4000
#         user_val = floor + 2000
#         result = get_intent_max_tokens("code", user_max_tokens=user_val)
#         self.assertEqual(result, user_val)

#     def test_general_uses_user_value_when_higher(self):
#         """general intent should still respect a user value above its floor."""
#         result = get_intent_max_tokens("general", user_max_tokens=5000)
#         self.assertEqual(result, 5000)


# # ---------------------------------------------------------------------------
# # Task 4: RAG insufficient-context flag
# # ---------------------------------------------------------------------------
# class RAGInsufficiencyPromptTests(TestCase):

#     def test_rag_insufficient_flag_adds_warning(self):
#         """When rag_insufficient=True, the system prompt must contain the warning text."""
#         prompt = build_system_prompt("general", rag_insufficient=True)
#         self.assertIn("document context was insufficient", prompt)
#         self.assertIn(RAG_INSUFFICIENT_PROMPT.strip()[:30], prompt)

#     def test_rag_sufficient_no_warning(self):
#         """When rag_insufficient=False (default), no warning should be injected."""
#         prompt = build_system_prompt("general", rag_insufficient=False)
#         self.assertNotIn("document context was insufficient", prompt)

#     def test_rag_threshold_constant_in_range(self):
#         """RAG_SIMILARITY_THRESHOLD must be a float between 0 and 1."""
#         self.assertIsInstance(RAG_SIMILARITY_THRESHOLD, float)
#         self.assertGreater(RAG_SIMILARITY_THRESHOLD, 0.0)
#         self.assertLess(RAG_SIMILARITY_THRESHOLD, 1.0)

#     def test_core_prompt_present_in_all_intents(self):
#         """CORE_SYSTEM_PROMPT should be the prefix for every intent prompt."""
#         for intent in VALID_INTENTS:
#             prompt = build_system_prompt(intent)
#             self.assertTrue(
#                 prompt.startswith(CORE_SYSTEM_PROMPT[:50]),
#                 f"CORE_SYSTEM_PROMPT missing from intent '{intent}'"
#             )
