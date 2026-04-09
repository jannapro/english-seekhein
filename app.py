import os
import json
import random
import secrets
from datetime import date
from flask import Flask, render_template, request, Response, jsonify, stream_with_context, session, redirect, url_for
from openai import OpenAI
from dotenv import load_dotenv
import psycopg
from psycopg.rows import dict_row

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "english-seekhein-secret-2024")
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

DATABASE_URL = os.environ.get("DATABASE_URL")


# ── Database ────────────────────────────────────────────────────────────────

def get_db():
    return psycopg.connect(DATABASE_URL)


def init_db():
    if not DATABASE_URL:
        return
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    username VARCHAR(100) PRIMARY KEY,
                    unique_code VARCHAR(20) NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_memory (
                    username VARCHAR(100) PRIMARY KEY REFERENCES users(username) ON DELETE CASCADE,
                    total_sessions INTEGER DEFAULT 0,
                    total_sentences INTEGER DEFAULT 0,
                    common_mistakes JSONB DEFAULT '[]',
                    last_session DATE
                )
            """)
        conn.commit()


init_db()


# ── User helpers ─────────────────────────────────────────────────────────────

def get_user(username):
    """Return {username, code} or None. Case-insensitive lookup."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT username, unique_code FROM users WHERE LOWER(username) = LOWER(%s)",
                (username,)
            )
            row = cur.fetchone()
    return {"username": row[0], "code": row[1]} if row else None


def username_exists(username, exclude=None):
    with get_db() as conn:
        with conn.cursor() as cur:
            if exclude:
                cur.execute(
                    "SELECT 1 FROM users WHERE LOWER(username) = LOWER(%s) AND username != %s",
                    (username, exclude)
                )
            else:
                cur.execute(
                    "SELECT 1 FROM users WHERE LOWER(username) = LOWER(%s)",
                    (username,)
                )
            return cur.fetchone() is not None


def create_user(username, code):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, unique_code) VALUES (%s, %s)",
                (username, code)
            )
        conn.commit()


def rename_user(old_username, new_username):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET username = %s WHERE username = %s",
                (new_username, old_username)
            )
        conn.commit()


def remove_user(username):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE username = %s", (username,))
        conn.commit()


def generate_unique_code():
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    parts = [''.join(secrets.choice(chars) for _ in range(4)) for _ in range(3)]
    return '-'.join(parts)


# ── Memory helpers (per-user) ────────────────────────────────────────────────

def load_memory(username):
    with get_db() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM user_memory WHERE username = %s", (username,))
            row = cur.fetchone()
    if row:
        return {
            "total_sessions": row["total_sessions"],
            "total_sentences": row["total_sentences"],
            "common_mistakes": row["common_mistakes"],
            "last_session": str(row["last_session"]) if row["last_session"] else None,
        }
    return {"total_sessions": 0, "total_sentences": 0, "common_mistakes": [], "last_session": None}


def save_memory(username, memory):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_memory (username, total_sessions, total_sentences, common_mistakes, last_session)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (username) DO UPDATE SET
                    total_sessions = EXCLUDED.total_sessions,
                    total_sentences = EXCLUDED.total_sentences,
                    common_mistakes = EXCLUDED.common_mistakes,
                    last_session = EXCLUDED.last_session
            """, (
                username,
                memory["total_sessions"],
                memory["total_sentences"],
                json.dumps(memory["common_mistakes"]),
                memory["last_session"],
            ))
        conn.commit()


def update_memory_with_analysis(username, analysis):
    memory = load_memory(username)
    memory["total_sentences"] += 1
    memory["last_session"] = str(date.today())
    if analysis.get("has_errors"):
        for err in analysis.get("errors", []):
            mistake_text = err.get("mistake", "")
            found = next((m for m in memory["common_mistakes"] if m["mistake"] == mistake_text), None)
            if found:
                found["count"] += 1
            else:
                memory["common_mistakes"].append({
                    "mistake": mistake_text,
                    "fix": err.get("fix", ""),
                    "count": 1
                })
    memory["common_mistakes"].sort(key=lambda x: x["count"], reverse=True)
    memory["common_mistakes"] = memory["common_mistakes"][:10]
    save_memory(username, memory)


# ── Levels ───────────────────────────────────────────────────────────────────

LEVELS = [
    {"name": "Bad",             "emoji": "🌱", "min": 0},
    {"name": "Good",            "emoji": "📈", "min": 10},
    {"name": "Fluent",          "emoji": "⭐", "min": 30},
    {"name": "Native",          "emoji": "🏆", "min": 70},
    {"name": "Ready to Launch", "emoji": "🚀", "min": 150},
]

def get_user_level(total_sentences):
    idx = 0
    for i, lv in enumerate(LEVELS):
        if total_sentences >= lv["min"]:
            idx = i
    return {"index": idx, "name": LEVELS[idx]["name"], "emoji": LEVELS[idx]["emoji"]}


# ── Static data ───────────────────────────────────────────────────────────────

DAILY_WORDS = [
    "perseverance", "eloquent", "diligent", "ambiguous", "meticulous",
    "pragmatic", "resilient", "articulate", "profound", "innovative",
    "empathy", "tenacious", "gregarious", "lucid", "versatile",
    "integrity", "compassion", "curiosity", "patience", "gratitude"
]

GRAMMAR_TOPICS = [
    "Present Simple Tense",
    "Present Continuous Tense",
    "Past Simple Tense",
    "Future Tense",
    "Articles (a, an, the)",
    "Prepositions",
    "Modal Verbs (can, could, should, must)",
    "Comparatives and Superlatives",
    "Conditional Sentences",
    "Active and Passive Voice"
]

SYSTEM_PROMPT = """You are a friendly English teacher helping Urdu-speaking students learn English.
When a student makes grammatical errors, gently correct them and explain why in simple terms.
Keep responses concise (2-4 sentences), warm, and encouraging.
Use simple vocabulary suitable for beginners to intermediate learners.
If the student writes in Urdu/Roman Urdu, understand their message but always respond in English (you may add a brief Urdu note in parentheses if helpful).
Focus on practical conversation skills. End each response with a follow-up question to keep the conversation going."""


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/login")
def login_page():
    if session.get("username"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.json
    username = data.get("username", "").strip()
    if not username:
        return jsonify({"error": "Please enter a username"}), 400
    if len(username) < 3:
        return jsonify({"error": "Username must be at least 3 characters"}), 400
    if username_exists(username):
        return jsonify({"error": "This username is already taken. Try another one."}), 400
    code = generate_unique_code()
    create_user(username, code)
    session["username"] = username
    return jsonify({"username": username, "code": code})


@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.json
    username = data.get("username", "").strip()
    code = data.get("code", "").strip()
    user = get_user(username)
    if not user or user["code"] != code:
        return jsonify({"error": "Wrong username or unique code. Please try again."}), 401
    session["username"] = user["username"]
    return jsonify({"ok": True, "username": user["username"]})


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.pop("username", None)
    return jsonify({"ok": True})


# ── User management routes ────────────────────────────────────────────────────

@app.route("/api/user/info", methods=["GET"])
def user_info():
    username = session.get("username")
    if not username:
        return jsonify({"error": "Not logged in"}), 401
    user = get_user(username)
    memory = load_memory(username)
    level = get_user_level(memory.get("total_sentences", 0))
    return jsonify({"username": username, "code": user["code"] if user else "", "level": level})


@app.route("/api/user/change-username", methods=["POST"])
def change_username():
    username = session.get("username")
    if not username:
        return jsonify({"error": "Not logged in"}), 401
    data = request.json
    new_username = data.get("username", "").strip()
    if not new_username or len(new_username) < 3:
        return jsonify({"error": "Username must be at least 3 characters"}), 400
    if username_exists(new_username, exclude=username):
        return jsonify({"error": "Username already taken. Try another."}), 400
    rename_user(username, new_username)
    session["username"] = new_username
    return jsonify({"ok": True, "username": new_username})


@app.route("/api/user/delete", methods=["POST"])
def delete_account():
    username = session.get("username")
    if not username:
        return jsonify({"error": "Not logged in"}), 401
    remove_user(username)
    session.pop("username", None)
    return jsonify({"ok": True})


# ── Main app route ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if not session.get("username"):
        return redirect(url_for("login_page"))
    username = session.get("username")
    memory = load_memory(username)
    memory["total_sessions"] += 1
    save_memory(username, memory)
    level = get_user_level(memory.get("total_sentences", 0))
    return render_template("index.html", grammar_topics=GRAMMAR_TOPICS,
                           username=username, level=level)


# ── Chat ──────────────────────────────────────────────────────────────────────

@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json
    messages = data.get("messages", [])

    def generate():
        try:
            all_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
            stream = client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=1024,
                messages=all_messages,
                stream=True,
            )
            for chunk in stream:
                text = chunk.choices[0].delta.content
                if text:
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# ── Vocabulary ────────────────────────────────────────────────────────────────

@app.route("/api/vocabulary", methods=["POST"])
def vocabulary():
    data = request.json
    word = data.get("word", "").strip()
    if not word:
        return jsonify({"error": "Please enter a word"}), 400

    prompt = f"""Give information about the English word "{word}" in this exact JSON format:
{{
    "word": "{word}",
    "pronunciation": "phonetic pronunciation like /wɜːrd/",
    "word_type": "noun / verb / adjective / adverb / etc",
    "meaning_english": "clear, simple definition in English",
    "meaning_urdu": "Roman Urdu mein matlab (e.g.: Yeh lafz X matlab rakhta hai)",
    "example_sentences": [{{"en": "Simple example sentence 1.", "ur": "Roman Urdu translation 1"}}, {{"en": "Simple example sentence 2.", "ur": "Roman Urdu translation 2"}}],
    "synonyms": [{{"word": "synonym1", "urdu": "Roman Urdu matlab"}}, {{"word": "synonym2", "urdu": "Roman Urdu matlab"}}, {{"word": "synonym3", "urdu": "Roman Urdu matlab"}}],
    "antonyms": [{{"word": "antonym1", "urdu": "Roman Urdu matlab"}}, {{"word": "antonym2", "urdu": "Roman Urdu matlab"}}]
}}
Only respond with valid JSON. No extra text."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content)
        return jsonify(result)
    except json.JSONDecodeError:
        return jsonify({"error": "Could not process this word. Try another."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/daily-word", methods=["GET"])
def daily_word():
    word = random.choice(DAILY_WORDS)
    prompt = f"""Give information about the English word "{word}" in this exact JSON format:
{{
    "word": "{word}",
    "pronunciation": "phonetic pronunciation like /wɜːrd/",
    "word_type": "noun / verb / adjective / adverb / etc",
    "meaning_english": "clear, simple definition in English",
    "meaning_urdu": "Roman Urdu mein matlab",
    "example_sentences": [{{"en": "Simple example sentence 1.", "ur": "Roman Urdu translation 1"}}, {{"en": "Simple example sentence 2.", "ur": "Roman Urdu translation 2"}}],
    "synonyms": [{{"word": "synonym1", "urdu": "Roman Urdu matlab"}}, {{"word": "synonym2", "urdu": "Roman Urdu matlab"}}, {{"word": "synonym3", "urdu": "Roman Urdu matlab"}}],
    "antonyms": [{{"word": "antonym1", "urdu": "Roman Urdu matlab"}}, {{"word": "antonym2", "urdu": "Roman Urdu matlab"}}]
}}
Only respond with valid JSON. No extra text."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Grammar ───────────────────────────────────────────────────────────────────

@app.route("/api/grammar", methods=["POST"])
def grammar():
    data = request.json
    topic = data.get("topic", "Present Simple Tense")

    prompt = f"""Explain "{topic}" in English grammar in this exact JSON format:
{{
    "topic": "{topic}",
    "explanation_english": "2-3 sentence simple explanation in English",
    "explanation_urdu": "Roman Urdu mein 2-3 sentences ki explanation",
    "structure": "e.g.: Subject + Verb + Object",
    "rules": ["Rule 1: ...", "Rule 2: ...", "Rule 3: ..."],
    "examples": [
        {{"sentence": "example sentence", "urdu": "Roman Urdu translation", "note": "brief grammar note"}},
        {{"sentence": "example sentence 2", "urdu": "Roman Urdu translation", "note": "brief grammar note"}},
        {{"sentence": "example sentence 3", "urdu": "Roman Urdu translation", "note": "brief grammar note"}}
    ],
    "common_mistakes": [
        {{"wrong": "incorrect usage", "correct": "correct usage", "tip": "remember tip"}},
        {{"wrong": "incorrect usage 2", "correct": "correct usage 2", "tip": "remember tip"}}
    ]
}}
Only respond with valid JSON. No extra text."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content)
        return jsonify(result)
    except json.JSONDecodeError:
        return jsonify({"error": "Could not load grammar lesson. Try again."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Quiz ──────────────────────────────────────────────────────────────────────

@app.route("/api/quiz", methods=["POST"])
def quiz():
    data = request.json
    topic = data.get("topic", "General English")
    difficulty = data.get("difficulty", "beginner")

    prompt = f"""Create 5 English quiz questions about "{topic}" for {difficulty} level in this exact JSON format:
{{
    "topic": "{topic}",
    "difficulty": "{difficulty}",
    "questions": [
        {{
            "id": 1,
            "question": "question text here",
            "options": {{"A": "option text", "B": "option text", "C": "option text", "D": "option text"}},
            "correct": "A",
            "explanation": "why this answer is correct (1-2 sentences)"
        }}
    ]
}}
Make questions progressively harder. Use clear, simple language. Only respond with valid JSON. No extra text."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content)
        # Shuffle options so correct answer is not always A or B
        keys = ["A", "B", "C", "D"]
        for q in result.get("questions", []):
            correct_text = q["options"][q["correct"]]
            values = list(q["options"].values())
            random.shuffle(values)
            q["options"] = dict(zip(keys, values))
            q["correct"] = next(k for k, v in q["options"].items() if v == correct_text)
        return jsonify(result)
    except json.JSONDecodeError:
        return jsonify({"error": "Could not generate quiz. Try again."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Voice ─────────────────────────────────────────────────────────────────────

@app.route("/api/voice/transcribe", methods=["POST"])
def transcribe():
    if 'audio' not in request.files:
        return jsonify({"error": "No audio file"}), 400
    audio_file = request.files['audio']
    try:
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=("recording.webm", audio_file.stream, audio_file.mimetype),
            language="en"
        )
        return jsonify({"text": transcript.text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/voice/analyze", methods=["POST"])
def analyze():
    username = session.get("username")
    data = request.json
    user_text = data.get("text", "").strip()
    history = data.get("history", [])
    if not user_text:
        return jsonify({"error": "No text provided"}), 400

    past_mistakes = []
    if username:
        memory = load_memory(username)
        past_mistakes = memory.get("common_mistakes", [])

    mistake_context = ""
    if past_mistakes:
        top = past_mistakes[:3]
        mistake_context = "This student has previously struggled with: " + "; ".join(
            f"{m['mistake']} (fix: {m['fix']})" for m in top
        ) + ". Gently remind them if they repeat the same mistake."

    conversation_context = ""
    if history:
        lines = []
        for h in history[-6:]:
            role = "Student" if h["role"] == "user" else "Teacher"
            lines.append(f"{role}: {h['content']}")
        conversation_context = "Recent conversation:\n" + "\n".join(lines) + "\n\n"

    prompt = f"""You are an English teacher for an Urdu-speaking student.
{mistake_context}

{conversation_context}Now analyze this new sentence from the student: "{user_text}"

Respond in this exact JSON format:
{{
    "original": "{user_text}",
    "has_errors": true or false,
    "corrected": "corrected version (same as original if no errors)",
    "tense": "Present Simple / Present Continuous / Past Simple / Past Continuous / Future Simple / etc",
    "tense_urdu": "Roman Urdu mein (e.g.: Hal ka waqt / Mazi ka waqt / Mustaqbil ka waqt)",
    "errors": [
        {{"mistake": "what was wrong", "fix": "correct version", "explanation": "simple reason"}}
    ],
    "teacher_response": "Warm 2-3 sentence response. Do NOT repeat or confirm what the student said — they can already see it on screen. Go straight to the correction or encouragement, then ask a follow-up question to keep talking.",
    "encouragement": "Short Roman Urdu encouragement (e.g.: Bohot acha! / Sahi ja rahe ho! / Koshish karte raho!)"
}}
Only respond with valid JSON. No extra text."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content)
        if username:
            update_memory_with_analysis(username, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/voice/speak", methods=["POST"])
def speak():
    data = request.json
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400
    try:
        response = client.audio.speech.create(
            model="tts-1",
            voice="nova",
            input=text,
            speed=0.8,
        )
        return Response(response.content, mimetype="audio/mpeg")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Talk ──────────────────────────────────────────────────────────────────────

@app.route("/api/talk", methods=["POST"])
def talk():
    data = request.json
    history = data.get("history", [])

    system = """You are Alex, a friendly and warm person having a casual voice conversation with someone you just met.
Rules:
- Keep EVERY response very short: 1-2 sentences only, like real voice messages
- Be genuinely curious and friendly — like a real friend
- Ask only ONE question at a time
- Use simple everyday English (the person is learning English)
- If they make a grammar mistake, naturally use the correct form in your reply without pointing it out
- Take turns sharing about yourself too — make it feel mutual
- Topics flow naturally: greetings → name → where they're from → hobbies → family → food → daily life
- Be warm, fun, and encouraging
- Never say 'Great sentence!' or comment on their English — just talk naturally"""

    messages = [{"role": "system", "content": system}] + history

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=80,
            messages=messages,
            temperature=0.9,
        )
        text = response.choices[0].message.content.strip()
        return jsonify({"response": text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Memory ────────────────────────────────────────────────────────────────────

@app.route("/api/memory", methods=["GET"])
def get_memory():
    username = session.get("username")
    if not username:
        return jsonify({"error": "Not logged in"}), 401
    return jsonify(load_memory(username))


@app.route("/api/memory/clear", methods=["POST"])
def clear_memory():
    username = session.get("username")
    if not username:
        return jsonify({"error": "Not logged in"}), 401
    save_memory(username, {"total_sessions": 0, "total_sentences": 0, "common_mistakes": [], "last_session": None})
    return jsonify({"ok": True})


# ── How to Say ────────────────────────────────────────────────────────────────

@app.route("/api/how-to-say", methods=["POST"])
def how_to_say():
    data = request.json
    user_input = data.get("text", "").strip()
    if not user_input:
        return jsonify({"error": "Please enter something to translate"}), 400

    prompt = f"""The user wrote this in Urdu or Roman Urdu: "{user_input}"
They want to know how to say this naturally in English.

Respond in this exact JSON format:
{{
    "urdu_input": "{user_input}",
    "english_phrase": "The most natural English way to say it",
    "alternatives": ["A slightly different natural way", "Another variation if applicable"],
    "pronunciation_tip": "Simple tip on how to say the key words (e.g. stress, rhythm)",
    "grammar_note": "One sentence explaining what grammar structure is used (e.g. Present Continuous: Subject + is/am/are + verb-ing)",
    "example_conversation": [
        {{"speaker": "Someone asks", "line": "A question someone might ask you"}},
        {{"speaker": "You reply", "line": "Your English reply using the phrase"}}
    ],
    "urdu_tip": "Ek line Roman Urdu mein — kab aur kaise ye phrase use karein"
}}
Only respond with valid JSON. No extra text."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content)
        return jsonify(result)
    except json.JSONDecodeError:
        return jsonify({"error": "Could not process. Try again."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
