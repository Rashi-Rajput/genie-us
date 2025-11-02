#!/usr/bin/env python3
"""
Google Classroom Study Buddy
==================================================

This CLI tool connects to the Google Classroom and Drive APIs to:
1. Detect new lecture materials (PDFs, Google Docs).
2. Extract the text content from those materials.
3. Use the Gemini API to generate study aids (Audio Summaries, Flashcards, Quizzes).
4. Upload the generated study aids directly to your Google Drive.

Setup:
-------
1. Enable Google Classroom & Drive APIs in Google Cloud Console.
2. Download OAuth credentials.json.
3. Install dependencies:
   pip install google-auth google-auth-oauthlib google-api-python-client \
               google-generativeai python-dotenv typer rich pdfplumber gTTS
4. Create a .env file with:
   GEMINI_API_KEY=your_api_key_here
5. Run for the first time to authenticate:
   python study_buddy.py list-courses
6. Run to detect new lectures:
   python study_buddy.py detect --all-courses --since 24
"""

import os
import pickle
import re
import io
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Tuple, Optional
import typer
from rich import print
from rich.console import Console
from rich.panel import Panel
from rich.status import Status

# Google API Imports
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

# Text & AI Imports
import google.generativeai as genai
from dotenv import load_dotenv
import pdfplumber
from gtts import gTTS # <-- NEW IMPORT

# ---------------------- Setup ----------------------
app = typer.Typer(help="A CLI Study Buddy for Google Classroom, powered by Gemini AI.")
console = Console()
load_dotenv()

# --- NEW SCOPES ---
# We now need to read course materials AND read/write to Google Drive
SCOPES = [
    'https://www.googleapis.com/auth/classroom.courses.readonly',
    'https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly',
    'https://www.googleapis.com/auth/drive.readonly', # To read/download lecture files
    'https://www.googleapis.com/auth/drive.file'     # To upload generated study aids
]

# ---------------------- Core Class ----------------------
class StudyBuddyCLI:
    def __init__(self, credentials_file='credentials.json', token_file='token.pickle'):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.creds = None
        self.classroom_service = None
        self.drive_service = None
        self.gemini_model = None
        self._authenticate()
        self._setup_gemini()

    def _authenticate(self):
        """Authenticate with Google APIs (Classroom & Drive)."""
        creds = None
        if os.path.exists(self.token_file):
            with open(self.token_file, 'rb') as token:
                creds = pickle.load(token)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                console.print("[yellow]Refreshing expired credentials...[/yellow]")
                try:
                    creds.refresh(Request())
                except Exception as e:
                    console.print(f"[red]Error refreshing token: {e}[/red]")
                    console.print("[yellow]Please re-authenticate.[/yellow]")
                    creds = None # Force re-authentication
                    if os.path.exists(self.token_file):
                         os.remove(self.token_file) # Delete bad token
            
            if not creds:
                if not os.path.exists(self.credentials_file):
                    raise FileNotFoundError(
                        f"Credentials file '{self.credentials_file}' not found. "
                        "Download it from Google Cloud Console."
                    )
                console.print("[cyan]Authenticating with Google...[/cyan]")
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)

            with open(self.token_file, 'wb') as token:
                pickle.dump(creds, token)
            console.print("[green]✓ Authentication successful![/green]")

        self.creds = creds
        try:
            self.classroom_service = build('classroom', 'v1', credentials=creds)
            self.drive_service = build('drive', 'v3', credentials=creds)
            console.print("[green]✓ Classroom and Drive services initialized.[/green]")
        except HttpError as e:
            console.print(f"[red]Error building services: {e}[/red]")
            raise

    def _setup_gemini(self):
        """Setup Gemini AI."""
        api_key = os.getenv('GEMINI_API_KEY')
        if not api_key:
            raise ValueError("Missing GEMINI_API_KEY in environment variables (.env file).")
        genai.configure(api_key=api_key)
        self.gemini_model = genai.GenerativeModel('models/gemini-1.5-flash')
        console.print("[green]✓ Gemini AI configured![/green]")

    def get_courses(self):
        """Fetch available Google Classroom courses."""
        try:
            results = self.classroom_service.courses().list(
                pageSize=100, courseStates=['ACTIVE']
            ).execute()
            return results.get('courses', [])
        except HttpError as e:
            console.print(f"[red]⚠️ Error fetching courses:[/red] {e}")
            return []

    def get_new_materials(self, course_id, since_hours):
        """
        Fetch new courseWorkMaterial items (lectures, readings, etc.)
        """
        try:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=since_hours)
            
            materials = []
            page_token = None
            while True:
                response = self.classroom_service.courses().courseWorkMaterials().list(
                    courseId=course_id,
                    pageSize=20,
                    pageToken=page_token,
                    orderBy='updateTime desc'
                ).execute()
                
                items = response.get('courseWorkMaterial', [])
                
                for item in items:
                    update_time = self._parse_timestamp(item.get('updateTime'))
                    if update_time > cutoff_time:
                        materials.append(item)
                    else:
                        # Stop paging once we hit old items
                        return materials
                
                page_token = response.get('nextPageToken')
                if not page_token or not items:
                    break
                    
            return materials
        except HttpError as e:
            console.print(f"[red]⚠️ Error fetching materials for course {course_id}:[/red] {e}")
            return []

    def _parse_timestamp(self, ts: str):
        """Parses Google API timestamp to a comparable datetime object."""
        if not ts:
            return datetime.min.replace(tzinfo=timezone.utc)
        # Handle nanoseconds and 'Z'
        if '.' in ts:
            ts = ts.split('.')[0] + 'Z'
        return datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)

    def get_drive_file_text(self, drive_file) -> Optional[str]:
        """
        Downloads a Google Drive file (Doc, PDF) and extracts all text.
        """
        file_id = drive_file.get('id')
        name = drive_file.get('title', 'Unknown File')
        
        # Get file metadata to check its MIME type
        try:
            metadata = self.drive_service.files().get(fileId=file_id, fields='mimeType, name').execute()
            mime_type = metadata.get('mimeType')
            name = metadata.get('name', name)
            
            console.print(f"  > Reading file: [dim]{name}[/dim] (Type: {mime_type})")
            
            request = None
            if 'google-apps.document' in mime_type:
                # It's a Google Doc, export as plain text
                request = self.drive_service.files().export_media(
                    fileId=file_id, mimeType='text/plain'
                )
            elif 'google-apps.presentation' in mime_type:
                # It's a Google Slide, export as plain text
                request = self.drive_service.files().export_media(
                    fileId=file_id, mimeType='text/plain'
                )
            elif 'pdf' in mime_type:
                # It's a PDF, download directly
                request = self.drive_service.files().get_media(fileId=file_id)
            else:
                console.print(f"  [yellow]Skipping unsupported file type: {mime_type}[/yellow]")
                return None

            # Download the file content
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
            
            fh.seek(0)
            
            # Extract text based on original type
            if 'pdf' in mime_type:
                try:
                    with pdfplumber.open(fh) as pdf:
                        full_text = "\n".join(
                            page.extract_text() for page in pdf.pages if page.extract_text()
                        )
                        return full_text
                except Exception as e:
                    console.print(f"  [red]Error reading PDF content: {e}[/red]")
                    return None
            else:
                # It was exported as plain text
                return fh.read().decode('utf-8')

        except HttpError as e:
            console.print(f"  [red]Error accessing Drive file {name}: {e}[/red]")
            return None

    def _upload_to_drive(self, content: str, filename: str, mime_type: str = 'text/markdown') -> Optional[str]:
        """
        Uploads the generated text content to Google Drive.
        """
        file_metadata = {'name': filename}
        media = MediaIoBaseUpload(
            io.BytesIO(content.encode('utf-8')),
            mimetype=mime_type,
            resumable=True
        )
        try:
            file = self.drive_service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, webViewLink'
            ).execute()
            return file.get('webViewLink')
        except HttpError as e:
            console.print(f"[red]Error uploading {filename} to Drive: {e}[/red]")
            return None

    # --- NEW FUNCTION ---
    def _upload_audio_to_drive(self, audio_bytes_io: io.BytesIO, filename: str) -> Optional[str]:
        """
        Uploads generated audio bytes to Google Drive.
        """
        file_metadata = {'name': filename}
        audio_bytes_io.seek(0) # Rewind the in-memory file
        media = MediaIoBaseUpload(
            audio_bytes_io,
            mimetype='audio/mpeg',
            resumable=True
        )
        try:
            file = self.drive_service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, webViewLink'
            ).execute()
            return file.get('webViewLink')
        except HttpError as e:
            console.print(f"[red]Error uploading {filename} to Drive: {e}[/red]")
            return None

    # --- Gemini Generation Functions ---

    def _run_gemini_prompt(self, prompt: str, lecture_text: str) -> str:
        """Helper function to run a generation prompt with error handling."""
        full_prompt = f"{prompt}\n\nLECTURE TEXT:\n---\n{lecture_text}\n---"
        try:
            response = self.gemini_model.generate_content(full_prompt)
            return response.text.strip()
        except Exception as e:
            console.print(f"[red]Error during Gemini generation: {e}[/red]")
            return "Error: Could not generate content."

    # --- NEW FUNCTION ---
    def generate_audio_narration(self, text: str) -> str:
        prompt = """
        You are a narrator. Based on the provided lecture text, write a
        concise 2-3 minute audio script (narration only).
        This script will be used for a text-to-speech audio summary.
        Focus on the main topics, key definitions, and conclusions.
        Be clear, concise, and speak directly to the listener.
        
        IMPORTANT: Do NOT include any visual cues, headings, titles, or Markdown formatting.
        Output only the plain text of the narration.
        
        Example:
        Hello, and welcome to this lecture summary. Today we're covering three main points. First, what is...
        """
        return self._run_gemini_prompt(prompt, text)

    def generate_flashcards(self, text: str) -> str:
        prompt = """
        You are a study aid generator. Based on the provided lecture text,
        generate 15-20 flashcards in CSV format.
        Each row should be a "Question,Answer" pair.
        Do NOT include a header row.
        Ensure questions are clear and answers are concise.
        
        Example:
        "What is the capital of France?","Paris"
        "What is 2+2?","4"
        """
        return self._run_gemini_prompt(prompt, text)

    def generate_quiz(self, text: str) -> str:
        prompt = """
        You are a professor. Based on the provided lecture text, create a
        10-question multiple-choice quiz in Markdown format.
        For each question, provide 4 options (A, B, C, D).
        At the end of each question, clearly indicate the correct answer.
        
        Example:
        **1. What is the capital of France?**
        A) London
        B) Berlin
        C) Paris
        D) Madrid
        
        *Correct Answer: C*
        """
        return self._run_gemini_prompt(prompt, text)


# ---------------------- Typer Commands ----------------------

@app.command()
def list_courses(
    credentials: str = typer.Option("credentials.json", help="Path to credentials file."),
    token: str = typer.Option("token.pickle", help="Path to saved token file."),
):
    """List all your active Google Classroom courses."""
    console.print("📚 [bold]Fetching your courses...[/bold]\n")
    cli = StudyBuddyCLI(credentials_file=credentials, token_file=token)
    courses = cli.get_courses()
    if not courses:
        console.print("No active courses found.")
        raise typer.Exit()
    
    console.print(Panel(
        "\n".join([f"• [green]{c['name']}[/green] (ID: {c['id']})" for c in courses]),
        title="Active Courses",
        border_style="blue"
    ))

@app.command()
def detect(
    course_id: str = typer.Option(None, help="Specific course ID to scan."),
    all_courses: bool = typer.Option(False, "--all-courses", help="Scan all available courses."),
    since: int = typer.Option(24, help="Only scan materials from last N hours."),
    credentials: str = typer.Option("credentials.json", help="Path to credentials file"),
    token: str = typer.Option("token.pickle", help="Path to saved token file.")
):
    """
    Detect new lecture materials and generate study aids.
    """
    console.print("🚀 [bold]Initializing Study Buddy...[/bold]")
    cli = StudyBuddyCLI(credentials_file=credentials, token_file=token)
    print()

    # Determine which courses to process
    if course_id:
        try:
            course_info = cli.classroom_service.courses().get(id=course_id).execute()
            course_name = course_info.get('name', f'Course {course_id}')
            courses = [{'id': course_id, 'name': course_name}]
        except HttpError:
            console.print(f"[red]Error: Could not find course with ID {course_id}[/red]")
            raise typer.Exit()
    elif all_courses:
        courses = cli.get_courses()
        if not courses:
            console.print("[red]No active courses found.[/red]")
            raise typer.Exit()
    else:
        console.print("[yellow]❗ Please specify either --course-id or --all-courses.[/yellow]")
        raise typer.Exit()

    # --- Main Processing Loop ---
    total_materials_found = 0
    for course in courses:
        course_name = course['name']
        console.rule(f"[bold blue]🔍 Scanning: {course_name}[/bold blue]")
        
        new_materials = cli.get_new_materials(course['id'], since_hours=since)
        
        if not new_materials:
            console.print(f"[dim]No new materials found in the last {since} hours.[/dim]\n")
            continue
            
        console.print(f"📢 Found [green]{len(new_materials)}[/green] new material(s)!")
        total_materials_found += len(new_materials)
        
        for material in new_materials:
            title = material.get('title', 'Untitled Material')
            console.print(f"\n[bold]Processing Material:[/bold] [cyan]{title}[/cyan]")
            
            if not material.get('materials'):
                console.print("[dim]  > No files attached. Skipping.[/dim]")
                continue

            # --- Text Extraction ---
            full_lecture_text = ""
            for item in material.get('materials', []):
                drive_file = item.get('driveFile', {}).get('driveFile')
                if drive_file:
                    text = cli.get_drive_file_text(drive_file)
                    if text:
                        full_lecture_text += f"\n\n--- (Source: {drive_file.get('title', 'N/A')}) ---\n{text}"
            
            if not full_lecture_text.strip():
                console.print("[yellow]  > Could not extract any text from attachments. Skipping.[/yellow]")
                continue
            
            console.print(f"[green]  ✓ Extracted {len(full_lecture_text.split())} words of text.[/green]")
            
            # --- Interactive Generation ---
            if not typer.confirm(f"\n🧠 Do you want to generate study aids for '{title}'?"):
                console.print("[dim]  > Skipping generation.[/dim]")
                continue

            base_filename = f"{re.sub(r'[^\w\-_\. ]', '', title).replace(' ', '_')}"
            
            # --- MODIFIED BLOCK ---
            # Audio Summary (MP3)
            if typer.confirm("  1. Generate an Audio Summary (MP3)?"):
                with Status("[bold magenta]Generating audio summary...[/bold magenta]", console=console):
                    try:
                        # 1. Generate text narration
                        narration_text = cli.generate_audio_narration(full_lecture_text)
                        
                        # 2. Generate MP3 in memory
                        audio_fp = io.BytesIO()
                        tts = gTTS(text=narration_text, lang='en')
                        tts.write_to_fp(audio_fp)
                        
                        # 3. Upload the in-memory MP3 file
                        filename = f"Summary-{base_filename}.mp3"
                        link = cli._upload_audio_to_drive(audio_fp, filename)
                        
                        console.print(f"  [green]✓ Audio Summary generated![/green] [dim]({link})[/dim]")
                    except Exception as e:
                        console.print(f"  [red]Error generating audio: {e}[/red]")
                
            # Flashcards
            if typer.confirm("  2. Generate Flashcards?"):
                with Status("[bold magenta]Generating flashcards...[/bold magenta]", console=console):
                    content = cli.generate_flashcards(full_lecture_text)
                    filename = f"Flashcards-{base_filename}.csv"
                    link = cli._upload_to_drive(content, filename, mime_type='text/csv')
                console.print(f"  [green]✓ Flashcards generated![/green] [dim]({link})[/dim]")

            # Quiz
            if typer.confirm("  3. Generate a Quiz?"):
                with Status("[bold magenta]Generating quiz...[/I-will-not-generate-that-content]", console=console):
                    content = cli.generate_quiz(full_lecture_text)
                    filename = f"Quiz-{base_filename}.md"
                    link = cli._upload_to_drive(content, filename)
                console.print(f"  [green]✓ Quiz generated![/green] [dim]({link})[/dim]")

            console.print(f"\n[bold green]✅ Done processing '{title}'![/bold green]")
            
    console.rule("[bold]Detection Complete[/bold]")
    console.print(f"✅ Scanned [bold]{len(courses)}[/bold] course(s).")
    console.print(f"🎯 Found [bold]{total_materials_found}[/bold] new materials.")
    console.print()


# ---------------------- Main ----------------------
if __name__ == "__main__":
    app()
