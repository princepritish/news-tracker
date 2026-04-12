import feedparser
import hashlib
import json
import sqlite3
import requests
import os
from newspaper import Article
from groq import Groq

# ---------------- LOAD CONFIG ----------------
with open("config.json") as f:
    config = json.load(f)

SITES = config["sites"]
FILTERS = config["filters"]

KEYWORD = FILTERS.get("keyword", "solar").lower()

# -------- STATE NORMALIZATION --------
STATE_ALIASES = {
    "jharkhand": ["jharkhand"],
    "bihar": ["bihar"],
    "odisha": ["odisha", "orissa"],
    "assam": ["assam"],
    "chhattisgarh": ["chhattisgarh", "chattisgarh"],
    "madhya pradesh": ["madhya pradesh", "mp"],
    "andhra pradesh": ["andhra pradesh", "ap"],
    "uttar pradesh": ["uttar pradesh", "up"]
}

print("\n[INIT] Keyword:", KEYWORD)

# ---------------- ENV VARIABLES ----------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
BREVO_API_KEY = os.getenv("BREVO_API_KEY")
EMAIL_TO = os.getenv("EMAIL_RECEIVER")

if not GROQ_API_KEY or not BREVO_API_KEY:
    raise Exception("Missing API keys")

if not EMAIL_TO:
    raise Exception("EMAIL_RECEIVER not set")

# -------- EMAIL PARSE --------
EMAIL_TO = [e.strip() for e in EMAIL_TO.split(",")]
PRIMARY_EMAIL = EMAIL_TO[0]
BCC_EMAILS = EMAIL_TO[1:]

print("[EMAIL INIT] TO:", PRIMARY_EMAIL)
print("[EMAIL INIT] BCC:", BCC_EMAILS)

# ---------------- INIT CLIENT ----------------
client = Groq(api_key=GROQ_API_KEY)

# ---------------- DATABASE ----------------
DB_PATH = "/data/seen.db" if os.path.exists("/data") else "seen.db"

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute("CREATE TABLE IF NOT EXISTS articles (hash TEXT PRIMARY KEY)")
conn.commit()

print("[DB] Using:", DB_PATH)

def is_new(url):
    h = hashlib.md5(url.encode()).hexdigest()
    c.execute("SELECT 1 FROM articles WHERE hash=?", (h,))
    if c.fetchone():
        print("[DB] Duplicate")
        return False

    c.execute("INSERT INTO articles VALUES (?)", (h,))
    conn.commit()
    print("[DB] Stored")
    return True

# ---------------- BREVO EMAIL ----------------
def send_email(subject, body):
    print("[EMAIL] Sending via Brevo...")

    url = "https://api.brevo.com/v3/smtp/email"

    headers = {
        "accept": "application/json",
        "api-key": BREVO_API_KEY,
        "content-type": "application/json"
    }

    payload = {
        "sender": {
            "name": "Solar Alerts",
            "email": "princepritish26@gmail.com"   # 🔴 CHANGE THIS to your verified sender
        },
        "to": [{"email": PRIMARY_EMAIL}],
        "subject": subject,
        "textContent": body
    }

    if BCC_EMAILS:
        payload["bcc"] = [{"email": e} for e in BCC_EMAILS]

    try:
        res = requests.post(url, json=payload, headers=headers)

        print("[EMAIL STATUS]", res.status_code)
        print("[EMAIL RESPONSE]", res.text)

    except Exception as e:
        print("[EMAIL ERROR]", e)

# ---------------- STATE DETECTION ----------------
def find_state(text):
    text = text.lower()

    if KEYWORD not in text:
        return None

    for state, aliases in STATE_ALIASES.items():
        for name in aliases:
            if name in text:
                return state

    return None

# ---------------- LLM CHECK ----------------
def llm_detect(title, content):
    print("[LLM] Checking...")

    try:
        prompt = f"""
Answer ONLY YES or NO.

Return YES only if:
- Article is about SOLAR energy
- AND mentions one of these states:
Jharkhand, Bihar, Odisha, Assam, Chhattisgarh, Madhya Pradesh, Andhra Pradesh, Uttar Pradesh

Article:
{title}
{content[:2000]}
"""

        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}]
        )

        ans = response.choices[0].message.content.lower().strip()
        print("[LLM]", ans)

        return "yes" in ans

    except Exception as e:
        print("[LLM ERROR]", e)
        return False

# ---------------- SUMMARY ----------------
def summarize(title, content):
    print("[SUMMARY] Generating...")

    try:
        prompt = f"""
Summarize in:
- 3 bullet points
- 1 line why it matters

Article:
{title}
{content[:2000]}
"""

        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}]
        )

        return response.choices[0].message.content.strip()

    except Exception as e:
        print("[SUMMARY ERROR]", e)
        return "Summary unavailable"

# ---------------- PROCESS SITE ----------------
def process_site(site):
    print(f"\n===== {site['name']} =====")

    try:
        r = requests.get(site["rss"], headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        feed = feedparser.parse(r.content)
        print("[FEED] Entries:", len(feed.entries))
    except Exception as e:
        print("[FEED ERROR]", e)
        return

    for i, entry in enumerate(feed.entries[:30], 1):
        print(f"\n--- Article {i} ---")

        title = entry.get("title", "")
        url = entry.get("link", "")
        desc = entry.get("summary", "")

        print("[TITLE]", title)

        if not is_new(url):
            continue

        state = find_state(title + " " + desc)

        if state:
            print("[FAST MATCH]", state)
            content = desc

        else:
            print("[SCRAPE] Fetching full article...")
            try:
                article = Article(url)
                article.download()
                article.parse()
                content = article.text
            except Exception as e:
                print("[SCRAPE ERROR]", e)
                continue

            state = find_state(content)

            if not state:
                if not llm_detect(title, content):
                    print("[SKIP] Not relevant")
                    continue

                print("[SKIP] No valid state → ignore")
                continue

        summary = summarize(title, content)

        body = f"""
Title: {title}

State: {state}

Summary:
{summary}

Link: {url}
"""

        send_email(f"Solar Alert - {state}", body)

# ---------------- MAIN ----------------
if __name__ == "__main__":
    print("\n[SYSTEM] Running one cycle\n")

    for site in SITES:
        process_site(site)

    print("\n[SYSTEM] Done\n")
