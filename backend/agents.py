from groq import Groq
import os

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# Resume Critic
def resume_critic(resume_text):
    response = client.chat.completions.create(
        model="llama3-70b-8192",
        messages=[
            {"role": "system", "content": "You are an expert resume reviewer."},
            {"role": "user", "content": f"Analyze this resume and suggest improvements:\n{resume_text}"}
        ]
    )
    return response.choices[0].message.content


# Job Hunter
def job_hunter(skills):
    response = client.chat.completions.create(
        model="llama3-70b-8192",
        messages=[
            {"role": "system", "content": "You are a job search assistant."},
            {"role": "user", "content": f"Suggest jobs and companies for these skills:\n{skills}"}
        ]
    )
    return response.choices[0].message.content