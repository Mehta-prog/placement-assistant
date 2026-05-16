import streamlit as st
import requests

BASE_URL = "http://127.0.0.1:8000"

st.title("🚀 AI Placement Assistant")

# ---------------- LOGIN ----------------
st.header("🔐 Login")

username = st.text_input("Username")
password = st.text_input("Password", type="password")

if st.button("Login"):
    response = requests.post(f"{BASE_URL}/login", data={
        "username": username,
        "password": password
    })

    if response.status_code == 200:
        token = response.json()["access_token"]
        st.session_state["token"] = token
        st.success("Login Successful ✅")
    else:
        st.error("Login Failed ❌")

# ---------------- UPLOAD RESUME ----------------
st.header("📄 Upload Resume")

uploaded_file = st.file_uploader("Upload PDF", type=["pdf"])

if st.button("Upload Resume"):
    if "token" not in st.session_state:
        st.warning("Please login first")
    elif uploaded_file is not None:
        headers = {
            "Authorization": f"Bearer {st.session_state['token']}"
        }

        files = {
            "file": uploaded_file
        }

        response = requests.post(
            f"{BASE_URL}/upload-resume",
            headers=headers,
            files=files
        )

        if response.status_code == 200:
            st.success("Resume Uploaded ✅")
        else:
            st.error("Upload Failed ❌")

# ---------------- ANALYZE RESUME ----------------
st.header("📊 Analyze Resume (ATS Score)")

if st.button("Analyze Resume"):
    if "token" not in st.session_state:
        st.warning("Please login first")
    else:
        headers = {
            "Authorization": f"Bearer {st.session_state['token']}"
        }

        response = requests.get(
            f"{BASE_URL}/analyze-resume",
            headers=headers
        )

        if response.status_code == 200:
            data = response.json()

            st.subheader(f"ATS Score: {data.get('ats_score', 'N/A')}")

            st.write("### Strengths")
            st.write(data.get("strengths", ""))

            st.write("### Weaknesses")
            st.write(data.get("weaknesses", ""))

            st.write("### Missing Keywords")
            st.write(data.get("missing_keywords", ""))

            st.write("### Suggestions")
            st.write(data.get("suggestions", ""))

        else:
            st.error("Analysis Failed ❌")

# ---------------- JOB MATCH ----------------
st.header("💼 Job Matching")

if st.button("Find Jobs"):
    if "token" not in st.session_state:
        st.warning("Please login first")
    else:
        headers = {
            "Authorization": f"Bearer {st.session_state['token']}"
        }

        response = requests.get(
            f"{BASE_URL}/match-job",
            headers=headers
        )

        if response.status_code == 200:
            st.write(response.json().get("job_matches", ""))
        else:
            st.error("Job Matching Failed ❌")