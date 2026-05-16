from fastapi import FastAPI, UploadFile, File
from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import shutil
import PyPDF2
import os

# ---------------- DATABASE SETUP ---------------- #

DATABASE_URL = "sqlite:///./users.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

SessionLocal = sessionmaker(bind=engine)

Base = declarative_base()

# ---------------- MODEL ---------------- #

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    email = Column(String, unique=True)
    skills = Column(String)  # store as comma-separated string
    resume_path = Column(String)

# Create tables
Base.metadata.create_all(bind=engine)

# ---------------- FASTAPI APP ---------------- #

app = FastAPI()

# Folder to store resumes
UPLOAD_FOLDER = "resumes"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ---------------- PDF TEXT EXTRACTION FUNCTION ---------------- #

def extract_text_from_pdf(file_path):
    text = ""
    with open(file_path, "rb") as file:
        reader = PyPDF2.PdfReader(file)
        for page in reader.pages:
            text += page.extract_text()
    return text


# ---------------- API ENDPOINT ---------------- #

@app.post("/upload-resume")
async def upload_resume(
    name: str,
    email: str,
    skills: str,
    file: UploadFile = File(...)
):
    # Save file
    file_path = f"{UPLOAD_FOLDER}/{file.filename}"

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # Extract text
    extracted_text = extract_text_from_pdf(file_path)

    # Save to database
    db = SessionLocal()

    new_user = User(
        name=name,
        email=email,
        skills=skills,
        resume_path=file_path
    )

    db.add(new_user)
    db.commit()
    db.close()

    return {
        "message": "Resume uploaded successfully",
        "extracted_text": extracted_text[:500]  # preview only
    }