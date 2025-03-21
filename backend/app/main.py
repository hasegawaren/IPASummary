from bs4 import BeautifulSoup
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List, Dict
import io
import re
import httpx
import os
import logging
import tiktoken
import pdfplumber
import fitz  
from PyPDF2 import PdfReader
from dotenv import load_dotenv
import uuid
import asyncio
from fastapi import Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

if not DEEPSEEK_API_KEY:
    logging.error("❌ DeepSeek API Key is missing! Please set it in the .env file.")
    raise ValueError("DeepSeek API Key is missing! Please set it in the .env file.")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

sessions: Dict[str, Dict] = {}
MAX_TOKENS = 4000
MAX_PDF_SIZE_MB = 10

def extract_pdf_toc(content: bytes) -> List[Dict[str, str]]:
    pdf_document = fitz.open(stream=content, filetype="pdf")
    toc = pdf_document.get_toc()
    pdf_document.close()
    
    return [{"title": entry[1], "page": entry[2]} for entry in toc] if toc else []

async def extract_text_from_pdf(content: bytes) -> str:
    pdf_reader = PdfReader(io.BytesIO(content))
    extracted_texts = []

    for page in pdf_reader.pages:
        page_text = page.extract_text()
        if not page_text:
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                try:
                    page_text = pdf.pages[page.page_number].extract_text()
                except IndexError:
                    page_text = ""
        extracted_texts.append(page_text or "")

    extracted_text = re.sub(r"\s+", " ", " ".join(extracted_texts).strip())
    return extracted_text

async def get_deepseek_response_batch(chunks: List[str]) -> List[str]:
    tasks = [
        get_deepseek_response([{"role": "user", "content": f"โปรดสรุปข้อมูลนี้:\n\n{chunk}"}]) 
        for chunk in chunks
    ]
    return await asyncio.gather(*tasks)

async def get_deepseek_response(messages: List[Dict[str, str]]) -> str:
    """เรียก API ของ DeepSeek แบบ async พร้อมเพิ่ม timeout และ retry"""
    deepseek_api_url = "https://api.deepseek.com/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": "deepseek-chat", "messages": messages}

    retries = 3
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(deepseek_api_url, json=payload, headers=headers)
                response.raise_for_status()
                response_data = response.json()
                return response_data["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            if attempt < retries - 1:
                await asyncio.sleep(3)
            else:
                raise HTTPException(status_code=500, detail=f"DeepSeek API error: {e.response.text}")
async def fetch_wikipedia_content(wiki_url: str) -> Dict[str, str]:
    """ 🔹 ดึงข้อมูลจาก Wikipedia พร้อมหัวข้อย่อย (TOC) """
    try:
        logging.info(f"🌍 Fetching Wikipedia URL: {wiki_url}")

        async with httpx.AsyncClient() as client:
            response = await client.get(wiki_url)
            response.raise_for_status()
            html_content = response.text

        soup = BeautifulSoup(html_content, "html.parser")

        paragraphs = [p.get_text().strip() for p in soup.select("div.mw-parser-output > p") if p.get_text().strip()]
        raw_text = " ".join(paragraphs[:3])

        summary = await get_deepseek_response([
            {"role": "user", "content": f"โปรดสรุปข้อมูลนี้ให้อ่านง่ายโดยใช้ Markdown:\n\n{raw_text}"}
        ])

        formatted_summary = summary.replace("**-", "\n\n**- ").replace("- ", "\n- ")

        # ✅ Debug หัวข้อที่ดึงมาได้
        toc_list = []
        exclude_list = ["สารบัญ", "หมายเหตุ", "ดูเพิ่ม", "อ้างอิง", "แหล่งข้อมูลอื่น"]
        for heading in soup.select("h2, h3"):
            heading_text = heading.get_text(strip=True).replace("[แก้ไข]", "").replace("[edit]", "")
            if heading_text and heading_text not in exclude_list:
                toc_list.append(heading_text)

        logging.info(f"📌 Extracted TOC: {toc_list}")  # ✅ ดูว่ามีหัวข้อจริงไหม

        return {
            "summary": formatted_summary if formatted_summary else "ไม่สามารถดึงข้อมูลจาก Wikipedia ได้",
            "toc": toc_list,
            "html": html_content
        }

    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch Wikipedia content: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")


@app.post("/api/summarize")
async def summarize(
    input_type: str = Form(...),
    user_text: Optional[str] = Form(None),
    pdf_file: Optional[UploadFile] = File(None),
    wiki_url: Optional[str] = Form(None),
    session_id: Optional[str] = Form(None)
):
    logging.info(f"📩 Received Request - input_type: {input_type}, session_id: {session_id}")

    if not session_id:
        session_id = str(uuid.uuid4())

    if session_id not in sessions:
        sessions[session_id] = {"type": input_type, "data": None, "toc": []}

    summary_text = ""
    toc = []

    if input_type == "text":
        logging.info("📝 Processing Text Input")
        if not user_text:
            raise HTTPException(status_code=400, detail="No text provided.")
        summary_text = await get_deepseek_response([{"role": "user", "content": f"โปรดสรุปข้อความนี้:\n\n{user_text}"}])
        sessions[session_id]["data"] = user_text

    elif input_type == "pdf":
        logging.info("📄 Processing PDF File")
        if not pdf_file:
            raise HTTPException(status_code=400, detail="No PDF file uploaded.")
        content = await pdf_file.read()
        extracted_text = await extract_text_from_pdf(content)
        summary_text = await get_deepseek_response([
            {"role": "user", "content": f"โปรดสรุปเนื้อหานี้:\n\n{extracted_text[:2000]}"}  # จำกัดข้อความ
        ])
        sessions[session_id]["data"] = extracted_text

    elif input_type == "wiki":
        logging.info(f"🌍 Fetching Wikipedia Summary from: {wiki_url}")
        if not wiki_url:
            raise HTTPException(status_code=400, detail="No wiki_url provided.")
        wiki_data = await fetch_wikipedia_content(wiki_url)
        summary_text = wiki_data["summary"]
        toc = wiki_data["toc"]
        sessions[session_id]["data"] = wiki_data["html"]

    sessions[session_id]["toc"] = toc

    return {"session_id": session_id, "summary": summary_text, "toc": toc}

from fastapi import Request

@app.post("/api/chat")
async def chat(request: Request):
    data = await request.json()  # ✅ อ่าน request JSON
    
    session_id = data.get("session_id")
    question = data.get("question")

    if not session_id or not question:
        logging.error("❌ Missing session_id or question!")
        raise HTTPException(status_code=400, detail="session_id and question are required")

    logging.info(f"📩 Received /api/chat request - session_id: {session_id}, question: {question}")

    # ✅ ตรวจสอบว่า session มีอยู่หรือไม่
    if session_id not in sessions:
        logging.error(f"❌ Session {session_id} not found!")
        raise HTTPException(status_code=400, detail="Session not found.")

    session = sessions[session_id]
    input_type = session.get("type")  # 🟢 ตรวจสอบประเภทข้อมูล (text, pdf, wiki)
    data = session.get("data")  # 🟢 ดึงเนื้อหาที่เคยสรุปไปแล้ว

    if not input_type or not data:
        logging.error(f"❌ No content found for session {session_id}!")
        raise HTTPException(status_code=400, detail="No previous content to reference.")

    response_text = ""

    # ✅ ถามต่อเกี่ยวกับข้อความธรรมดา
    if input_type == "text":
        response_text = await get_deepseek_response([
            {"role": "user", "content": f"เกี่ยวกับข้อความที่สรุปไปก่อนหน้านี้:\n\n{data}\n\nคำถาม: {question}"}
        ])

    # ✅ ถามต่อเกี่ยวกับ PDF
    elif input_type == "pdf":
        response_text = await get_deepseek_response([
            {"role": "user", "content": f"เกี่ยวกับ PDF ที่สรุปไป:\n\n{data[:1000]}...\n\nคำถาม: {question}"}
        ])

    # ✅ ถามต่อเกี่ยวกับ Wikipedia
    elif input_type == "wiki":
        html_content = data  # 🟢 ใช้ HTML ที่ดึงมาจาก Wikipedia
        soup = BeautifulSoup(html_content, "html.parser")
        content_div = soup.find("div", {"class": "mw-parser-output"})

        if not content_div:
            logging.error("❌ Wikipedia content not found in session!")
            raise HTTPException(status_code=400, detail="Unable to find Wikipedia content.")

        topic_text = ""
        for heading in content_div.find_all(["h2", "h3"]):
            heading_text = heading.get_text(strip=True).replace("[แก้ไข]", "").replace("[edit]", "")
            if heading_text == question:
                topic_text = " ".join(p.get_text(strip=True) for p in heading.find_next_siblings() if p.name == "p")
                break

        if not topic_text:
            logging.warning(f"⚠️ ไม่พบเนื้อหาสำหรับ '{question}' ใน Wikipedia!")
            raise HTTPException(status_code=400, detail=f"ไม่พบเนื้อหาสำหรับ '{question}'")

        response_text = await get_deepseek_response([
            {"role": "user", "content": f"เกี่ยวกับหัวข้อ '{question}':\n\n{topic_text}"}
        ])

    return {"answer": response_text}



@app.get("/api/get_session/{session_id}")
async def get_session(session_id: str):
    """ ดึงข้อมูล session และ wiki_url """
    logging.info(f"📌 Checking session_id: {session_id}")
    logging.info(f"📌 All sessions: {sessions}")

    session = sessions.get(session_id)
    if not session:
        logging.warning(f"⚠️ Session {session_id} not found!")
        raise HTTPException(status_code=404, detail="Session not found")

    wiki_url = session.get("wiki_url", None)
    logging.info(f"📌 Found session - wiki_url: {wiki_url}")

    return {
        "session_id": session_id,
        "wiki_url": wiki_url,
        "summary": session.get("summary", None),
        "toc": session.get("toc", [])
    }
