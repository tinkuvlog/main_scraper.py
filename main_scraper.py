import os
import firebase_admin
from firebase_admin import credentials, firestore
import requests
from bs4 import BeautifulSoup
import json
import time
from datetime import datetime
import re

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

def call_gemini_api_for_job_details(prompt, job_title):
    """Calls Gemini API to extract structured data for a job posting."""
    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        print("ERROR: GEMINI_API_KEY not found in environment variables.")
        return None

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
    except Exception as e:
        print(f"An error occurred in call_gemini_api_for_job_details: {e}")
    return None

def scrape_section(db, app_id, section_id, collection_name):
    """Scrapes a specific section (Latest Jobs, Results, Admit Cards) from the website."""
    URL = f"https://www.sarkariresult.com.im/{section_id}/"
    print(f"Scraping {URL} for new {collection_name}...")
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        page = requests.get(URL, headers=headers, timeout=30)
        page.raise_for_status()
        
        soup = BeautifulSoup(page.content, "html.parser")
        
        post_content = soup.find(id="post")
        if not post_content:
            print(f"Could not find content for section '{section_id}'.")
            return

        links = post_content.find_all('a')
        print(f"Found {len(links)} potential links in {collection_name} section.")

        for link in links:
            title = link.text.strip()
            url = link.get("href")

            if not title or not url:
                continue

            items_ref = db.collection(f"artifacts/{app_id}/public/data/{collection_name}")
            existing_item_query = items_ref.where("title", "==", title).limit(1).get()
            
            if len(existing_item_query) > 0:
                print(f"{collection_name[:-1].capitalize()} '{title}' already exists. Skipping.")
                continue

            print(f"Processing new {collection_name[:-1]}: {title}")

            item_data = {
                "title": title,
                "url": url,
                "postedDate": datetime.now().strftime("%Y-%m-%d")
            }
            
            # If it's a job, we need to get more details
            if collection_name == 'jobs':
                try:
                    job_page = requests.get(url, headers=headers, timeout=30)
                    job_page.raise_for_status()
                    job_soup = BeautifulSoup(job_page.content, "html.parser")
                    content_div = job_soup.find("div", id="post")
                    notification_text = content_div.get_text(separator="\n", strip=True) if content_div else ""

                    prompt = f"""
                    Analyze the following job notification text and extract the details as a JSON object.
                    - "title": Use the title: "{title}".
                    - "organization": The name of the recruiting body.
                    - "vacancies": The total number of posts.
                    - "postedDate": The application start date in "YYYY-MM-DD" format.
                    - "lastDate": The last date to apply in "YYYY-MM-DD" format.
                    - "education": A concise summary of the qualification.
                    - "notificationText": A detailed summary.

                    Text: --- {notification_text} ---
                    """
                    
                    structured_data = call_gemini_api_for_job_details(prompt, title)
                    if structured_data:
                        item_data.update(structured_data)
                        item_data["applicationUrl"] = url
                        item_data["notificationPdfUrl"] = url
                except Exception as e:
                    print(f"Could not process details for job '{title}': {e}")
                    continue # Skip adding the job if details can't be processed

            items_ref.add(item_data)
            print(f"Successfully added {collection_name[:-1]}: {title}")
            time.sleep(2)

    except Exception as e:
        print(f"An error occurred while scraping section {section_id}: {e}")

def main():
    """Main function to run the scraper."""
    db = initialize_firebase()
    if db:
        app_id = os.environ.get('FIREBASE_PROJECT_ID', 'my-job-porta')
        scrape_section(db, app_id, "latestjob", "jobs")
        scrape_section(db, app_id, "result", "results")
        scrape_section(db, app_id, "admit-card", "admitCards")
    else:
        print("Could not connect to Firebase. Exiting.")

if __name__ == "__main__":
    main()
