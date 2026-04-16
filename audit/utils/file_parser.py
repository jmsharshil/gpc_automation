# utils/file_parser.py

import PyPDF2
import docx
import re

def extract_text_from_pdf(file):
    reader = PyPDF2.PdfReader(file)
    text = ""
    for page in reader.pages:
        text += page.extract_text() or ""
    return text


def extract_text_from_docx(file):
    doc = docx.Document(file)
    return "\n".join([p.text for p in doc.paragraphs])


def extract_file_text(file):
    name = file.name.lower()

    if name.endswith(".pdf"):
        return extract_text_from_pdf(file)

    elif name.endswith(".docx"):
        return extract_text_from_docx(file)

    else:
        try:
            return file.read().decode("utf-8", errors="ignore")
        except:
            return ""
        
def chunk_text(text, chunk_size=1000, overlap=200):
    sentences = re.split(r'(?<=[.!?]) +', text)
    
    chunks = []
    current = ""

    for sentence in sentences:
        if len(current) + len(sentence) <= chunk_size:
            current += " " + sentence
        else:
            chunks.append(current.strip())
            current = sentence

    if current:
        chunks.append(current.strip())

    return chunks