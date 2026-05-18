from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file, jsonify
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import sqlite3
import os
import subprocess
import requests
import speech_recognition as sr
from pydub import AudioSegment
from langdetect import detect
import tempfile

from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.summarizers.lex_rank import LexRankSummarizer
from fpdf import FPDF
from docx import Document
import io
import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import random
import json
import asyncio
from deep_translator import GoogleTranslator
import openai
from dotenv import load_dotenv
import tempfile
import base64

load_dotenv()

# Configure FFmpeg path for pydub
ffmpeg_dir = r"C:\Users\Admin\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin"
ffmpeg_path = os.path.join(ffmpeg_dir, "ffmpeg.exe")
ffprobe_path = os.path.join(ffmpeg_dir, "ffprobe.exe")

if os.path.isdir(ffmpeg_dir) and os.path.exists(ffmpeg_path) and os.path.exists(ffprobe_path):
    os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
    os.environ["FFMPEG_BINARY"] = ffmpeg_path
    os.environ["FFPROBE_BINARY"] = ffprobe_path
    AudioSegment.converter = ffmpeg_path
    AudioSegment.ffprobe = ffprobe_path
    print(f"FFmpeg configured successfully: {ffmpeg_path}")
    print(f"FFprobe configured successfully: {ffprobe_path}")
else:
    print(f"Warning: FFmpeg binary not found in {ffmpeg_dir}")
    print(f"Expected ffmpeg.exe at: {ffmpeg_path}")
    print(f"Expected ffprobe.exe at: {ffprobe_path}")

class User(UserMixin):
    def __init__(self, id, username, password):
        self.id = id
        self.username = username
        self.password = password

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key_here_change_in_production'
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['ALLOWED_EXTENSIONS'] = {'mp3', 'wav', 'm4a', 'aac', 'flac', 'ogg', 'wma', 'aiff', 'webm'}
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024  # 25 MB max upload size

login_manager = LoginManager()
login_manager.init_app(app)

@app.errorhandler(413)
def request_entity_too_large(error):
    return "Recorded audio is too large. Please record a shorter clip or use a smaller file.", 413

@login_manager.user_loader
def load_user(user_id):
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE id = ?', (user_id,))
    user = c.fetchone()
    conn.close()
    if user:
        return User(user[0], user[1], user[2])
    return None

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def create_tables():
    conn = sqlite3.connect('database.db')
    conn.execute('PRAGMA encoding="UTF-8"')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS uploads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        filename TEXT,
        transcription TEXT,
        notes TEXT,
        note_type TEXT,
        language TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS reset_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL,
        token TEXT NOT NULL,
        expires DATETIME NOT NULL
    )''')
    conn.commit()
    conn.close()

create_tables()

def send_reset_email(email, code):
    # Configure your email settings here
    sender_email = "surojsnehitha5@gmail.com"  # Replace with your email
    sender_password = "12345"       # Replace with your app password
    receiver_email = email

    message = MIMEMultipart("alternative")
    message["Subject"] = "Password Reset Code - Notexa"
    message["From"] = sender_email
    message["To"] = receiver_email

    text = f"Your password reset code is: {code}\n\nThis code will expire in 1 hour."
    part = MIMEText(text, "plain")
    message.attach(part)

    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, receiver_email, message.as_string())
        server.quit()
        print("Reset email sent successfully")
    except Exception as e:
        print(f"Error sending email: {e}")
        # For demo purposes, print the code
        print(f"Demo: Reset code for {email}: {code}")

def check_audio_quality(file_path):
    """Check basic audio quality metrics before transcription"""
    try:
        # Load audio file
        audio = AudioSegment.from_file(file_path)

        # Check duration
        duration_seconds = len(audio) / 1000.0
        if duration_seconds < 1:
            return "Audio file is too short (less than 1 second). Please provide longer audio."

        if duration_seconds > 300:  # 5 minutes
            return "Audio file is too long (over 5 minutes). Please provide shorter audio or split into smaller segments."

        # Check sample rate
        if audio.frame_rate < 8000:
            return "Audio quality is too low (sample rate below 8kHz). Please use higher quality audio."

        # Check channels (prefer mono for better recognition)
        if audio.channels > 1:
            print("Warning: Multi-channel audio detected. Converting to mono for better recognition.")

        # Check if audio has content (not just silence)
        samples = audio.get_array_of_samples()
        max_amplitude = max(abs(sample) for sample in samples[:10000])  # Check first 10k samples
        if max_amplitude < 100:  # Very quiet
            return "Audio appears to be silent or extremely quiet. Please check your microphone and try recording again."

        return None  # No issues found

    except Exception as e:
        return f"Unable to analyze audio file: {str(e)}. The file may be corrupted or in an unsupported format."

def translate_text(text, dest_language, src_language=None):
    if not text or not dest_language:
        return text
    if src_language == dest_language:
        return text

    language_aliases = {
        'zh-cn': 'zh-CN',
        'zh-tw': 'zh-TW',
        'pt-br': 'pt-BR',
        'pt': 'pt',
        'en': 'en',
        'es': 'es',
        'fr': 'fr',
        'de': 'de',
        'it': 'it',
        'ru': 'ru',
        'ja': 'ja',
        'ko': 'ko',
        'ar': 'ar',
        'hi': 'hi'
    }
    dest = language_aliases.get(dest_language.lower(), dest_language)
    src = language_aliases.get(src_language.lower(), src_language) if src_language else 'auto'

    try:
        translator = GoogleTranslator(source=src, target=dest)
        translation = translator.translate(text)
        print(f"Translation to {dest}: {translation}")
        return translation
    except Exception as e:
        print(f"Translation error to {dest}: {e}")
        return text
def transcribe_audio(file_path):
    """Transcribe audio file by converting to WAV using ffmpeg directly and auto-detect language"""

    # First, check audio quality
    quality_check = check_audio_quality(file_path)
    if quality_check:
        return quality_check
    r = sr.Recognizer()
    r.energy_threshold = 4000  # Lower threshold for better sensitivity
    r.dynamic_energy_threshold = True

    if not os.path.exists(file_path):
        return "Error: Uploaded audio file could not be found."

    # Map language codes to Google Speech Recognition format
    language_map = {
        'en': 'en-US',
        'es': 'es-ES',
        'fr': 'fr-FR',
        'de': 'de-DE',
        'it': 'it-IT',
        'pt': 'pt-BR',
        'ru': 'ru-RU',
        'ja': 'ja-JP',
        'ko': 'ko-KR',
        'zh-cn': 'zh-CN',
        'ar': 'ar-SA',
        'hi': 'hi-IN'
    }

    # Get file extension
    file_ext = os.path.splitext(file_path)[1].lower().lstrip('.')

    # Convert non-WAV files to WAV using ffmpeg directly
    wav_path = file_path
    if file_ext != 'wav':
        try:
            # Create a temporary WAV file
            wav_file = tempfile.NamedTemporaryFile(delete=False, suffix='.wav')
            wav_path = wav_file.name
            wav_file.close()

            # Use ffmpeg to convert to WAV
            cmd = [ffmpeg_path, '-i', file_path, '-acodec', 'pcm_s16le', '-ac', '1', '-ar', '16000', wav_path, '-y']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode != 0:
                return f"Error: FFmpeg conversion failed. {result.stderr}"

        except subprocess.TimeoutExpired:
            return "Error: Audio conversion timed out."
        except FileNotFoundError:
            return "Error: FFmpeg not found. Unable to convert audio format."
        except Exception as e:
            return f"Error converting audio: {str(e)}"

    # Transcribe the WAV file
    try:
        with sr.AudioFile(wav_path) as source:
            # Record the entire audio with better handling
            audio = r.record(source)
            
            # Verify audio has content
            if not audio.get_raw_data():
                if wav_path != file_path and os.path.exists(wav_path):
                    os.remove(wav_path)
                return "Error: Audio file is empty or corrupted. Please provide valid audio content."

        # Try to transcribe with multiple languages, starting with English, then others
        text = None
        detected_lang = 'en'
        tried_languages = ['en-US', 'ja-JP', 'ko-KR', 'zh-CN', 'es-ES', 'fr-FR', 'de-DE', 'it-IT', 'pt-BR', 'ru-RU', 'ar-SA', 'hi-IN']
        
        for lang in tried_languages:
            try:
                print(f"Attempting speech recognition with {lang}")
                text = r.recognize_google(audio, language=lang)
                if text:
                    # Map back to language code
                    lang_code = lang.split('-')[0]
                    if lang == 'zh-CN':
                        lang_code = 'zh-cn'
                    elif lang == 'pt-BR':
                        lang_code = 'pt'
                    detected_lang = lang_code
                    print(f"Successfully transcribed with {lang}: {text}")
                    break
            except sr.UnknownValueError:
                continue  # Try next language
            except sr.RequestError as e:
                print(f"Request error with {lang}: {e}")
                continue
        
        if not text:
            if wav_path != file_path and os.path.exists(wav_path):
                try:
                    os.remove(wav_path)
                except:
                    pass
            return """Audio transcription failed. This usually happens when:

• The audio quality is too low or has excessive background noise
• The audio is too quiet or the speaker is too far from the microphone
• The language setting doesn't match the spoken language
• The audio file is corrupted or in an unsupported format

Suggestions:
• Try recording in a quiet environment
• Speak clearly and closer to the microphone
• Ensure the correct language is selected
• Use a higher quality audio file (WAV preferred)
• Check that your audio file isn't empty or corrupted

If the problem persists, try uploading a different audio file."""

        # Translate transcription to English if not already
        if detected_lang != 'en':
            print(f"Translating from {detected_lang} to English")
            text = translate_text(text, 'en', src_language=detected_lang)



        # Clean up temporary file if created
        if wav_path != file_path and os.path.exists(wav_path):
            try:
                os.remove(wav_path)
            except:
                pass

        return text

    except sr.UnknownValueError:
        if wav_path != file_path and os.path.exists(wav_path):
            try:
                os.remove(wav_path)
            except:
                pass
        return """Audio transcription failed. This usually happens when:

• The audio quality is too low or has excessive background noise
• The audio is too quiet or the speaker is too far from the microphone
• The language setting doesn't match the spoken language
• The audio file is corrupted or in an unsupported format

Suggestions:
• Try recording in a quiet environment
• Speak clearly and closer to the microphone
• Ensure the correct language is selected
• Use a higher quality audio file (WAV preferred)
• Check that your audio file isn't empty or corrupted

If the problem persists, try uploading a different audio file."""
    except sr.RequestError as e:
        if wav_path != file_path and os.path.exists(wav_path):
            try:
                os.remove(wav_path)
            except:
                pass
        return f"Speech API error: {str(e)}"
    except Exception as e:
        if wav_path != file_path and os.path.exists(wav_path):
            try:
                os.remove(wav_path)
            except:
                pass
        return f"Error processing audio: {str(e)}"

def generate_notes(text, note_type, language='en'):
    try:
        notes_text = text

        # Create a detailed prompt for Gemini
        prompt = f"""Convert the following text into structured notes.

Text: {notes_text}

Return ONLY the formatted notes, nothing else."""

        # Try Gemini API first
        gemini_api_key = os.getenv('GEMINI_API_KEY', 'AIzaSyC_PH_FCww-8utgWklein58IZMsPB_DV4o')
        if gemini_api_key:
            try:
                response = requests.post(
                    f'https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={gemini_api_key}',
                    json={
                        "contents": [{
                            "parts": [{"text": prompt}]
                        }],
                        "generationConfig": {
                            "maxOutputTokens": 1024,
                            "temperature": 0.7
                        }
                    },
                    timeout=30
                )
                if response.status_code == 200:
                    result = response.json()
                    if 'candidates' in result and len(result['candidates']) > 0:
                        try:
                            notes = result['candidates'][0]['content']['parts'][0]['text']
                            print(f"Gemini raw notes: {notes}")
                            # Always translate to target language, including script conversion
                            translated_notes = translate_text(notes, language, src_language='en')
                            print(f"Gemini translated notes: {translated_notes}")
                            return translated_notes
                        except (KeyError, IndexError):
                            pass
            except Exception as e:
                print(f"Gemini API error: {e}")

        return fallback_notes(notes_text, note_type, language)

    except Exception as e:
        print(f"Critical error in generate_notes: {e}")
        return fallback_notes(text, note_type, language)

def fallback_notes(text, note_type, language='en'):
    if note_type == 'bullet points':
        sentences = text.split('.')
        notes = '\n'.join([f"- {sentence.strip()}" for sentence in sentences if sentence.strip()])
    elif note_type == 'detailed notes':
        notes = text  # Just the text
    elif note_type == 'explanation':
        notes = f"Explanation: {text}"
    elif note_type == 'summarized notes':
        parser = PlaintextParser.from_string(text, Tokenizer("english"))
        summarizer = LexRankSummarizer()
        summary = summarizer(parser.document, 3)
        notes = ' '.join([str(sentence) for sentence in summary])
    elif note_type == 'mind maps':
        notes = f"Mind Map:\n- Central Idea: {text[:50]}...\n- Branches: {', '.join(text.split()[:10])}"
    elif note_type == 'flashcards':
        sentences = text.split('.')
        notes = '\n\n'.join([f"Front: {sentence.strip()}\nBack: (Answer here)" for sentence in sentences[:5] if sentence.strip()])
    elif note_type == 'cornell notes':
        notes = f"Cues:\n\nNotes:\n{text}\n\nSummary:\n(Summarize here)"
    elif note_type == 'keyword/highlight notes':
        words = text.split()
        keywords = [word for word in words if len(word) > 5][:10]
        notes = f"Keywords: {', '.join(keywords)}\n\nHighlighted Text: {text}"
    elif note_type == 'question answer':
        notes = f"Question: What is the main topic?\nAnswer: {text[:100]}..."
    else:
        notes = text

    # Always translate to target language, ensuring proper script representation
    if language and language.lower() != 'en':
        print(f"Fallback notes before translation: {notes}")
        translated = translate_text(notes, language, src_language='en')
        print(f"Fallback notes after translation: {translated}")
        return translated

    return notes

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        hashed_password = generate_password_hash(password)
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        try:
            c.execute('INSERT INTO users (username, email, password) VALUES (?, ?, ?)', (username, email, hashed_password))
            conn.commit()
            flash('Registration successful! Please log in.')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Username or email already exists.')
        conn.close()
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        c.execute('SELECT id, username, password FROM users WHERE username = ?', (username,))
        user = c.fetchone()
        conn.close()
        if user and check_password_hash(user[2], password):
            user_obj = User(user[0], user[1], user[2])
            login_user(user_obj)
            return redirect(url_for('dashboard'))
        flash('Invalid credentials.')
    return render_template('login.html')

@app.route('/forgot', methods=['GET', 'POST'])
def forgot():
    if request.method == 'POST':
        email = request.form['email']
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        c.execute('SELECT id FROM users WHERE email = ?', (email,))
        user = c.fetchone()
        if user:
            code = str(random.randint(100000, 999999))
            expires = datetime.datetime.now() + datetime.timedelta(hours=1)
            c.execute('INSERT INTO reset_tokens (email, token, expires) VALUES (?, ?, ?)', (email, code, expires))
            conn.commit()
            send_reset_email(email, code)
            flash('Reset code sent to your email.')
            return redirect(url_for('reset'))
        else:
            flash('Email not found.')
        conn.close()
    return render_template('forgot.html')

@app.route('/reset', methods=['GET', 'POST'])
def reset():
    if request.method == 'POST':
        code = request.form['code']
        password = request.form['password']
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        c.execute('SELECT email FROM reset_tokens WHERE token = ? AND expires > ?', (code, datetime.datetime.now()))
        row = c.fetchone()
        if row:
            email = row[0]
            hashed = generate_password_hash(password)
            c.execute('UPDATE users SET password = ? WHERE email = ?', (hashed, email))
            c.execute('DELETE FROM reset_tokens WHERE token = ?', (code,))
            conn.commit()
            flash('Password reset successful. Please login.')
            return redirect(url_for('login'))
        else:
            flash('Invalid or expired code.')
        conn.close()
    return render_template('reset.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/search', methods=['GET', 'POST'])
def search():
    query = ''
    answer = None
    error = None

    if request.method == 'POST':
        query = request.form.get('query', '').strip()
        if not query:
            error = 'Please enter a question before searching.'
        else:
            try:
                # Since Gemini API is not set up, use a placeholder
                answer = f"Simulated response for: {query}"
            except Exception as e:
                error = str(e)

    return render_template('search.html', query=query, answer=answer, error=error)

@app.route('/dashboard')
@login_required
def dashboard():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('SELECT id, filename, transcription, notes, note_type, language, created_at FROM uploads WHERE user_id = ?', (current_user.id,))
    uploads = c.fetchall()
    conn.close()
    return render_template('dashboard.html', uploads=uploads)

def process_uploaded_audio(file_path, filename, note_type, output_languages):
    raw_transcription = transcribe_audio(file_path)

    error_indicators = ["Error:", "Could not", "Speech API", "Audio transcription failed", "Unable to analyze", "too short", "too long", "too low", "silent"]
    if any(indicator in raw_transcription for indicator in error_indicators):
        return None, raw_transcription

    # Transcription is now in English, translate to output languages if needed
    transcriptions = {}
    for lang in output_languages:
        if lang.lower() == 'en':
            transcriptions[lang] = raw_transcription
        else:
            transcriptions[lang] = translate_text(raw_transcription, lang, src_language='en')

    # Generate notes in English
    notes = generate_notes(raw_transcription, note_type, 'en')
    
    # Translate notes to each output language
    notes_dict = {}
    for lang in output_languages:
        if lang.lower() == 'en':
            notes_dict[lang] = notes
        else:
            notes_dict[lang] = translate_text(notes, lang, src_language='en')
    
    # Store as JSON
    import json
    transcription_json = json.dumps(transcriptions)
    notes_json = json.dumps(notes_dict)
    
    # Use the first language for the language field
    primary_language = output_languages[0]
    
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('INSERT INTO uploads (user_id, filename, transcription, notes, note_type, language) VALUES (?, ?, ?, ?, ?, ?)',
              (current_user.id, filename, transcription_json, notes_json, note_type, primary_language))
    conn.commit()
    upload_id = c.lastrowid
    conn.close()
    return upload_id, None

@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    if request.method == 'POST':
        if not os.path.exists(app.config['UPLOAD_FOLDER']):
            os.makedirs(app.config['UPLOAD_FOLDER'])

        note_type = request.form.get('note_type')
        output_languages = request.form.getlist('output_language')
        if not output_languages:
            output_languages = ['en']
        file = request.files.get('file')
        audio_data = request.form.get('audio_data')

        if not file and not audio_data:
            flash('Please select a file to upload or record audio.')
            return redirect(url_for('upload'))

        try:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_")

            if audio_data and not file:
                decoded = base64.b64decode(audio_data.split(',')[1])
                filename = timestamp + 'live_recording.webm'
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                with open(file_path, 'wb') as f:
                    f.write(decoded)
            else:
                filename = secure_filename(file.filename)
                if not allowed_file(filename):
                    flash('File type not allowed. Supported: MP3, WAV, M4A, AAC, FLAC, OGG, WMA, AIFF, WebM')
                    return redirect(url_for('upload'))
                filename = timestamp + filename
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(file_path)

            flash('Audio received successfully. Processing...')
            upload_id, error = process_uploaded_audio(file_path, filename, note_type, output_languages)

            if error:
                flash(f'Transcription error: {error}')
                return redirect(url_for('upload'))

            flash('Audio processed successfully!')
            return redirect(url_for('result', upload_id=upload_id))
        except Exception as e:
            flash(f'Error processing audio: {str(e)}')
            return redirect(url_for('upload'))

    return render_template('upload.html')

@app.route('/live_record')
@login_required
def live_record():
    return render_template('live_record.html')

@app.route('/result/<int:upload_id>')
@login_required
def result(upload_id):
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('SELECT filename, transcription, notes, note_type, language FROM uploads WHERE id = ? AND user_id = ?', (upload_id, current_user.id))
    upload = c.fetchone()
    conn.close()
    if not upload:
        return "Not found", 404
    
    import json
    try:
        transcriptions = json.loads(upload[1])
        notes = json.loads(upload[2])
    except:
        # Fallback for old format
        transcriptions = {'en': upload[1]}
        notes = {'en': upload[2]}
    
    return render_template('result.html', upload=upload, upload_id=upload_id, transcriptions=transcriptions, notes=notes)

@app.route('/download/<int:upload_id>/<format>')
@login_required
def download(upload_id, format):
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('SELECT notes, filename FROM uploads WHERE id = ? AND user_id = ?', (upload_id, current_user.id))
    upload = c.fetchone()
    conn.close()
    if not upload:
        return "Not found", 404
    notes, filename = upload
    if format == 'pdf':
        try:
            pdf = FPDF()
            pdf.add_page()
            pdf.set_font("helvetica", size=15)
            # Handle potential encoding issues
            safe_notes = notes.encode('latin-1', 'replace').decode('latin-1')
            pdf.multi_cell(0, 10, safe_notes)
            output = io.BytesIO()
            pdf.output(output)
            output.seek(0)
            return send_file(output, as_attachment=True, download_name=f"{filename}.pdf", mimetype='application/pdf')
        except Exception as e:
            flash(f'Error generating PDF: {str(e)}')
            return redirect(url_for('result', upload_id=upload_id))
    elif format == 'docx':
        try:
            doc = Document()
            doc.add_paragraph(notes)
            output = io.BytesIO()
            doc.save(output)
            output.seek(0)
            return send_file(output, as_attachment=True, download_name=f"{filename}.docx", mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        except Exception as e:
            flash(f'Error generating Word document: {str(e)}')
            return redirect(url_for('result', upload_id=upload_id))

@app.route('/download_audio/<int:upload_id>')
@login_required
def download_audio(upload_id):
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('SELECT filename FROM uploads WHERE id = ? AND user_id = ?', (upload_id, current_user.id))
    upload = c.fetchone()
    conn.close()
    if not upload:
        return "Not found", 404
    filename = upload[0]
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    # Detect file type and set proper MIME type
    file_ext = filename.lower().split('.')[-1] if '.' in filename else 'wav'
    mime_types = {
        'mp3': 'audio/mpeg',
        'wav': 'audio/wav',
        'm4a': 'audio/mp4',
        'aac': 'audio/aac',
        'flac': 'audio/flac',
        'ogg': 'audio/ogg',
        'wma': 'audio/x-ms-wma',
        'aiff': 'audio/aiff',
        'webm': 'audio/webm'
    }
    mime_type = mime_types.get(file_ext, 'audio/mpeg')
    
    # Send with proper MIME type and original filename
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True, download_name=filename, mimetype=mime_type)
    return "File not found", 404

if __name__ == '__main__':
    app.run(debug=True, port=8000)
