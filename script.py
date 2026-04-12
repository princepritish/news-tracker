import feedparser
import hashlib
import json
import smtplib
import sqlite3
import time
import requests
import os
from email.mime.text import MIMEText
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

ALLOWED_STATES = list(STATE_ALIASES.keys())

print("\n[INIT] Keyword:", KEYWORD)
print("[INIT] Allowed states:", ALLOWED_STATES)

# ---------------- ENV VARIABLES ----------------
API_KEY = os.getenv("GROQ_API_KEY")

EMAIL = {
    "sender": os.getenv("EMAIL_SENDER"),
    "password": os.getenv("EMAIL_PASSWORD"),
    "receiver": os.getenv("EMAIL_RECEIVER")
}

if not API_KEY:
    raise Exception("Missing GROQ_API_KEY")

# ---------------- INIT CLIENT ----------------
client = Groq(api_key=API_KEY)

# ---------------- DATABASE ----------------
conn = sqlite3.connect("seen.db")
c = conn.cursor()
c.execute("CREATE TABLE IF NOT EXISTS articles (hash TEXT PRIMARY KEY)")
conn.commit()

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

# ---------------- EMAIL ----------------
def send_email(subject, body):
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = EMAIL["sender"]
        msg["To"] = EMAIL["receiver"]

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL["sender"], EMAIL["password"])
            server.send_message(msg)

        print("[EMAIL] Sent")

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

# ---------------- LLM FALLBACK ----------------
def llm_detect(title, content):
    try:
        prompt = f"""
Answer ONLY YES or NO.

Return YES only if:
- Article is about SOLAR energy
- AND mentions one of these states:
Jharkhand, Bihar, Odisha, Assam, Chhattisgarh, Madhya Pradesh, Andhra Pradesh, Uttar Pradesh

Rules:
- If only "India" → NO
- If other states → NO
- If no state → NO

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

        # STEP 1
        state = find_state(title + " " + desc)

        if state:
            content = desc
            print("[FAST MATCH]", state)

        else:
            try:
                article = Article(url)
                article.download()
                article.parse()
                content = article.text
                print("[SCRAPE] Done")
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

        # SUMMARY
        summary = summarize(title, content)

        # EMAIL
        message = f"""
Title: {title}

State: {state}

Summary:
{summary}

Link: {url}
"""

        send_email(f"Solar Alert - {state}", message)

# ---------------- MAIN ----------------
if __name__ == "__main__":
    print("\n[SYSTEM] Running one cycle\n")

    for site in SITES:
        process_site(site)

    print("\n[SYSTEM] Done\n")
