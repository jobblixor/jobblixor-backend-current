import os
import json
import time
import requests
import datetime
import bcrypt
from dotenv import dotenv_values
from pathlib import Path
from playwright.sync_api import sync_playwright
from flask import Flask, request, jsonify
from flask_cors import CORS

import firebase_admin
from firebase_admin import credentials, firestore

UPLOAD_DIR = "uploads"
COOKIE_DIR = "cookies"
SCREENSHOT_DIR = "debug/screenshots"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(COOKIE_DIR, exist_ok=True)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

config = dotenv_values("config.env")
DEFAULT_EMAIL = config.get("DEFAULT_EMAIL", "me@jobblixor.local")

# Firebase setup
cred = credentials.Certificate("firebase_credentials.json")
firebase_admin.initialize_app(cred)
db = firestore.client()
app = Flask(__name__)
CORS(app)

def get_user_inputs():
    job_title = input("Job Title (e.g. Cashier): ")
    location = input("Location (e.g. New York, NY): ")
    first_name = input("First Name: ")
    last_name = input("Last Name: ")
    phone = input("Phone Number: ")
    email = input(f"Email [{DEFAULT_EMAIL}]: ") or DEFAULT_EMAIL

    if "@" not in email or "." not in email:
        print("❌ Please enter a valid email address.")
        exit()

    password = input("Enter your password: ")
    doc_ref = db.collection("users").document(email)
    doc = doc_ref.get()

    if doc.exists:
        stored_hash = doc.to_dict().get("password_hash")
        if not stored_hash or not bcrypt.checkpw(password.encode(), stored_hash.encode()):
            print("❌ Incorrect password. Please try again.")
            exit()
    else:
        confirm = input("Confirm password: ")
        if password != confirm:
            print("❌ Passwords do not match.")
            exit()
        hashed_pw = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    resume_path = input("Path to Resume File (e.g. resume.pdf): ")
    profile_photo = input("(Optional) Path to Profile Photo: ") or None
    preferred_salary = input("Preferred Salary (optional): ")
    num_jobs = int(input("How many jobs would you like to apply to?: "))

    resume_filename = os.path.basename(resume_path)
    saved_resume_path = os.path.join(UPLOAD_DIR, resume_filename)
    with open(resume_path, "rb") as src, open(saved_resume_path, "wb") as dst:
        dst.write(src.read())

    profile_photo_path = None
    if profile_photo:
        profile_filename = os.path.basename(profile_photo)
        profile_photo_path = os.path.join(UPLOAD_DIR, profile_filename)
        with open(profile_photo, "rb") as src, open(profile_photo_path, "wb") as dst:
            dst.write(src.read())

    return {
        "job_title": job_title.strip(),
        "location": location.strip(),
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "phone": phone.strip(),
        "email": email.strip(),
        "password_hash": hashed_pw if not doc.exists else None,
        "resume_path": saved_resume_path,
        "profile_photo": profile_photo_path,
        "preferred_salary": preferred_salary.strip(),
        "num_jobs": num_jobs
    }

def save_user_data(user_input):
    now = datetime.datetime.utcnow().isoformat()
    email = user_input["email"]
    doc_ref = db.collection("users").document(email)
    doc = doc_ref.get()

    updates = {
        "job_title": user_input["job_title"],
        "location": user_input["location"],
        "salary": user_input["preferred_salary"],
        "phone": user_input["phone"],
        "resume": user_input["resume_path"],
        "profile_photo": user_input.get("profile_photo"),
        "updated_at": now
    }

    if doc.exists:
        doc_ref.update(updates)
        print("📬 Updated user preferences in Firestore.")
    else:
        new_user_data = {
            **user_input,
            "resume": user_input["resume_path"],
            "profile_photo": user_input.get("profile_photo"),
            "application_count": 0,
            "free_uses_left": 5,
            "plan_id": "free",
            "subscription_status": "active",
            "created_at": now,
            "updated_at": now,
            "stripe_customer_id": None
        }
        doc_ref.set(new_user_data)
        print("✅ New user created and saved to Firestore!")

    with open("user_data.json", "w") as f:
        json.dump(user_input, f, indent=4)
    print("✅ User data saved to user_data.json")

def fetch_jobs(job_title, location, limit=10):
    serp_api_key = config["SERP_API_KEY"]
    query = f"{job_title} in {location}"
    url = "https://serpapi.com/search"
    params = {
        "engine": "google_jobs",
        "q": query,
        "api_key": serp_api_key,
        "hl": "en"
    }
    try:
        response = requests.get(url, params=params)
        data = response.json()
        jobs = data.get("jobs_results", [])[:limit]

        results = []
        for job in jobs:
            link = job.get("apply_options", [{}])[0].get("link", "N/A")
            results.append({
                "title": job.get("title"),
                "company": job.get("company_name"),
                "link": link
            })
        return results
    except Exception as e:
        print(f"❌ Failed to fetch jobs: {e}")
        return []

def apply_to_job(job, user_data):
    try:
        email = user_data["email"]
        doc_ref = db.collection("users").document(email)
        doc = doc_ref.get()
        if doc.exists:
            counts = doc.to_dict()
            if counts.get("free_uses_left", 0) <= 0:
                print("⛔ You're out of free job applications.\nUpgrade to a plan to keep applying!")
                exit()

        url = job["link"]
        if not url or "http" not in url:
            return "Skipped (invalid link)"

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            page = browser.new_page()
            page.goto(url, timeout=60000)

            domain = page.url
            screenshot_path = os.path.join(SCREENSHOT_DIR, f"{job['title'].replace(' ', '_')}_{job['company'].replace(' ', '_')}.png")
            page.screenshot(path=screenshot_path)

            if "greenhouse.io" in domain:
                page.fill("input[name='first_name']", user_data["first_name"])
                page.fill("input[name='last_name']", user_data["last_name"])
                page.fill("input[type='email']", user_data["email"])
                page.fill("input[type='tel']", user_data["phone"])
                page.set_input_files("input[type='file']", user_data["resume_path"])
                page.click("button[type='submit']")

            browser.close()

            doc_ref.update({
                "application_count": counts.get("application_count", 0) + 1,
                "free_uses_left": max(0, counts.get("free_uses_left", 0) - 1)
            })

            return "Success (screenshot taken)"
    except Exception as e:
        return f"Failed ({e})"

def main():
    print("🎯 Jobblixor is starting up...\n")
    user_data = get_user_inputs()
    save_user_data(user_data)

    print("\n✅ User input collected and saved to user_data.json:\n")
    print(json.dumps(user_data, indent=2))
    print(f"🔐 SERP_API_KEY loaded: {config['SERP_API_KEY']}")

    email = user_data["email"]
    doc_ref = db.collection("users").document(email)
    doc = doc_ref.get()
    if doc.exists:
        free_uses = doc.to_dict().get("free_uses_left", 0)
        print(f"💡 You have {free_uses} free applications left before upgrade.")

    job_results = fetch_jobs(user_data["job_title"], user_data["location"], limit=user_data["num_jobs"])

    print("\n📄 Matching Jobs:")
    for idx, job in enumerate(job_results, 1):
        print(f"{idx}. {job['title']} at {job['company']} – {job['link']}")

    print("\n🤖 Starting auto-apply bot...\n")
    for job in job_results:
        print(f"➡️ Visiting: {job['link']}")
        status = apply_to_job(job, user_data)
        print(f"❌ Error applying to {job['title']} at {job['company']} – {status}" if "Failed" in status else f"✅ Applied to {job['title']} at {job['company']} – {status}")

@app.route('/submit', methods=['POST'])
def submit():
    print("🔥 Form submitted — backend route triggered!")

    # Grab data from form or URL query
    job_title = request.form.get("job_title") or request.args.get("job_title")
    location = request.form.get("location") or request.args.get("location")
    first_name = request.form.get("first_name") or request.args.get("first_name")
    last_name = request.form.get("last_name") or request.args.get("last_name")
    phone = request.form.get("phone_number") or request.args.get("phone_number")
    email = request.form.get("email") or request.args.get("email")
    password = request.form.get("password") or request.args.get("password")
    confirm = request.form.get("confirm_password") or request.args.get("confirm_password")
    preferred_salary = request.form.get("preferred_salary") or request.args.get("preferred_salary")
    num_jobs = request.form.get("num_jobs") or request.args.get("num_jobs")
    print(f"📨 Received from frontend: job_title={job_title}, location={location}, email={email}, num_jobs={num_jobs}")

    if not email or password != confirm:
        return jsonify({"status": "error", "message": "Missing or mismatched credentials."}), 400

    if not password:
        return jsonify({"status": "error", "message": "Missing password."}), 400

    hashed_pw = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    # Save uploaded files 
    resume_file = request.files.get("resume")
    profile_photo = request.files.get("profilePhoto")

    resume_path = None
    profile_photo_path = None

    if resume_file:
        resume_filename = os.path.join("uploads", resume_file.filename)
        resume_file.save(resume_filename)
        resume_path = resume_filename

    if profile_photo:
        photo_filename = os.path.join("uploads", profile_photo.filename)
        profile_photo.save(photo_filename)
        profile_photo_path = photo_filename

    now = datetime.datetime.utcnow().isoformat()
    doc_ref = db.collection("users").document(email)
    doc = doc_ref.get()

    user_data = {
        "job_title": job_title,
        "location": location,
        "first_name": first_name,
        "last_name": last_name,
        "phone": phone,
        "email": email,
        "password_hash": hashed_pw,
        "salary": preferred_salary,
        "resume_path": resume_path or "uploads/resume.pdf",
        "profile_photo": profile_photo_path or "uploads/photo.jpg",
        "num_jobs": int(num_jobs) if num_jobs else 5,
        "updated_at": now,
    }

    if doc.exists:
        # Get the old values you want to preserve
        old_data = doc.to_dict()
        # Only update fields that should change—preserve these:
        user_data["free_uses_left"] = old_data.get("free_uses_left", 5)
        user_data["application_count"] = old_data.get("application_count", 0)
        user_data["plan_id"] = old_data.get("plan_id", "free")
        user_data["subscription_status"] = old_data.get("subscription_status", "active")
        user_data["created_at"] = old_data.get("created_at", now)
        # Only update these fields—does NOT overwrite the whole doc
        doc_ref.update(user_data)
    else:
        # New user: set default values
        user_data.update({
            "free_uses_left": 5,
            "application_count": 0,
            "plan_id": "free",
            "subscription_status": "active",
            "created_at": now,
            "stripe_customer_id": None
        })
        doc_ref.set(user_data)
    print("✅ User data saved to Firebase")

    # Start auto-apply
    job_results = fetch_jobs(job_title, location, limit=user_data["num_jobs"])
    logs = []

    for job in job_results:
        print(f"➡️ Visiting: {job['link']}")
        status = apply_to_job(job, user_data)
        logs.append(f"{job['title']} at {job['company']} – {status}")

    return jsonify({"status": "success", "log": logs})

if __name__ == "__main__":
    app.run(debug=True)
