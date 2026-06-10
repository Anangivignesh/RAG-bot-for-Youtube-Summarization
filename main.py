from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import requests
import os
import shutil
from dotenv import load_dotenv
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import Chroma
from langchain_community.embeddings import HuggingFaceEmbeddings

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
DB_PATH = "./db"

video_metadata = {}

def extract_video_id(url: str) -> str:
    import re
    patterns = [
        r"(?:v=|\/)([0-9A-Za-z_-]{11})",
        r"youtu\.be\/([0-9A-Za-z_-]{11})",
        r"shorts\/([0-9A-Za-z_-]{11})"
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

class VideoRequest(BaseModel):
    url: str

class ChatRequest(BaseModel):
    question: str

@app.get("/")
def root():
    import os
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(base_dir, "index.html"))

@app.post("/load")
async def load_video(req: VideoRequest):
    video_id = extract_video_id(req.url)
    if not video_id:
        return {"error": "Invalid YouTube URL. Could not extract video ID."}

    try:
        transcript_list = YouTubeTranscriptApi().fetch(video_id)
    except TranscriptsDisabled:
        return {"error": "This video has transcripts disabled."}
    except NoTranscriptFound:
        return {"error": "No transcript found for this video. Try a different video."}
    except Exception as e:
        return {"error": f"Could not fetch transcript: {str(e)}"}

    full_text = " ".join([entry.text for entry in transcript_list])
    total_words = len(full_text.split())

    if os.path.exists(DB_PATH):
        shutil.rmtree(DB_PATH)

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=60
    )
    chunks = splitter.split_text(full_text)

    vectordb = Chroma.from_texts(
        chunks,
        embeddings,
        persist_directory=DB_PATH
    )
    vectordb.persist()

    video_metadata["video_id"] = video_id
    video_metadata["url"] = req.url
    video_metadata["chunks"] = len(chunks)
    video_metadata["words"] = total_words

    return {
        "video_id": video_id,
        "chunks_stored": len(chunks),
        "total_words": total_words,
        "message": "Video transcript loaded. You can now ask questions."
    }

@app.post("/chat")
async def chat(req: ChatRequest):
    if not os.path.exists(DB_PATH):
        return {"error": "No video loaded yet. Submit a YouTube URL first."}

    vectordb = Chroma(
        persist_directory=DB_PATH,
        embedding_function=embeddings
    )

    docs = vectordb.similarity_search(req.question, k=4)
    if not docs:
        return {"answer": "Could not find relevant content in the video transcript."}

    context = "\n\n".join([d.page_content for d in docs])

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "openrouter/owl-alpha",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a helpful assistant answering questions about a YouTube video. "
                        "Use only the transcript excerpts below to answer. "
                        "If the answer is not in the transcript, say so clearly.\n\n"
                        f"Transcript excerpts:\n{context}"
                    )
                },
                {
                    "role": "user",
                    "content": req.question
                }
            ],
            "max_tokens": 600
        },
        timeout=30
    )

    if response.status_code != 200:
        return {"error": f"LLM API error: {response.status_code} — {response.text[:200]}"}

    return {
        "answer": response.json()["choices"][0]["message"]["content"],
        "chunks_used": len(docs)
    }

@app.get("/status")
def status():
    if not video_metadata:
        return {"loaded": False}
    return {"loaded": True, **video_metadata}

@app.delete("/reset")
def reset():
    if os.path.exists(DB_PATH):
        shutil.rmtree(DB_PATH)
    video_metadata.clear()
    return {"message": "Cleared. Load a new video."}
