#!/usr/bin/env python3
"""
Google Classroom Assignment Helper
==================================================

DISCLAIMER: This tool is for educational purposes to demonstrate
automation and API integration. It is not intended to bypass
academic integrity.

This CLI tool:
1. Detects new, unsubmitted assignments.
2. Reads the assignment text and any attached files (Docs, PDFs).
3. Uses the Gemini API to generate a "Solution Draft" with code,
   output, and explanations.
4. Creates a new Google Doc with this draft.
5. Attaches the new Doc to the assignment submission.
6. Asks the user for confirmation before turning in the assignment.

Setup:
-------
1. Enable Classroom, Drive, Docs, and Generative Language APIs
   in Google Cloud Console.
2. Download OAuth credentials.json.
3. Install dependencies:
   pip install google-auth google-auth-oauthlib google-api-python-client \
               google-generativeai python-dotenv typer rich pdfplumber
4. Create a .env file with:
   GEMINI_API_KEY=your_api_key_here
5. Run:
   python assignment_helper.py detect --all-courses --since 48
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
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# Text & AI Imports
import google.generativeai as genai
from dotenv import load_dotenv
import pdfplumber

# ---------------------- Setup ----------------------
app = typer.Typer(help="A CLI tool to assist with Google Classroom assignments using Gemini AI.")
console = Console()
load_dotenv()

# --- CRITICAL: New Scopes ---
# This tool needs extensive permissions to read, create, and submit.
SCOPES = [
    'https://www.googleapis.com/auth/classroom.courses.readonly',
    'https://www.googleapis.com/auth/classroom.coursework.me',
    'https://www.googleapis.com/auth/classroom.student-submissions.me',
    'https://www.googleapis.com/auth/drive.file', # Full drive access to create/upload
    'https://www.googleapis.com/auth/drive.readonly' # To read assignment attachments
]

# ---------------------- Core Class ----------------------
class StudyAutomatorCLI:
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
                    creds = None
                    if os.path.exists(self.token_file):
                         os.remove(self.token_file)
            
            if not creds:
                if not os.path.exists(self.credentials_file):
                    raise FileNotFoundError(
                        f"Credentials file '{self.credentials_file}' not found."
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
            raise ValueError("Missing GEMINI_API_KEY in .env file.")
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

    def get_new_assignments(self, course_id, since_hours) -> List[Tuple[Dict, Dict]]:
        """
        Fetch new assignments that have not been turned in.
        Returns a list of (assignment, submission) tuples.
        """
        try:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=since_hours)
            
            assignments_to_do = []
            page_token = None
            
            while True:
                response = self.classroom_service.courses().courseWork().list(
                    courseId=course_id,
                    courseWorkStates=['PUBLISHED'],
                    pageSize=10,
                    pageToken=page_token,
                    orderBy='updateTime desc'
                ).execute()
                
                items = response.get('courseWork', [])
                
                for work in items:
                    # Skip if it's not an assignment
                    if 'assignment' not in work:
                        continue

                    update_time = self._parse_timestamp(work.get('updateTime'))
                    if update_time < cutoff_time:
                        # We've gone past the time window
                        return assignments_to_do
                    
                    # Check submission status
                    submission = self.classroom_service.courses().courseWork().studentSubmissions().get(
                        courseId=course_id,
                        courseWorkId=work['id'],
                        id='me' # 'me' is a special ID for the current user
                    ).execute()
                    
                    # We only care about new or reclaimed assignments
                    if submission['state'] in ['CREATED', 'RECLAIMED']:
                        assignments_to_do.append((work, submission))
                
                page_token = response.get('nextPageToken')
                if not page_token or not items:
                    break
                    
            return assignments_to_do
        except HttpError as e:
            console.print(f"[red]⚠️ Error fetching assignments for course {course_id}:[/red] {e}")
            return []

    def _parse_timestamp(self, ts: str):
        """Parses Google API timestamp to a comparable datetime object."""
        if not ts:
            return datetime.min.replace(tzinfo=timezone.utc)
        if '.' in ts:
            ts = ts.split('.')[0] + 'Z'
        return datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)

    def get_drive_file_text(self, drive_file) -> Optional[str]:
        """Downloads a Google Drive file (Doc, PDF) and extracts all text."""
        file_id = drive_file.get('id')
        name = drive_file.get('title', 'Unknown File')
        
        try:
            metadata = self.drive_service.files().get(fileId=file_id, fields='mimeType, name').execute()
            mime_type = metadata.get('mimeType')
            name = metadata.get('name', name)
            
            console.print(f"  > Reading file: [dim]{name}[/dim] (Type: {mime_type})")
            
            request = None
            if 'google-apps.document' in mime_type:
                request = self.drive_service.files().export_media(
                    fileId=file_id, mimeType='text/plain'
                )
            elif 'google-apps.presentation' in mime_type:
                request = self.drive_service.files().export_media(
                    fileId=file_id, mimeType='text/plain'
                )
            elif 'pdf' in mime_type:
                request = self.drive_service.files().get_media(fileId=file_id)
            else:
                console.print(f"  [yellow]Skipping unsupported file type: {mime_type}[/yellow]")
                return None

            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
            fh.seek(0)
            
            if 'pdf' in mime_type:
                try:
                    with pdfplumber.open(fh) as pdf:
                        return "\n".join(
                            page.extract_text() for page in pdf.pages if page.extract_text()
                        )
                except Exception as e:
                    console.print(f"  [red]Error reading PDF content: {e}[/red]")
                    return None
            else:
                return fh.read().decode('utf-8')

        except HttpError as e:
            console.print(f"  [red]Error accessing Drive file {name}: {e}[/red]")
            return None

    def extract_assignment_text(self, assignment: Dict) -> str:
        """Extracts all text from assignment title, description, and attachments."""
        console.print("  > Extracting text from assignment...")
        full_text = []
        
        if 'title' in assignment:
            full_text.append(f"Title: {assignment['title']}")
        if 'description' in assignment:
            full_text.append(f"Description: {assignment['description']}")

        for item in assignment.get('materials', []):
            drive_file = item.get('driveFile', {}).get('driveFile')
            if drive_file:
                text = self.get_drive_file_text(drive_file)
                if text:
                    full_text.append(f"\n--- Attachment: {drive_file.get('title', 'N/A')} ---\n{text}")
        
        return "\n".join(full_text)

    def generate_solution_draft(self, assignment_text: str) -> str:
        """Generates a suggested solution draft using Gemini."""
        
        # This prompt is crucial. It asks for the user's format but
        # frames it as an educational tool, not a cheating tool.
        prompt = f"""
        You are a teaching assistant. Your goal is to help a student
        understand *how* to approach an assignment, not to do it for them.
        The student has provided their assignment questions.
        For each question, provide a suggested solution in this format:

        **1. Question:** [Briefly restate or number the question]
        **2. Suggested Code:** [Provide well-commented Python, Java, or C++ code]
        **3. Example Output:** [Show what output the code would produce]
        **4. Explanation:** [CRITICAL: Briefly explain the *logic* and *why* this 
           approach was taken, mentioning key concepts or alternatives.]

        IMPORTANT: Start the entire response with this disclaimer block:
        
        > **DISCLAIMER: This is an AI-generated solution draft.**
        > It is for study and review purposes *only*.
        > **You must verify this solution.**
        > Always write and submit your own original code.

        ASSIGNMENT TEXT:
        ---
        {assignment_text}
        ---
        """
        
        try:
            response = self.gemini_model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            console.print(f"[red]Error during Gemini generation: {e}[/red]")
            return "Error: Could not generate content."

    def create_solution_doc(self, content: str, title: str) -> Optional[str]:
        """
        Creates a new Google Doc with the solution content.
        Returns the new file's ID.
        """
        console.print("  > Creating new Google Doc in your Drive...")
        # We must upload a local file and have Drive convert it.
        temp_filename = "temp_solution_draft.txt"
        doc_title = f"Solution Draft - {title}"
        
        try:
            with open(temp_filename, "w", encoding="utf-8") as f:
                f.write(content)
            
            file_metadata = {
                'name': doc_title,
                'mimeType': 'application/vnd.google-apps.document'
            }
            media = MediaFileUpload(temp_filename, mimetype='text/plain')
            
            file = self.drive_service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, webViewLink'
            ).execute()
            
            console.print(f"  > Doc created: [green]{doc_title}[/green] ({file.get('webViewLink')})")
            return file.get('id')

        except HttpError as e:
            console.print(f"[red]Error creating Google Doc: {e}[/red]")
            return None
        finally:
            if os.path.exists(temp_filename):
                os.remove(temp_filename)

    def attach_and_turn_in(self, course_id, work_id, submission_id, doc_id):
        """Attaches the doc and CONFIRMS with user before turning in."""
        try:
            # Step 1: Attach the new Google Doc
            console.print(f"  > Attaching Doc (ID: {doc_id}) to assignment...")
            add_attachments_request = {
                'addAttachments': [
                    {'driveFile': {'id': doc_id}}
                ]
            }
            self.classroom_service.courses().courseWork().studentSubmissions().modifyAttachments(
                courseId=course_id,
                courseWorkId=work_id,
                id=submission_id,
                body=add_attachments_request
            ).execute()
            console.print("  > [green]Draft successfully attached.[/green]")

            # Step 2: Critical safety check - ask user to confirm
            if typer.confirm("\n[bold yellow]Do you want to turn in this assignment now?[/bold yellow]", default=False):
                # Step 3: Turn in
                console.print("  > Turning in assignment...")
                self.classroom_service.courses().courseWork().studentSubmissions().turnIn(
                    courseId=course_id,
                    courseWorkId=work_id,
                    id=submission_id,
                    body={}
                ).execute()
                console.print("\n[bold green]✅ Assignment Turned In![/bold green]")
            else:
                # Step 4: User opted out
                console.print(
                    "\n[bold cyan]✅ Draft attached. Assignment is NOT turned in.[/bold cyan]"
                )
                console.print(
                    "  > Please review the Google Doc, make your edits, and submit manually."
                )

        except HttpError as e:
            console.print(f"[red]Error during submission: {e}[/red]")


# ---------------------- Typer Commands ----------------------

@app.command()
def list_courses(
    credentials: str = typer.Option("credentials.json", help="Path to credentials file."),
    token: str = typer.Option("token.pickle", help="Path to saved token file."),
):
    """List all your active Google Classroom courses."""
    console.print("📚 [bold]Fetching your courses...[/bold]\n")
    cli = StudyAutomatorCLI(credentials_file=credentials, token_file=token)
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
    since: int = typer.Option(24, help="Only scan assignments from last N hours."),
    credentials: str = typer.Option("credentials.json", help="Path to credentials file."),
    token: str = typer.Option("token.pickle", help="Path to saved token file.")
):
    """
    Detect new assignments, generate solution drafts, and attach them.
    """
    console.print(Panel(
        "[bold]Classroom Assignment Helper[/bold]\n\n"
        "This tool is for educational purposes to demonstrate API automation. "
        "Generated content is a DRAFT and requires your review.",
        title="⚠️ DISCLAIMER ⚠️",
        border_style="red"
    ))
    
    cli = StudyAutomatorCLI(credentials_file=credentials, token_file=token)
    print()

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
    total_assignments_found = 0
    for course in courses:
        console.rule(f"[bold blue]🔍 Scanning: {course['name']}[/bold blue]")
        
        with Status("Checking for new assignments...", console=console):
            new_assignments = cli.get_new_assignments(course['id'], since_hours=since)
        
        if not new_assignments:
            console.print(f"[dim]No new assignments found in the last {since} hours.[/dim]\n")
            continue
            
        console.print(f"📢 Found [green]{len(new_assignments)}[/green] new assignment(s)!")
        total_assignments_found += len(new_assignments)
        
        for (assignment, submission) in new_assignments:
            title = assignment.get('title', 'Untitled Assignment')
            console.print(f"\n[bold]Processing Assignment:[/bold] [cyan]{title}[/cyan]")
            
            try:
                # 1. Extract Text
                with Status("Extracting assignment text...", console=console):
                    assignment_text = cli.extract_assignment_text(assignment)
                if not assignment_text.strip():
                    console.print("[yellow]  > Assignment has no text. Skipping.[/yellow]")
                    continue
                console.print(f"  ✓ Extracted {len(assignment_text.split())} words.")

                # 2. Generate Solution
                with Status("Generating solution draft with Gemini...", console=console):
                    solution_draft = cli.generate_solution_draft(assignment_text)
                console.print("  ✓ Solution draft generated.")

                # 3. Create Google Doc
                with Status("Creating Google Doc in your Drive...", console=console):
                    doc_id = cli.create_solution_doc(solution_draft, title)
                if not doc_id:
                    console.print("[red]  > Failed to create Google Doc. Skipping.[/red]")
                    continue
                console.print("  ✓ Google Doc created.")

                # 4. Attach and Turn In (with confirmation)
                cli.attach_and_turn_in(
                    course_id=course['id'],
                    work_id=assignment['id'],
                    submission_id=submission['id'],
                    doc_id=doc_id
                )

            except Exception as e:
                console.print(f"[red]An unexpected error occurred processing '{title}': {e}[/red]")
            
    console.rule("[bold]Automation Complete[/bold]")
    console.print(f"✅ Scanned [bold]{len(courses)}[/bold] course(s).")
    console.print(f"🎯 Found and processed [bold]{total_assignments_found}[/bold] new assignment(s).")
    console.print()


# ---------------------- Main ----------------------
if __name__ == "__main__":
    app()
