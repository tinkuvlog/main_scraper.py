import os
import firebase_admin
from firebase_admin import credentials, firestore
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
import json
import time
from datetime import datetime
import re
import logging
from typing import Optional, Dict, Any

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('job_scraper.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def initialize_firebase() -> Optional[firestore.Client]:
    """Initializes Firebase Admin SDK using credentials from environment variables."""
    try:
        service_account_key_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT_KEY_JSON')
        if not service_account_key_json:
            logger.error("FIREBASE_SERVICE_ACCOUNT_KEY_JSON environment variable not set.")
            return None

        try:
            service_account_info = json.loads(service_account_key_json)
            required_fields = ['project_id', 'private_key', 'client_email']
            if not all(field in service_account_info for field in required_fields):
                logger.error("Invalid Firebase service account JSON: missing required fields.")
                return None
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse FIREBASE_SERVICE_ACCOUNT_KEY_JSON: {e}")
            return None

        cred = credentials.Certificate(service_account_info)
        
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
            logger.info(f"Firebase initialized with project ID: {service_account_info['project_id']}")
        
        return firestore.client()
    except Exception as e:
        logger.error(f"Failed to initialize Firebase: {e}")
        return None

def create_session_with_retries() -> requests.Session:
    """Creates a requests session with retry logic."""
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

def call_gemini_api(prompt: str) -> Optional[Dict[str, Any]]:
    """Calls the Gemini API to process notification text."""
    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        logger.error("GEMINI_API_KEY environment variable not set.")
        return None

    model = 'gemini-2.0-flash'
    api_url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}'
    
    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt}]
        }]
    }
    
    session = create_session_with_retries()
    
    try:
        response = session.post(api_url, json=payload, headers={'Content-Type': 'application/json'}, timeout=30)
        response.raise_for_status()
        result = response.json()
        
        candidates = result.get('candidates', [])
        if not candidates:
            logger.error("No candidates found in Gemini API response.")
            return None
        
        parts = candidates[0].get('content', {}).get('parts', [])
        if not parts:
            logger.error("No content parts found in Gemini API response.")
            return None
        
        text = parts[0].get('text', '')
        if not text:
            logger.error("Empty text in Gemini API response.")
            return None
        
        text = text.strip().replace('```json', '').replace('```', '').strip()
        return json.loads(text)
    
    except requests.exceptions.RequestException as e:
        logger.error(f"Error calling Gemini API: {e}")
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding JSON from Gemini API response: {e}")
        logger.debug(f"Raw response text: {text}")
    except Exception as e:
        logger.error(f"Unexpected error in call_gemini_api: {e}")
    return None

def extract_job_content(soup: BeautifulSoup) -> str:
    """Extracts job content from a page using multiple fallback selectors."""
    selectors = [
        ('div', {'id': 'post'}),
        ('div', {'class': re.compile('content|main|post-content|article')}),
        ('article', {}),
        ('section', {}),
        ('body', {})
    ]
    
    for tag, attrs in selectors:
        content = soup.find(tag, attrs)
        if content:
            return content.get_text(separator="\n", strip=True)
    
    logger.warning("No specific content found, falling back to entire page text.")
    return soup.get_text(separator="\n", strip=True)

def scrape_website(db: firestore.Client, app_id: str) -> None:
    """Scrapes the job listing website and adds new jobs to Firestore."""
    URL = "https://www.sarkariresult.com.im/latestjob/"
    logger.info(f"Scraping {URL} for new jobs...")
    
    session = create_session_with_retries()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    try:
        response = session.get(URL, headers=headers, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        
        # Multiple selectors for job lists
        list_selectors = [
            ('ul', {}),
            ('div', {'class': re.compile('job-list|listing|posts|updates')}),
            ('section', {'class': re.compile('job|listing|posts')})
        ]
        
        job_links = []
        for tag, attrs in list_selectors:
            containers = soup.find_all(tag, attrs)
            for container in containers:
                links = container.find_all('a')
                for link in links:
                    href = link.get('href')
                    text = link.text.strip()
                    if href and (re.search(r'\d{4}', text) or any(keyword in text.lower() for keyword in ['recruitment', 'online form', 'vacancy', 'jobs'])):
                        job_links.append((link, text, href))
        
        if not job_links:
            logger.warning("No job links found. Website structure may have changed.")
            return
        
        logger.info(f"Found {len(job_links)} potential job links.")
        
        jobs_ref = db.collection(f"artifacts/{app_id}/public/data/jobs")
        
        for link, job_title, job_url in job_links:
            if not job_title or not job_url:
                logger.warning("Skipping job with missing title or URL.")
                continue
            
            # Enhanced duplicate check
            existing_job_query = jobs_ref.where("title", "==", job_title).where("applicationUrl", "==", job_url).limit(1).get()
            if existing_job_query:
                logger.info(f"Job '{job_title}' already exists. Skipping.")
                continue
            
            logger.info(f"Processing new job: {job_title}")
            
            try:
                job_response = session.get(job_url, headers=headers, timeout=30)
                job_response.raise_for_status()
                job_soup = BeautifulSoup(job_response.content, "html.parser")
                
                notification_text = extract_job_content(job_soup)
                
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
                    job_data["scrapedAt"] = datetime.utcnow().isoformat()
                    
                    jobs_ref.add(job_data)
                    logger.info(f"Successfully added job: {job_data['title']}")
                else:
                    logger.error(f"Failed to process job data for: {job_title}")
                
                time.sleep(2)  # Respectful delay
            
            except requests.exceptions.RequestException as e:
                logger.error(f"Failed to fetch job page for '{job_title}': {e}")
            except Exception as e:
                logger.error(f"Unexpected error processing job '{job_title}': {e}")
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch main page: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in scrape_website: {e}")

def main():
    """Main function to run the scraper."""
    # Validate environment variables
    required_env_vars = ['FIREBASE_SERVICE_ACCOUNT_KEY_JSON', 'GEMINI_API_KEY', 'FIREBASE_PROJECT_ID']
    missing_vars = [var for var in required_env_vars if not os.environ.get(var)]
    if missing_vars:
        logger.error(f"Missing environment variables: {', '.join(missing_vars)}")
        return
    
    db = initialize_firebase()
    if not db:
        logger.error("Could not connect to Firebase. Exiting.")
        return
    
    app_id = os.environ.get('FIREBASE_PROJECT_ID')
    scrape_website(db, app_id)

if __name__ == "__main__":
    main()
