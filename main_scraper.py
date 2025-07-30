import os
import firebase_admin
from firebase_admin import credentials, firestore
import requests
from bs4 import BeautifulSoup
import json
import time
from datetime import datetime, timedelta
import re

def initialize_firebase():
    """Initializes Firebase Admin SDK using credentials from environment variables."""
    try:
        service_account_key_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT_KEY_JSON')
        if not service_account_key_json:
            print("ERROR: Firebase service account key JSON not found in environment variables.")
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
    """
    Calls the Gemini API with a retry mechanism and forced JSON output.
    """
    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        print("ERROR: GEMINI_API_KEY not found in environment variables.")
        return None

    model = 'gemini-2.0-flash'
    api_url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}'
    
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
        }
    }
    
    max_retries = 3
    base_delay = 5
    for attempt in range(max_retries):
        try:
            response = requests.post(api_url, json=payload, headers={'Content-Type': 'application/json'}, timeout=60)
            
            if response.status_code in [500, 502, 503, 504]:
                print(f"WARNING: Received server error {response.status_code}. Retrying in {base_delay * (attempt + 1)}s...")
                time.sleep(base_delay * (attempt + 1))
                continue

            response.raise_for_status()
            
            result = response.json()
            text = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', None)
            
            if not text:
                print("WARNING: Gemini API returned an empty response.")
                return None

            return json.loads(text)

        except requests.exceptions.RequestException as e:
            print(f"A network error occurred on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                time.sleep(base_delay * (attempt + 1))
            else:
                print("Max retries reached. Giving up.")
                return None
        except Exception as e:
            print(f"An unexpected error occurred in call_gemini_api: {e}")
            return None

    return None

def get_base_title(title):
    """
    Creates a highly restrictive, unique identifier for a post to prevent duplicates.
    """
    title = title.lower()
    # More aggressive regex to remove noise words, years, and numbers
    title = re.sub(r'recruitment|online form|apply online|vacancy|\d{4,}|posts?|\[|\]|admit card|result|answer key|marks|phase|advt|no\.|for|various|post|of', '', title)
    title = re.sub(r'\b\d+\b', '', title)
    words = sorted(filter(None, title.split()))
    return ' '.join(words)

def find_main_content(soup):
    """
    Finds the main content container of a page using a series of fallbacks.
    """
    content_div = soup.find("div", class_="post-content-right")
    if content_div: return content_div
    content_div = soup.find("div", id="post")
    if content_div: return content_div
    content_div = soup.find("article")
    if content_div: return content_div
    content_div = soup.find("main")
    if content_div: return content_div
    return soup.find("body")


def scrape_section(db, app_id, section_id, collection_name):
    """
    Scrapes a specific section with a highly restrictive two-step duplicate filter.
    """
    URL = f"https://www.sarkariresult.com.im/{section_id}/"
    print("-" * 50)
    print(f"Scraping {URL} for new {collection_name}...")
    
    section_keywords = {
        "jobs": {"include": ["recruitment", "vacancy", "post", "apprentice", "form"], "exclude": ["result", "admit card", "answer key", "marks", "yojana", "syllabus", "exam date"]},
        "results": {"include": ["result", "marks", "score card", "final result", "cut off"], "exclude": ["admit card", "recruitment", "apply", "notification", "exam date"]},
        "admitCards": {"include": ["admit card", "exam date", "exam city", "status", "check"], "exclude": ["result", "recruitment", "apply", "notification", "answer key", "final result"]}
    }

    keywords = section_keywords.get(collection_name)
    if not keywords: return

    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        page = requests.get(URL, headers=headers, timeout=30)
        page.raise_for_status()
        
        soup = BeautifulSoup(page.content, "html.parser")
        
        unique_links = set()
        for link_tag in soup.find_all('a', href=True):
            unique_links.add((link_tag.text.strip(), link_tag['href']))

        item_links = []
        for text, href in unique_links:
            lower_text = text.lower()
            if any(key in lower_text for key in keywords["include"]) and not any(key in lower_text for key in keywords["exclude"]):
                item_links.append((text, href))

        if not item_links:
            print(f"Could not find any new potential links in section '{section_id}'.")
            return

        print(f"Found {len(item_links)} potential new items. Checking against database...")

        processed_count = 0
        for title, url in item_links:
            if processed_count >= 5:
                print("Processed 5 new items. Stopping to conserve API quota for this section.")
                break

            if not title or not url: continue

            items_ref = db.collection(f"artifacts/{app_id}/public/data/{collection_name}")
            
            # --- UPDATED: Changed the time window for duplicate checks to 5 days ---
            cutoff_date = (datetime.now() - timedelta(days=5)).strftime('%Y-%m-%d')
            
            # STEP 1: Check if the exact URL has been posted recently.
            url_query = items_ref.where("sourceUrl", "==", url).where("postedDate", ">=", cutoff_date).limit(1).get()
            if len(url_query) > 0:
                continue

            # STEP 2: If URL is new, check the cleaned base title.
            base_title = get_base_title(title)
            if not base_title: continue
            
            title_query = items_ref.where("baseTitle", "==", base_title).where("postedDate", ">=", cutoff_date).limit(1).get()
            if len(title_query) > 0:
                continue

            print(f"Processing new {collection_name[:-1]}: {title}")
            
            try:
                job_page = requests.get(url, headers=headers, timeout=30)
                job_page.raise_for_status()
                job_soup = BeautifulSoup(job_page.content, "html.parser")
                
                content_div = find_main_content(job_soup)
                notification_text = content_div.get_text(separator="\n", strip=True) if content_div else ""

                if not notification_text:
                    print(f"WARNING: Could not find content for '{title}'. Skipping.")
                    continue

                today_date = datetime.now().strftime('%Y-%m-%d')
                prompt = ""
                if collection_name == 'jobs':
                    prompt = f"""Analyze the job notification text. Create a JSON object with keys: "title", "organization", "lastDate" (YYYY-MM-DD), "description" (one-sentence summary), and "notificationText" (detailed summary). Use this exact title: "{title}". Set "postedDate" to "{today_date}". Text: --- {notification_text} ---"""
                elif collection_name == 'results':
                    prompt = f"""Analyze the result notification text. Create a JSON object with keys: "title", "organization". Use this exact title: "{title}". Set "postedDate" (result date) to "{today_date}". Text: --- {notification_text} ---"""
                elif collection_name == 'admitCards':
                    prompt = f"""Analyze the admit card text. Create a JSON object with keys: "title", "organization", "lastDate" (exam date, YYYY-MM-DD). Use this exact title: "{title}". Set "postedDate" (admit card release date) to "{today_date}". Text: --- {notification_text} ---"""

                structured_data = call_gemini_api(prompt)

                if structured_data and isinstance(structured_data, dict):
                    structured_data["postedDate"] = structured_data.get("postedDate", today_date)
                    structured_data["baseTitle"] = base_title
                    structured_data["sourceUrl"] = url 
                    
                    if collection_name == 'jobs':
                        structured_data["applicationUrl"] = url
                        structured_data["notificationPdfUrl"] = url
                    else:
                        structured_data["url"] = url
                    
                    items_ref.add(structured_data)
                    print(f"SUCCESS: Added '{title}' to Firestore.")
                    processed_count += 1
                else:
                    print(f"FAILED: Could not get valid structured data for '{title}'.")

            except Exception as e:
                print(f"ERROR: Could not process details for '{title}': {e}")
            
            print("Waiting for 5 seconds before next item...")
            time.sleep(5)

    except Exception as e:
        print(f"An unrecoverable error occurred while scraping section {section_id}: {e}")

def main():
    """Main function to run the scraper for all sections."""
    db = initialize_firebase()
    if db:
        app_id = os.environ.get('FIREBASE_PROJECT_ID', 'my-job-porta')
        
        scrape_section(db, app_id, "latestjob", "jobs")
        print("\nWaiting for 15 seconds before scraping next section...\n")
        time.sleep(15)
        
        scrape_section(db, app_id, "result", "results")
        print("\nWaiting for 15 seconds before scraping next section...\n")
        time.sleep(15)
        
        scrape_section(db, app_id, "admit-card", "admitCards")
        
        print("-" * 50)
        print("Scraping run finished.")
    else:
        print("Could not connect to Firebase. Exiting.")

if __name__ == "__main__":
    main()
