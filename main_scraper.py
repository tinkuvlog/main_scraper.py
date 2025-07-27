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

def scrape_ssc(db, app_id):
    """Scrapes the official Staff Selection Commission (SSC) website."""
    URL = "https://ssc.gov.in/portal/latest-news"
    print(f"Scraping SSC: {URL}")
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        page = requests.get(URL, headers=headers, timeout=30)
        page.raise_for_status()
        soup = BeautifulSoup(page.content, "html.parser")
        
        # This selector targets the rows in the latest news table
        news_items = soup.select(".view-content table tbody tr")
        print(f"Found {len(news_items)} items on SSC.")

        for item in news_items:
            title_tag = item.find('a')
            if not title_tag:
                continue
            
            title = title_tag.text.strip()
            url = "https://ssc.gov.in" + title_tag.get('href')
            
            # Basic categorization based on title keywords
            if "result" in title.lower():
                collection_name = "results"
            elif "admit card" in title.lower() or "status" in title.lower():
                collection_name = "admitCards"
            elif "notice" in title.lower() or "recruitment" in title.lower() or "advertisement" in title.lower():
                 collection_name = "jobs"
            else:
                continue # Skip items that are not jobs, results, or admit cards

            items_ref = db.collection(f"artifacts/{app_id}/public/data/{collection_name}")
            existing_item_query = items_ref.where("title", "==", title).limit(1).get()
            
            if len(existing_item_query) > 0:
                print(f"SSC Item '{title}' already exists. Skipping.")
                continue

            print(f"Processing new SSC {collection_name[:-1]}: {title}")

            item_data = {
                "title": title,
                "url": url, # This is the primary link (e.g., to the PDF)
                "postedDate": datetime.now().strftime("%Y-%m-%d"),
                "organization": "Staff Selection Commission (SSC)"
            }

            if collection_name == 'jobs':
                 # For SSC, the notice IS the main content. We can use the title as the notification text for AI processing.
                notification_text = title 
                prompt = f"""
                Analyze the following job title and extract key details.
                - "title": "{title}"
                - "organization": "Staff Selection Commission (SSC)"
                - "vacancies": "Not Specified in title"
                - "postedDate": "{datetime.now().strftime('%Y-%m-%d')}"
                - "lastDate": "Please see notification PDF"
                - "education": "Please see notification PDF"
                - "notificationText": "{title}"
                """
                structured_data = call_gemini_api_for_job_details(prompt, title)
                if structured_data:
                    item_data.update(structured_data)
                    item_data["applicationUrl"] = url # Often the notice page has the apply link
                    item_data["notificationPdfUrl"] = url
            
            items_ref.add(item_data)
            print(f"Successfully added SSC {collection_name[:-1]}: {title}")
            time.sleep(2)

    except Exception as e:
        print(f"An error occurred while scraping SSC: {e}")


def main():
    """Main function to run the scraper."""
    db = initialize_firebase()
    if db:
        app_id = os.environ.get('FIREBASE_PROJECT_ID', 'my-job-porta')
        scrape_ssc(db, app_id)
        # In the future, you would add more functions like scrape_upsc(db, app_id) here.
    else:
        print("Could not connect to Firebase. Exiting.")

if __name__ == "__main__":
    main()
