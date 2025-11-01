#!/usr/bin/env python3
"""
Google Classroom Assignment Solver and Submitter (Typer Version)
================================================================

This CLI extends the announcement monitor to detect assignments, solve them using Gemini AI,
generate a solution PDF with code and outputs, and submit it to Google Classroom.

New Features:
-------------
• Detects assignments from announcements.
• Uses Gemini to generate code solutions for programming assignments.
• Generates PDF with code and simulated output screenshots.
• Submits the PDF as a student submission.

Setup:
-------
1. Enable Google Classroom, Drive APIs in Google Cloud Console.
2. Add scopes for student submissions and Drive.
3. Download OAuth credentials.json.
4. Install dependencies:
   pip install google-auth google-auth-oauthlib google-api-python-client google-generativeai python-dotenv typer rich reportlab
5. Create a .env file with:
   GEMINI_API_KEY=your_api_key_here
6. Run commands like:
   python classroom_tool.py solve-and-submit --course-id YOUR_COURSE_ID --assignment-text "assignment description here"
"""

import os
import pickle
from datetime import datetime, timedelta
import typer
from rich import print
from rich.console import Console

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

import google.generativeai as genai
from dotenv import load_dotenv
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

# ---------------------- Setup ----------------------
app = typer.Typer(help="Monitor, solve, and submit Google Classroom assignments with Gemini AI.")
console = Console()
load_dotenv()

SCOPES = [
    'https://www.googleapis.com/auth/classroom.courses.readonly',
    'https://www.googleapis.com/auth/classroom.announcements.readonly',
    'https://www.googleapis.com/auth/classroom.student-submissions.students.readonly',  # For submissions
    'https://www.googleapis.com/auth/classroom.student-submissions.me.readonly',
    'https://www.googleapis.com/auth/drive.file'  # For uploading PDFs to Drive
]

# ---------------------- Core Class ----------------------
class ClassroomTool:
    def __init__(self, credentials_file='credentials.json', token_file='token.pickle'):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = None
        self.drive_service = None
        self.gemini_model = None
        self._authenticate()
        self._setup_gemini()

    def _authenticate(self):
        """Authenticate with Google Classroom and Drive APIs."""
        creds = None
        if os.path.exists(self.token_file):
            with open(self.token_file, 'rb') as token:
                creds = pickle.load(token)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                console.print("[yellow]Refreshing expired credentials...[/yellow]")
                creds.refresh(Request())
            else:
                if not os.path.exists(self.credentials_file):
                    raise FileNotFoundError(
                        f"Credentials file '{self.credentials_file}' not found. "
                        "Download it from Google Cloud Console and name it 'credentials.json'."
                    )
                console.print("[cyan]Authenticating with Google...[/cyan]")
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)

            with open(self.token_file, 'wb') as token:
                pickle.dump(creds, token)
            console.print("[green]✓ Authentication successful![/green]")

        self.service = build('classroom', 'v1', credentials=creds)
        self.drive_service = build('drive', 'v3', credentials=creds)

    def _setup_gemini(self):
        """Setup Gemini AI."""
        api_key = os.getenv('GEMINI_API_KEY')
        if not api_key:
            raise ValueError("Missing GEMINI_API_KEY in environment variables (.env file).")
        genai.configure(api_key=api_key)
        self.gemini_model = genai.GenerativeModel('models/gemini-1.5-flash')  # Updated to a recent model
        console.print("[green]✓ Gemini AI configured![/green]")

    def get_courses(self):
        """Fetch available Google Classroom courses."""
        try:
            results = self.service.courses().list(pageSize=100).execute()
            return results.get('courses', [])
        except HttpError as e:
            console.print(f"[red]⚠️ Error fetching courses:[/red] {e}")
            return []

    def get_announcements(self, course_id, max_results=10, since_hours=None):
        """Retrieve course announcements (e.g., to detect assignments)."""
        try:
            announcements, page_token = [], None
            while True:
                response = self.service.courses().announcements().list(
                    courseId=course_id,
                    pageSize=min(max_results, 100),
                    pageToken=page_token,
                    orderBy='updateTime desc'
                ).execute()

                items = response.get('announcements', [])
                if since_hours:
                    cutoff = datetime.now() - timedelta(hours=since_hours)
                    items = [i for i in items if self._parse_timestamp(i.get('updateTime')) > cutoff]

                announcements.extend(items)
                page_token = response.get('nextPageToken')
                if not page_token or len(announcements) >= max_results:
                    break

            return announcements[:max_results]
        except HttpError as e:
            console.print(f"[red]⚠️ Error fetching announcements:[/red] {e}")
            return []

    def _parse_timestamp(self, ts: str):
        if not ts:
            return datetime.min
        ts = ts.replace('Z', '+00:00')
        try:
            return datetime.fromisoformat(ts)
        except Exception:
            return datetime.min

    def generate_solution(self, assignment_text):
        """Use Gemini to generate code solutions for the assignment."""
        prompt = f"""
You are a Java programming expert. Generate complete, working Java code for the following assignment.
Include code for all programs, with comments. Also, simulate sample output as text (e.g., what would print in the console).

Assignment:
{assignment_text}

For each program:
- Provide the full code.
- Provide simulated output as plain text.
"""
        try:
            response = self.gemini_model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            return f"⚠️ Error generating solution: {str(e)}"

    def create_solution_pdf(self, solution_content, your_name, assignment_name):
        """Generate a PDF with solution code and simulated outputs."""
        pdf_filename = f"{your_name}_{assignment_name}.pdf"
        c = canvas.Canvas(pdf_filename, pagesize=letter)
        width, height = letter
        y = height - 50  # Starting y-position

        c.setFont("Helvetica-Bold", 14)
        c.drawString(50, y, f"Solution for {assignment_name}")
        y -= 30

        c.setFont("Helvetica", 12)
        lines = solution_content.split('\n')
        for line in lines:
            if y < 50:  # New page if needed
                c.showPage()
                y = height - 50
            c.drawString(50, y, line)
            y -= 15

        c.save()
        console.print(f"[green]✓ PDF generated: {pdf_filename}[/green]")
        return pdf_filename

    def upload_to_drive(self, pdf_filename):
        """Upload PDF to Google Drive and get shareable ID."""
        file_metadata = {'name': os.path.basename(pdf_filename)}
        media = MediaFileUpload(pdf_filename, mimetype='application/pdf')
        try:
            file = self.drive_service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id'
            ).execute()
            file_id = file.get('id')
            console.print(f"[green]✓ PDF uploaded to Drive: ID {file_id}[/green]")
            return file_id
        except HttpError as e:
            console.print(f"[red]⚠️ Error uploading to Drive:[/red] {e}")
            return None

    def submit_to_classroom(self, course_id, assignment_id, drive_file_id):
        """Submit the PDF as a student submission to the assignment."""
        submission = {
            'assignedGrade': None,
            'draftGrade': None,
            'submissionHistory': [],
            'attachments': [{
                'driveFile': {'id': drive_file_id}
            }]
        }
        try:
            self.service.courses().courseWork().studentSubmissions().modifyAttachments(
                courseId=course_id,
                courseWorkId=assignment_id,
                id='me',  # 'me' for the authenticated student
                body={'addAttachments': submission['attachments']}
            ).execute()
            # Alternatively, if creating a new submission, use turnIn() or similar
            console.print("[green]✓ Solution submitted to Google Classroom![/green]")
        except HttpError as e:
            console.print(f"[red]⚠️ Error submitting:[/red] {e}")

# ---------------------- Typer Commands ----------------------

# (Your existing list_courses and summarize commands here – omitted for brevity, but keep them in the script)

@app.command()
def solve_and_submit(
    course_id: str = typer.Option(..., help="Course ID where the assignment is."),
    assignment_id: str = typer.Option(..., help="Assignment ID (from announcement or Classroom)."),
    assignment_text: str = typer.Option(..., help="Text description of the assignment (e.g., paste from PDF)."),
    your_name: str = typer.Option("YourName", help="Your name for PDF filename."),
    assignment_name: str = typer.Option("Assignment", help="Assignment name for PDF filename."),
    credentials: str = typer.Option("credentials.json", help="Path to credentials file."),
    token: str = typer.Option("token.pickle", help="Path to saved token file.")
):
    """Solve an assignment using AI, generate PDF, and submit to Google Classroom."""
    console.print("🚀 [bold]Initializing Classroom Tool...[/bold]")
    tool = ClassroomTool(credentials_file=credentials, token_file=token)
    print()

    console.print("[cyan]Generating solution with Gemini AI...[/cyan]")
    solution = tool.generate_solution(assignment_text)
    console.print(f"[white]Generated Solution:[/white]\n{solution}\n")

    pdf_file = tool.create_solution_pdf(solution, your_name, assignment_name)

    drive_id = tool.upload_to_drive(pdf_file)
    if drive_id:
        tool.submit_to_classroom(course_id, assignment_id, drive_id)

    console.print("\n✅ Completed!")

# ---------------------- Main ----------------------
if __name__ == "__main__":
    app()
