import re
import os
import json
import shutil
import hashlib
import urllib.parse
from datetime import datetime, timedelta

import requests
from models import Base, User, SavedJob
from passlib.context import CryptContext
from groq import Groq
from PyPDF2 import PdfReader
from dotenv import load_dotenv
load_dotenv()

from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    File,
    UploadFile,
    Form,
    Header,
    BackgroundTasks,
    Request,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy.orm import Session

from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

from agents import resume_critic, job_hunter
from database import engine, get_db
from email_service import send_login_alert_email



app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------
# Config
# -------------------------

SECRET_KEY = os.getenv("SECRET_KEY", "")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is not set in .env")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is not set in .env")

client = Groq(api_key=GROQ_API_KEY)

ATS_CACHE = {}
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/google")

Base.metadata.create_all(bind=engine)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# -------------------------
# Request Models
# -------------------------

class ResumeRequest(BaseModel):
    resume: str


class SkillRequest(BaseModel):
    skills: str


class JobSaveRequest(BaseModel):
    title: str
    company: str
    location: str
    url: str


class TemplateUpdateRequest(BaseModel):
    template_name: str


class LocationPreferenceRequest(BaseModel):
    location_name: str
    lat: str
    lng: str




class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str


class GoogleLoginRequest(BaseModel):
    credential: str


class SetPasswordRequest(BaseModel):
    email: str
    password: str


# -------------------------
# Utility Functions
# ------------------------
    

def normalize_password(password: str) -> str:
    
    # bcrypt supports max 72 bytes

    # truncate safely before hashing/verifying

    return password[:72]

def hash_password(password: str) -> str:

    return pwd_context.hash(normalize_password(password))

def verify_password(plain_password: str, hashed_password: str) -> bool:

    return pwd_context.verify(normalize_password(plain_password), hashed_password)


def extract_text_from_pdf(file_path: str) -> str:
    reader = PdfReader(file_path)
    text = ""

    for page in reader.pages:
        page_text = page.extract_text()
        if page_text:
            text += page_text + "\n"

    return text.strip()


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    print("🔐 TOKEN RECEIVED:", token)

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        print("✅ DECODED PAYLOAD:", payload)

        username = payload.get("sub")

        if not username:
            print("❌ No 'sub' found in token")
            raise HTTPException(status_code=401, detail="Invalid token")

        return {"sub": username}

    except JWTError as e:
        print("🚨 JWT ERROR:", str(e))
        raise HTTPException(status_code=401, detail="Invalid token")


def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    return decode_token(token)


def get_user_email(current_user: dict) -> str:
    email = current_user.get("sub")
    if not email:
        raise HTTPException(status_code=401, detail="Invalid user")
    return email


def get_safe_email_prefix(email: str) -> str:
    return email.replace("@", "_at_").replace(".", "_")


def has_github_link(resume_text: str) -> bool:
    text = resume_text.lower()
    github_patterns = [
        "github.com/",
        "github:",
        "github ",
    ]
    return any(pattern in text for pattern in github_patterns)


def get_latest_user_resume(safe_email: str, folder: str = "uploads") -> str | None:
    if not os.path.exists(folder):
        return None

    matching_files = []
    for file in os.listdir(folder):
        if file.startswith(f"{safe_email}_"):
            full_path = os.path.join(folder, file)
            if os.path.isfile(full_path):
                matching_files.append(full_path)

    if not matching_files:
        return None

    return max(matching_files, key=os.path.getmtime)


def fetch_real_jobs_from_remotive(search_term: str, preferred_location: str = ""):
    encoded_term = urllib.parse.quote(search_term)
    url = f"https://remotive.com/api/remote-jobs?search={encoded_term}"

    response = requests.get(url, timeout=15)
    response.raise_for_status()

    data = response.json()
    jobs = data.get("jobs", [])

    cleaned_jobs = []
    preferred_location_lower = preferred_location.lower().strip()

    for job in jobs:
        job_location = (job.get("candidate_required_location") or "").lower()

        location_match = True
        if preferred_location_lower:
            if (
                preferred_location_lower in job_location
                or "remote" in job_location
                or "worldwide" in job_location
            ):
                location_match = True
            else:
                location_match = False

        cleaned_jobs.append(
            {
                "title": job.get("title"),
                "company": job.get("company_name"),
                "category": job.get("category"),
                "job_type": job.get("job_type"),
                "location": job.get("candidate_required_location"),
                "url": job.get("url"),
                "source": "Remotive",
                "location_match": location_match,
                "description": (
                    (job.get("description") or "")[:280] + "..."
                    if job.get("description")
                    else ""
                ),
            }
        )

    cleaned_jobs.sort(key=lambda x: x["location_match"], reverse=True)
    return cleaned_jobs[:8]


def build_search_links(search_terms, preferred_location=""):
    links = []
    location_part = f" in {preferred_location}" if preferred_location else ""

    for term in search_terms[:3]:
        full_term = f"{term} {preferred_location}".strip() if preferred_location else term
        encoded = urllib.parse.quote(full_term)

        links.append(
            {
                "source": "LinkedIn",
                "label": f"Search LinkedIn for {term}{location_part}",
                "url": f"https://www.linkedin.com/jobs/search/?keywords={encoded}",
            }
        )

        links.append(
            {
                "source": "Indeed",
                "label": f"Search Indeed for {term}{location_part}",
                "url": f"https://in.indeed.com/jobs?q={encoded}",
            }
        )

        links.append(
            {
                "source": "Internshala",
                "label": f"Search Internshala for {term}{location_part}",
                "url": f"https://internshala.com/internships/keywords-{urllib.parse.quote(term).replace('%20', '-')}/",
            }
        )

    return links




def is_strong_password(password: str) -> bool:

    if len(password) < 8:

        return False

    if len(password) > 72:

        return False

    if not re.search(r"[A-Z]", password):

        return False

    if not re.search(r"[a-z]", password):

        return False

    if not re.search(r"[0-9]", password):

        return False

    if not re.search(r"""[!@#$%^&*(),.?":{}|<>_\-+=/\\[\];'`~]""", password):

        return False

    return True


# -------------------------
# Basic Routes
# -------------------------

@app.get("/")
def home():
    return {"message": "Placement Assistant API Running 🚀"}


@app.post("/resume-review")
def review_resume(data: ResumeRequest):
    result = resume_critic(data.resume)
    return {"analysis": result}


@app.post("/job-search")
def search_jobs(data: SkillRequest):
    result = job_hunter(data.skills)
    return {"jobs": result}


@app.post("/upload-profile")
async def upload_profile(
    skills: str = Form(...),
    resume: UploadFile = File(...),
    authorization: str = Header(None),
):
    with open(f"uploaded_{resume.filename}", "wb") as f:
        f.write(await resume.read())

    return {
        "message": "Profile uploaded successfully",
        "skills": skills,
        "filename": resume.filename,
    }


# -------------------------
# Google Auth
# -------------------------



@app.get("/protected")
def protected(current_user: dict = Depends(get_current_user)):
    email = get_user_email(current_user)
    return {"message": f"Hello {email}, you are authorized!"}


@app.get("/profile")
def get_profile(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    email = get_user_email(current_user)
    user = db.query(User).filter(User.email == email).first()

    if not user:
        return {"error": "User not found"}

    return {"message": f"Welcome {user.name or user.email}"}


# -------------------------
# Resume Upload + Analysis
# -------------------------

@app.post("/upload-resume")
def upload_resume(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    email = get_user_email(current_user)
    safe_email = get_safe_email_prefix(email)

    os.makedirs("uploads", exist_ok=True)
    file_location = f"uploads/{safe_email}_{file.filename}"

    with open(file_location, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    resume_text = extract_text_from_pdf(file_location)

    if not has_github_link(resume_text):
        return {
            "warning": "Resume uploaded, but GitHub ID not found. Job matching & improvement will be disabled."
        }

    return {"message": "Resume uploaded successfully"}


@app.get("/analyze-resume")
def analyze_resume(current_user: dict = Depends(get_current_user)):
    email = get_user_email(current_user)
    safe_email = get_safe_email_prefix(email)

    user_file = get_latest_user_resume(safe_email)
    if not user_file:
        return {"error": "No resume found"}

    resume_text = extract_text_from_pdf(user_file)

    if not has_github_link(resume_text):
        return {"error": "Unable to analyze resume: GitHub ID missing"}

    resume_hash = hashlib.md5(resume_text.encode()).hexdigest()

    if resume_hash in ATS_CACHE:
        return ATS_CACHE[resume_hash]

    prompt = f"""
You are an ATS (Applicant Tracking System).

Analyze the resume STRICTLY using this scoring system:

TOTAL SCORE = 100

Breakdown:
- Skills match: 30 points
- Experience relevance: 25 points
- Projects quality: 15 points
- Keywords presence: 15 points
- Formatting & clarity: 15 points

IMPORTANT:
- Always give SAME score for SAME resume
- Do NOT randomize
- Be consistent

Respond ONLY in JSON format:

{{
  "ats_score": number,
  "strengths": ["..."],
  "weaknesses": ["..."],
  "missing_keywords": ["..."],
  "suggestions": ["..."]
}}

Resume:
{resume_text}
"""

    chat_completion = client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        model="llama-3.1-8b-instant",
        temperature=0,
        top_p=1,
    )

    ai_response = chat_completion.choices[0].message.content.strip()

    if ai_response.startswith("```json"):
        ai_response = ai_response.replace("```json", "", 1).strip()
    if ai_response.startswith("```"):
        ai_response = ai_response.replace("```", "", 1).strip()
    if ai_response.endswith("```"):
        ai_response = ai_response[:-3].strip()

    try:
        result = json.loads(ai_response)
    except Exception as e:
        print("AI RESPONSE PARSE ERROR:", e)
        print("RAW AI RESPONSE:", ai_response)
        result = {"raw": ai_response}

    ATS_CACHE[resume_hash] = result
    return result


# -------------------------
# Job Matching
# -------------------------

@app.get("/match-job")
def match_job(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    email = get_user_email(current_user)
    safe_email = get_safe_email_prefix(email)

    user = db.query(User).filter(User.email == email).first()
    preferred_location = user.preferred_location if user and user.preferred_location else ""

    user_file = get_latest_user_resume(safe_email)
    if not user_file:
        return {"error": "No resume found"}

    resume_text = extract_text_from_pdf(user_file)

    if not resume_text or not resume_text.strip():
        return {"error": "Resume text could not be extracted"}

    if not has_github_link(resume_text):
        return {"error": "Cannot match jobs: GitHub ID missing in resume"}

    prompt = f"""
You are an AI career assistant.

Read this resume and respond ONLY in valid JSON.
Do not add markdown.
Do not add triple backticks.
Do not add explanations outside JSON.

Return exactly in this format:

{{
  "recommended_search_terms": [
    "python developer",
    "fastapi backend intern",
    "react frontend developer"
  ],
  "summary": "Short summary of the best role directions for this candidate."
}}

Rules:
- Return 3 to 5 realistic search terms
- Consider the preferred location if provided
- Keep output stable and consistent

Preferred location: {preferred_location}

Resume:
{resume_text}
"""

    try:
        chat_completion = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant",
            temperature=0,
            top_p=1,
        )
        ai_response = chat_completion.choices[0].message.content.strip()
    except Exception as e:
        print("AI CALL ERROR:", e)
        return {"error": "Failed to generate job search terms"}

    if ai_response.startswith("```json"):
        ai_response = ai_response.replace("```json", "", 1).strip()
    if ai_response.startswith("```"):
        ai_response = ai_response.replace("```", "", 1).strip()
    if ai_response.endswith("```"):
        ai_response = ai_response[:-3].strip()

    try:
        parsed = json.loads(ai_response)
    except Exception as e:
        print("MATCH PARSE ERROR:", e)
        print("RAW MATCH RESPONSE:", ai_response)
        parsed = {
            "recommended_search_terms": [
                "software developer",
                "python developer",
                "backend intern",
            ],
            "summary": "Fallback search terms used.",
        }

    search_terms = parsed.get("recommended_search_terms", ["software developer"])
    if not isinstance(search_terms, list):
        search_terms = ["software developer"]

    cleaned_search_terms = []
    seen_terms = set()

    for term in search_terms:
        if isinstance(term, str):
            term = term.strip()
            if term and term.lower() not in seen_terms:
                cleaned_search_terms.append(term)
                seen_terms.add(term.lower())

    if not cleaned_search_terms:
        cleaned_search_terms = ["software developer"]

    all_jobs = []

    for term in cleaned_search_terms[:5]:
        try:
            jobs = fetch_real_jobs_from_remotive(term, preferred_location)
            for job in jobs:
                job["matched_by"] = term
            all_jobs.extend(jobs)
        except Exception as e:
            print(f"Error fetching jobs for {term}: {e}")

    unique_jobs = []
    seen_urls = set()

    for job in all_jobs:
        job_url = job.get("url")
        if job_url and job_url not in seen_urls:
            seen_urls.add(job_url)
            unique_jobs.append(job)

    unique_jobs.sort(key=lambda x: x.get("location_match", False), reverse=True)
    search_links = build_search_links(cleaned_search_terms, preferred_location)

    return {
        "summary": parsed.get("summary", ""),
        "preferred_location": preferred_location,
        "jobs": unique_jobs[:12],
        "search_links": search_links,
    }


# -------------------------
# Improve Resume
# -------------------------

@app.get("/improve-resume")
def improve_resume(current_user: dict = Depends(get_current_user)):
    email = get_user_email(current_user)
    safe_email = get_safe_email_prefix(email)

    user_file = get_latest_user_resume(safe_email)
    if not user_file:
        return {"error": "No resume found"}

    resume_text = extract_text_from_pdf(user_file)

    if not has_github_link(resume_text):
        return {"error": "Cannot improve resume: GitHub ID missing"}

    prompt = f"""
You are an expert resume writer.

Rewrite and improve the following resume into a professional ATS-friendly format.

Respond ONLY in valid JSON.
Do not add markdown.
Do not add triple backticks.
Do not add any explanation outside JSON.

Return exactly in this format:

{{
  "summary": ["point 1", "point 2"],
  "skills": ["skill 1", "skill 2", "skill 3"],
  "experience": ["point 1", "point 2"],
  "projects": ["project 1", "project 2"],
  "education": ["education detail 1", "education detail 2"],
  "certifications": ["cert 1", "cert 2"]
}}

Rules:
- Keep content realistic
- Do not invent fake achievements
- Improve wording and clarity
- Use strong action verbs
- Keep each item concise and resume-ready

Resume:
{resume_text}
"""

    chat_completion = client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        model="llama-3.1-8b-instant",
        temperature=0,
        top_p=1,
    )

    ai_response = chat_completion.choices[0].message.content.strip()

    if ai_response.startswith("```json"):
        ai_response = ai_response.replace("```json", "", 1).strip()
    if ai_response.startswith("```"):
        ai_response = ai_response.replace("```", "", 1).strip()
    if ai_response.endswith("```"):
        ai_response = ai_response[:-3].strip()

    try:
        improved_data = json.loads(ai_response)
    except Exception as e:
        print("IMPROVE RESUME PARSE ERROR:", e)
        print("RAW IMPROVE RESPONSE:", ai_response)
        improved_data = {
            "summary": [],
            "skills": [],
            "experience": [],
            "projects": [],
            "education": [],
            "certifications": [],
        }

    return improved_data


# -------------------------
# Saved Jobs
# -------------------------

@app.post("/save-job")
def save_job(
    job: JobSaveRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    email = get_user_email(current_user)

    existing_job = db.query(SavedJob).filter(
        SavedJob.url == job.url,
        SavedJob.username == email,
    ).first()

    if existing_job:
        return {"message": "Job already saved"}

    new_job = SavedJob(
        username=email,
        title=job.title,
        company=job.company,
        location=job.location,
        url=job.url,
        status="saved",
    )

    db.add(new_job)
    db.commit()
    db.refresh(new_job)

    return {"message": "Job saved successfully"}


@app.get("/saved-jobs")
def get_saved_jobs(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    email = get_user_email(current_user)

    jobs = db.query(SavedJob).filter(SavedJob.username == email).all()

    return {
        "jobs": [
            {
                "id": job.id,
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "url": job.url,
                "status": job.status,
            }
            for job in jobs
        ]
    }


@app.put("/mark-applied/{job_id}")
def mark_applied(
    job_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    email = get_user_email(current_user)

    job = db.query(SavedJob).filter(
        SavedJob.id == job_id,
        SavedJob.username == email,
    ).first()

    if not job:
        return {"error": "Job not found"}

    job.status = "applied"
    db.commit()

    return {"message": "Job marked as applied"}


@app.put("/update-job-status/{job_id}")
def update_job_status(
    job_id: int,
    status: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    email = get_user_email(current_user)

    job = db.query(SavedJob).filter(
        SavedJob.id == job_id,
        SavedJob.username == email,
    ).first()

    if not job:
        return {"error": "Job not found"}

    allowed_status = ["saved", "applied", "interview", "rejected"]
    if status not in allowed_status:
        return {"error": "Invalid status"}

    job.status = status
    db.commit()

    return {"message": "Status updated successfully"}


# -------------------------
# Template Preference
# -------------------------

@app.get("/get-template")
def get_template(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    email = get_user_email(current_user)

    user = db.query(User).filter(User.email == email).first()
    if not user:
        return {"error": "User not found"}

    return {"selected_template": user.selected_template}


@app.put("/set-template")
def set_template(
    data: TemplateUpdateRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    email = get_user_email(current_user)

    user = db.query(User).filter(User.email == email).first()
    if not user:
        return {"error": "User not found"}

    allowed_templates = ["classic_ats", "modern_ats", "fresher"]
    if data.template_name not in allowed_templates:
        return {"error": "Invalid template"}

    user.selected_template = data.template_name
    db.commit()
    db.refresh(user)

    return {
        "message": "Template updated successfully",
        "selected_template": user.selected_template,
    }


# -------------------------
# Location Preference
# -------------------------

@app.put("/set-location-preference")
def set_location_preference(
    data: LocationPreferenceRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    email = get_user_email(current_user)

    user = db.query(User).filter(User.email == email).first()

    if not user:
        return {"error": "User not found"}

    user.preferred_location = data.location_name
    user.preferred_lat = data.lat
    user.preferred_lng = data.lng

    db.commit()
    db.refresh(user)

    return {
        "message": "Location preference updated successfully",
        "preferred_location": user.preferred_location,
        "preferred_lat": user.preferred_lat,
        "preferred_lng": user.preferred_lng,
    }


@app.get("/get-location-preference")
def get_location_preference(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    email = get_user_email(current_user)

    user = db.query(User).filter(User.email == email).first()
    if not user:
        return {"error": "User not found"}

    return {
        "preferred_location": user.preferred_location or "",
        "preferred_lat": user.preferred_lat or "20.5937",
        "preferred_lng": user.preferred_lng or "78.9629",
    }


# -------------------------
# Debug
# -------------------------

@app.get("/debug-users")
def debug_users(db: Session = Depends(get_db)):
    users = db.query(User).all()
    return {
        "users": [
            {
                "email": u.email,
                "name": u.name,
                "google_sub": u.google_sub,
            }
            for u in users
        ]
    }

@app.post("/register")
def register(data: RegisterRequest, db: Session = Depends(get_db)):
    email = data.email.strip().lower()
    name = data.name.strip() if data.name else ""

    existing_user = db.query(User).filter(User.email == email).first()
    if existing_user:
        return {"error": "User already exists"}

    if not is_strong_password(data.password):
        return {
    "error": "Password must be 8 to 72 characters long and include 1 uppercase letter, 1 lowercase letter, 1 number, and 1 special character"
}

    new_user = User(
        email=email,
        name=name,
        password=hash_password(data.password),
        google_sub=None
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return {"message": "User registered successfully"}


@app.post("/auth/google")
def auth_google(
    data: GoogleLoginRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    print("GOOGLE_CLIENT_ID FROM ENV:", GOOGLE_CLIENT_ID)

    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_ID is not configured")

    if not data.credential:
        raise HTTPException(status_code=400, detail="Google credential is missing")

    try:
        idinfo = id_token.verify_oauth2_token(
            data.credential,
            google_requests.Request(),
            GOOGLE_CLIENT_ID
        )

        print("GOOGLE IDINFO:", idinfo)

        google_sub = idinfo.get("sub")
        email = idinfo.get("email")
        email_verified = idinfo.get("email_verified", False)
        name = idinfo.get("name", "")

        if not google_sub or not email or not email_verified:
            raise HTTPException(status_code=400, detail="Invalid Google account")

        user = db.query(User).filter(User.email == email).first()

        if not user:
            user = User(
                email=email,
                name=name,
                google_sub=google_sub,
                password=""
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        else:
            user.name = name
            user.google_sub = google_sub
            db.commit()
            db.refresh(user)

        access_token = create_access_token({"sub": user.email})

        client_ip = request.client.host if request.client else "Unknown"

        background_tasks.add_task(
            send_login_alert_email,
            to_email=user.email,
            user_name=user.name or "User",
            login_ip=client_ip,
        )

        return {
            "access_token": access_token,
            "token_type": "bearer",
            "email": user.email,
            "name": user.name,
            "needs_password_setup": user.password == ""
        }

    except ValueError as e:
        print("GOOGLE VERIFY ERROR:", str(e))
        raise HTTPException(status_code=401, detail=f"Invalid Google token: {str(e)}")


@app.post("/login")
def login(
    data: LoginRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    email = data.email.strip().lower()

    user = db.query(User).filter(User.email == email).first()

    if not user:
        raise HTTPException(status_code=400, detail="User not found")

    if not user.password:
        raise HTTPException(
            status_code=400,
            detail="This account does not have a password yet. Please complete Google registration or reset password."
        )

    if not verify_password(data.password, user.password):
        raise HTTPException(status_code=400, detail="Invalid password")

    access_token = create_access_token({"sub": user.email})

    client_ip = request.client.host if request.client else "Unknown"

    background_tasks.add_task(
        send_login_alert_email,
        to_email=user.email,
        user_name=user.name or "User",
        login_ip=client_ip,
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "email": user.email,
        "name": user.name,
    }

@app.post("/set-password")
def set_password(data: SetPasswordRequest, db: Session = Depends(get_db)):
    email = data.email.strip().lower()

    user = db.query(User).filter(User.email == email).first()

    if not user:
        return {"error": "User not found"}

    if not is_strong_password(data.password):
        return {
    "error": "Password must be 8 to 72 characters long and include 1 uppercase letter, 1 lowercase letter, 1 number, and 1 special character"
}

    user.password = hash_password(data.password)
    db.commit()
    db.refresh(user)

    return {"message": "Password set successfully"}

@app.delete("/delete-account")
def delete_account(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    email = get_user_email(current_user)

    user = db.query(User).filter(User.email == email).first()

    if not user:
        return {"error": "User not found"}

    # delete saved jobs first (important)
    db.query(SavedJob).filter(SavedJob.username == email).delete()

    # delete user
    db.delete(user)
    db.commit()

    return {"message": "Account deleted successfully"},