import os
import firebase_admin
from firebase_admin import credentials, firestore
import requests
from bs4 import BeautifulSoup
import json
import time
from datetime import datetime

def initialize_firebase():
    """Initializes Firebase Admin SDK using credentials from environment variables."""
    try:
        service_account_key_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT_KEY_JSON')
        if not service_account_key_json:
            print("Firebase service account key JSON not found in environment variables.")
            return None

        service_account_info = json.loads(service_account_key_json)
        cred = credentials.Certificate(service_account_info)
        
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
            
        print("Firebase connection successful.")
        return firestore.client()
    except Exception as e:
        print(f"An error occurred during Firebase initialization: {e}")
        return None

def call_gemini_api(prompt):
    """Calls the Gemini API to process the notification text."""
    api_key = "" # This will be handled by the environment
    model = 'gemini-2.0-flash'
    api_url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}'
    
    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt}]
        }]
    }
    
    try:
        response = requests.post(api_url, json=payload, headers={'Content-Type': 'application/json'})
        response.raise_for_status()
        result = response.json()
        
        text = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
        
        if text:
            text = text.strip().replace('```json', '').replace('```', '').strip()
        
        return json.loads(text)
    except requests.exceptions.RequestException as e:
        print(f"Error calling Gemini API: {e}")
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON from Gemini API response: {e}")
        print(f"Raw response text: {text}")
    except Exception as e:
        print(f"An unexpected error occurred in call_gemini_api: {e}")
    return None


def scrape_website(db, app_id):
    """Scrapes the job listing website, processes new jobs, and adds them to Firestore."""
    URL = "https://www.sarkariresult.com.im/latestjob/"
    
    print(f"Scraping {URL} for new jobs...")
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        page = requests.get(URL, headers=headers, timeout=30)
        page.raise_for_status()
        
        soup = BeautifulSoup(page.content, "html.parser")
        
        # ** IMPROVED SCRAPING LOGIC **
        # This selector is more specific and robust. It looks for list items within the main post content.
        job_links = soup.select("#post ul li a") 

        if not job_links:
            print("Could not find any job links using the specified selector. The website structure may have changed.")
            return

        print(f"Found {len(job_links)} potential job links.")

        for link in job_links:
            job_title = link.text.strip()
            job_url = link.get("href")

            if not job_title or not job_url:
                continue

            jobs_ref = db.collection(f"artifacts/{app_id}/public/data/jobs")
            existing_job_query = jobs_ref.where("title", "==", job_title).limit(1).get()
            
            if len(existing_job_query) > 0:
                print(f"Job '{job_title}' already exists. Skipping.")
                continue

            print(f"Processing new job: {job_title}")
            
            try:
                job_page = requests.get(job_url, headers=headers, timeout=30)
                job_page.raise_for_status()
                job_soup = BeautifulSoup(job_page.content, "html.parser")
                
                content_div = job_soup.find("div", id="post")
                if content_div:
                    notification_text = content_div.get_text(separator="\n", strip=True)
                else:
                    print(f"Could not find content div for '{job_title}'. Skipping.")
                    continue
                
                prompt = f"""
                You are an expert government job notification analyst. Analyze the following text and extract the key information.
                Provide the output ONLY as a valid JSON object. Do not include any text before or after the JSON.

                The JSON object must have these exact keys:
                - "title": The official name of the recruitment or exam. Use the title: "{job_title}".
                - "organization": The name of the recruiting body (e.g., Staff Selection Commission).
                - "vacancies": The total number of posts as a string (e.g., "17,727", "Not Specified").
                - "postedDate": The application start date, in "YYYY-MM-DD" format.
                - "lastDate": The last date to apply, in "YYYY-MM-DD" format.
                - "education": A concise summary of the required qualification (e.g., "10+2 Intermediate", "Bachelor's Degree").
                - "notificationText": A detailed summary including age limits, application fees for different categories, the full selection process, and a syllabus outline.

                Here is the text to analyze:
                ---
                {notification_text}
                ---
                """

                job_data = call_gemini_api(prompt)

                if job_data:
                    job_data["applicationUrl"] = job_url
                    job_data["notificationPdfUrl"] = job_url
                    
                    jobs_ref.add(job_data)
                    print(f"Successfully added job: {job_data['title']}")
                else:
                    print(f"Failed to process job data for: {job_title}")

            except requests.exceptions.RequestException as e:
                print(f"Could not fetch job page for '{job_title}': {e}")
            
            time.sleep(2) # Be respectful to the server

    except requests.exceptions.RequestException as e:
        print(f"An error occurred while scraping the website: {e}")
    except Exception as e:
        print(f"An unexpected error occurred in scrape_website: {e}")

def main():
    """Main function to run the scraper."""
    db = initialize_firebase()
    if db:
        app_id = os.environ.get('FIREBASE_PROJECT_ID', 'my-job-porta')
        scrape_website(db, app_id)
    else:
        print("Could not connect to Firebase. Exiting.")

if __name__ == "__main__":
    main()
