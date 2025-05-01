import base64
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import tempfile
import fitz  # PyMuPDF
import io
import os
import google.generativeai as genai  # Gemini API
from dotenv import load_dotenv
import re
from prometheus_fastapi_instrumentator import Instrumentator



load_dotenv()

app = FastAPI()
instrumentator = Instrumentator().instrument(app).expose(app)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Gemini config
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash-002')

@app.post("/process-pdf/")
async def process_pdf(
    pdf_file: UploadFile = File(...),
    question: str = Form(...),
    mask_sensitive_data: bool = Form(...),
):
    # Step 1: Save uploaded PDF
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(await pdf_file.read())
        tmp_path = tmp.name

    # Step 2: Read text from PDF
    doc = fitz.open(tmp_path)
    full_text = ""
    page_texts = []
    for page in doc:
        text = page.get_text()
        page_texts.append(text)
        full_text += text + "\n"

    # Step 3: Ask Gemini the answer to the question
    gemini_prompt = f"""You are given a document's content.
Question: {question}
Provide a short answer. Also extract the *exact section* (few lines) from the document that best answers it. This section should start with Relevant text:" and be followed by the text."""
    response = model.generate_content([gemini_prompt, full_text])
    answer = response.text
    # print(f"Gemini response: {answer}")

    # Step 4: Find the section to highlight
    answer_lines = answer.splitlines()
    extracted_section = ""
    for line in answer_lines:
        if "Relevant text:" in line:
            extracted_section = line.split("Relevant text:", 1)[1].strip()
            break

    # Step 5: Highlight the extracted section in the PDF
    if extracted_section:
        # Clean extracted_section text
        extracted_section = extracted_section.strip('"')  # Remove surrounding quotes if any
        extracted_section = extracted_section.replace("\n", " ").strip()  # Make it one line
        sentences = re.split(r'(?<=[.!?]) +', extracted_section)  # Split into sentences

        for page in doc:
            for sentence in sentences:
                sentence = sentence.strip()
                if not sentence:
                    continue
                areas = page.search_for(sentence)
                for area in areas:
                    highlight = page.add_highlight_annot(area)
                    highlight.update()

    # Step 6: Mask sensitive data using Gemini
    if mask_sensitive_data:
        # New Gemini prompt for sensitive data identification
        gemini_sensitive_data_prompt = f"""You are given a document's content.
Please identify and list any sensitive data, such as email addresses, phone numbers, or personal identifiers. Provide the sensitive data in the following format:
Sensitive Data:
- [Sensitive Data 1]
- [Sensitive Data 2]
- [Sensitive Data 3]"""

        sensitive_data_response = model.generate_content([gemini_sensitive_data_prompt, full_text])
        # print(f"Sensitive Data Identified: {sensitive_data_response.text}")

        # Parse the identified sensitive data
        sensitive_data = sensitive_data_response.text.strip().split('\n')[1:]  # Skip the first "Sensitive Data" line

        # Apply redactions to the PDF based on the sensitive data identified by Gemini
        for page in doc:
            for sensitive in sensitive_data:
                sensitive = sensitive.strip("- ").strip()  # Clean up the sensitive data
                if sensitive:  # Only process non-empty sensitive data
                    areas = page.search_for(sensitive)
                    for area in areas:
                        page.add_redact_annot(area, fill=(0, 0, 0))
            page.apply_redactions()

    # Step 7: Save processed PDF to memory
    output_pdf_stream = io.BytesIO()
    doc.save(output_pdf_stream)
    output_pdf_stream.seek(0)

    # Step 8: Cleanup
    doc.close()
    os.remove(tmp_path)

    # Step 9: Convert PDF to base64
    pdf_base64 = base64.b64encode(output_pdf_stream.getvalue()).decode('utf-8')

    # Step 10: Prepare the JSON response with the base64 PDF and the answer
    answer = answer.split("Relevant text:")[0].strip()
    response_data = {
        "answer": answer,
        "processed_pdf": pdf_base64
    }

    # Step 11: Return the JSON response
    return JSONResponse(content=response_data)
