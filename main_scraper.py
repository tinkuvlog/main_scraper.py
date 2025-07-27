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

def call_gemini_api(prompt):
    """Calls the Gemini API to process the notification text."""
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
        print(f"An error occurred in call_gemini_api: {e}")
    return None

def get_base_title(title):
    """Creates a simplified, unique identifier for a job title to prevent duplicates."""
    title = title.lower()
    title = re.sub(r'recruitment|online form|apply online|vacancy|\d{4}|posts|post|\[|\]', '', title)
    title = ' '.join(title.split())
    return title

def scrape_section(db, app_id, section_id, collection_name):
    """Scrapes a specific section (Latest Jobs, Results, Admit Cards) from the website."""
    URL = f"https://www.sarkariresult.com.im/{section_id}/"
    print(f"Scraping {URL} for new {collection_name}...")
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        page = requests.get(URL, headers=headers, timeout=30)
        page.raise_for_status()
        
        soup = BeautifulSoup(page.content, "html.parser")
        
        all_lists = soup.find_all('ul')
        item_links = []
        
        valid_keywords = ['recruitment', 'online', 'result', 'admit card', 'answer key', 'marks', 'admission', 'counseling']
        ignore_keywords = ['home', 'jobs', 'yojana', 'contact us', 'privacy policy']

        for lst in all_lists:
            links = lst.find_all('a')
            for link in links:
                href = link.get('href')
                text = link.text.strip().lower()
                
                if href and any(keyword in text for keyword in valid_keywords) and not any(keyword in text for keyword in ignore_keywords):
                     item_links.append(link)

        if not item_links:
            print(f"Could not find any potential links in section '{section_id}'.")
            return

        print(f"Found {len(item_links)} total links. Processing the top 10 new items.")

        processed_count = 0
        for link in item_links:
            if processed_count >= 10:
                print("Processed 10 new items for this section. Stopping to avoid rate limits.")
                break

            title = link.text.strip()
            url = link.get("href")

            if not title or not url:
                continue

            base_title = get_base_title(title)
            items_ref = db.collection(f"artifacts/{app_id}/public/data/{collection_name}")
            existing_item_query = items_ref.where("baseTitle", "==", base_title).limit(1).get()
            
            if len(existing_item_query) > 0:
                # This item already exists, so we don't count it towards our new item limit
                continue

            print(f"Processing new {collection_name[:-1]}: {title}")
            
            try:
                job_page = requests.get(url, headers=headers, timeout=30)
                job_page.raise_for_status()
                job_soup = BeautifulSoup(job_page.content, "html.parser")
                content_div = job_soup.find("div", id="post")
                notification_text = content_div.get_text(separator="\n", strip=True) if content_div else ""

                prompt = ""
                if collection_name == 'jobs':
                    prompt = f"""Analyze the text for a job notification. Extract details as a JSON object with keys: "title", "organization", "vacancies", "postedDate" (YYYY-MM-DD), "lastDate" (YYYY-MM-DD), "education", "description" (a one-sentence summary), "notificationText". Use this title: "{title}". Text: --- {notification_text} ---"""
                elif collection_name == 'results':
                    prompt = f"""Analyze the text for a result notification. Extract details as a JSON object with keys: "title", "organization", "postedDate" (Result Date, in YYYY-MM-DD format). Use this title: "{title}". Text: --- {notification_text} ---"""
                elif collection_name == 'admitCards':
                    prompt = f"""Analyze the text for an admit card notification. Extract details as a JSON object with keys: "title", "organization", "lastDate" (Exam Date, in YYYY-MM-DD format), "postedDate" (Admit Card release date, in YYYY-MM-DD format). Use this title: "{title}". Text: --- {notification_text} ---"""

                structured_data = call_gemini_api(prompt)

                if structured_data:
                    structured_data["url"] = url
                    structured_data["baseTitle"] = base_title
                    if collection_name == 'jobs':
                        structured_data["applicationUrl"] = url
                        structured_data["notificationPdfUrl"] = url
                    
                    items_ref.add(structured_data)
                    print(f"Successfully added {collection_name[:-1]}: {title}")
                    processed_count += 1
                else:
                    print(f"Failed to process data for: {title}")

            except Exception as e:
                print(f"Could not process details for '{title}': {e}")
            
            time.sleep(10) # Increased delay to 10 seconds to be safe

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
