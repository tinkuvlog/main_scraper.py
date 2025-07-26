# main_scraper.py
# This script automates the process of finding, processing, and posting government job notifications.

import requests
from bs4 import BeautifulSoup
import json
import os
from datetime import datetime

# Firebase Admin SDK for backend database operations
import firebase_admin
from firebase_admin import credentials, firestore

# --- CONFIGURATION ---

# IMPORTANT: Firebase Admin SDK Setup
# 1. Go to your Firebase project settings -> Service Accounts.
# 2. Click "Generate new private key" and download the JSON file.
# 3. Rename the file to "serviceAccountKey.json" and place it in the same directory as this script.
# 4. Make sure to keep this file secure and do not share it publicly.
try:
    cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("Firebase connection successful.")
except Exception as e:
    print(f"Firebase connection failed. Make sure 'serviceAccountKey.json' is in the correct path. Error: {e}")
    db = None

# Your App ID from the frontend (must match to write to the correct database path)
APP_ID = "sarkari-job-finder" 

# Gemini API Configuration
# NOTE: In a real production environment (like GitHub Actions), you would store this
# as a secure "secret" and not directly in the code.
GEMINI_API_KEY = "" # Leave empty, handled by environment
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"

# Target website to scrape
# We will use the SSC "Latest News" section as an example
TARGET_URL = "https://ssc.gov.in/portal/latest-news"

# --- HELPER FUNCTIONS ---

def call_gemini_api(prompt):
    """Sends a prompt to the Gemini API and returns the structured response."""
    headers = {'Content-Type': 'application/json'}
    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]})
    
    try:
        response = requests.post(GEMINI_API_URL, headers=headers, data=payload)
        response.raise_for_status()  # Raises an HTTPError for bad responses (4xx or 5xx)
        result = response.json()
        
        # Safely extract the text content
        candidate = result.get('candidates', [{}])[0]
        content = candidate.get('content', {}).get('parts', [{}])[0]
        text = content.get('text', '{}') # Return empty JSON string on failure
        
        return json.loads(text) # The prompt asks for JSON, so we parse it
    except requests.exceptions.RequestException as e:
        print(f"Error calling Gemini API: {e}")
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON from Gemini API response: {e}")
        print(f"Raw response text: {response.text}")
    except Exception as e:
        print(f"An unexpected error occurred in call_gemini_api: {e}")
        
    return None


def check_if_job_exists(notification_title):
    """Checks if a job with the same title already exists in Firestore to avoid duplicates."""
    if not db: return True # Assume it exists if DB is not connected
    
    jobs_ref = db.collection(f"artifacts/{APP_ID}/public/data/jobs")
    query = jobs_ref.where("title", "==", notification_title).limit(1)
    results = query.stream()
    
    return len(list(results)) > 0


def save_job_to_firestore(job_data):
    """Saves a new job dictionary to the Firestore database."""
    if not db:
        print("Cannot save job, Firestore not connected.")
        return
        
    try:
        jobs_ref = db.collection(f"artifacts/{APP_ID}/public/data/jobs")
        jobs_ref.add(job_data)
        print(f"SUCCESS: Saved new job '{job_data['title']}' to Firestore.")
    except Exception as e:
        print(f"ERROR: Could not save job '{job_data['title']}' to Firestore. Error: {e}")


# --- CORE SCRAPING AND PROCESSING LOGIC ---

def process_notification_text(raw_text):
    """Uses Gemini API to process raw notification text into structured JSON."""
    
    print("\nProcessing text with Gemini API...")
    
    # This is a powerful prompt that tells the AI exactly what to extract and in what format.
    prompt = f"""
    You are an expert government job notification analyst. Analyze the following text and extract the key information.
    Provide the output ONLY as a valid JSON object. Do not include any text before or after the JSON.

    The JSON object must have these exact keys:
    - "title": The official name of the recruitment or exam.
    - "organization": The name of the recruiting body (e.g., Staff Selection Commission).
    - "vacancies": The total number of posts as a string (e.g., "17,727", "Not Specified").
    - "postedDate": The date the notification was posted, in "YYYY-MM-DD" format.
    - "lastDate": The last date to apply, in "YYYY-MM-DD" format.
    - "education": A concise summary of the required qualification (e.g., "10+2 Intermediate", "Bachelor's Degree").
    - "applicationUrl": The direct official URL to apply online.
    - "notificationPdfUrl": The direct official URL to the full PDF notification.
    - "notificationText": A detailed summary including age limits, application fees for different categories, the full selection process, and a syllabus outline.

    Here is the text to analyze:
    ---
    {raw_text}
    ---
    """
    
    return call_gemini_api(prompt)


def scrape_ssc_website():
    """Scrapes the SSC website for new job notifications."""
    print(f"Scraping {TARGET_URL} for new jobs...")
    
    try:
        response = requests.get(TARGET_URL, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Find the table containing the latest news
        # Note: Website structures can change. This selector might need updating in the future.
        news_table = soup.find('table', class_='table')
        if not news_table:
            print("Could not find the news table on the page.")
            return
            
        # Iterate through each row in the table body
        for row in news_table.find('tbody').find_all('tr'):
            cells = row.find_all('td')
            if len(cells) < 3:
                continue

            # Extract data from the row
            post_date_str = cells[1].text.strip()
            title = cells[2].text.strip()
            link_tag = cells[2].find('a')
            
            if not link_tag or not link_tag.get('href'):
                continue
                
            # Construct the full PDF link
            pdf_url = "https://ssc.gov.in" + link_tag['href']
            
            print(f"\nFound potential job: '{title}'")
            
            # 1. CHECK FOR DUPLICATES
            if check_if_job_exists(title):
                print(f"SKIPPING: Job '{title}' already exists in the database.")
                continue
            
            # This is a simplified text extraction. A real-world scraper would use a library
            # like PyPDF2 or pdfplumber to extract text from the actual PDF at `pdf_url`.
            # For this example, we'll use the title and a placeholder as the "raw text".
            # In a real implementation, you would download the PDF and extract its text here.
            raw_notification_text = f"Notification Title: {title}. Posted on: {post_date_str}. Full details are in the PDF at {pdf_url}. Please extract all relevant information such as vacancies, eligibility, and syllabus from the official document."

            # 2. PROCESS WITH AI
            job_data = process_notification_text(raw_notification_text)
            
            if job_data:
                # The AI might not get the URLs right from the limited text, so we override them.
                job_data['notificationPdfUrl'] = pdf_url
                job_data['applicationUrl'] = "https://ssc.gov.in/" # Main portal for application
                job_data['title'] = title # Ensure title is accurate
                
                # 3. SAVE TO DATABASE
                save_job_to_firestore(job_data)

    except requests.exceptions.RequestException as e:
        print(f"An error occurred while scraping the website: {e}")


# --- MAIN EXECUTION ---
if __name__ == "__main__":
    if db:
        scrape_ssc_website()
    else:
        print("Script cannot run because the database connection failed.")

