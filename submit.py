#!/usr/bin/env python3
"""
Google Classroom Assignment Auto-Solver
========================================

This CLI tool automatically:
1. Detects new coding assignments in Google Classroom
2. Analyzes assignment questions (Java/Python/C++)
3. Generates solutions with code and output screenshots
4. Creates a formatted PDF with all solutions
5. Uploads and submits the PDF to Google Classroom

Setup:
-------
1. Enable Google Classroom & Drive APIs in Google Cloud Console.
2. Download OAuth credentials.json.
3. Install dependencies:
   pip install google-auth google-auth-oauthlib google-api-python-client 
   pip install google-generativeai python-dotenv typer rich
   pip install reportlab Pillow matplotlib
4. Create a .env file with:
   GEMINI_API_KEY=your_api_key_here
5. Run:
   python assignment_solver.py detect --all-courses
"""

import os
import pickle
import re
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Tuple, Optional
import typer
from rich import print
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

import google.generativeai as genai
from dotenv import load_dotenv

# PDF Generation
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, PageBreak
from reportlab.platypus import Table, TableStyle, Preformatted
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# Image processing
from PIL import Image as PILImage
import io

# ---------------------- Setup ----------------------
app = typer.Typer(help="Auto-solve and submit coding assignments from Google Classroom.")
console = Console()
load_dotenv()

SCOPES = [
    'https://www.googleapis.com/auth/classroom.courses.readonly',
    'https://www.googleapis.com/auth/classroom.coursework.me',
    'https://www.googleapis.com/auth/classroom.coursework.students',
    'https://www.googleapis.com/auth/drive.file'
]

CODING_LANGUAGES = ['python', 'java', 'c++', 'cpp', 'c', 'javascript', 'js']
ASSIGNMENT_KEYWORDS = ['assignment', 'homework', 'coding', 'program', 'exercise', 'problem', 'question']


# ---------------------- Core Class ----------------------
class AssignmentSolver:
    def __init__(self, credentials_file='credentials.json', token_file='token.pickle'):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = None
        self.drive_service = None
        self.gemini_model = None
        self.creds = None
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
                        f"Credentials file '{self.credentials_file}' not found."
                    )
                console.print("[cyan]Authenticating with Google Classroom...[/cyan]")
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)

            with open(self.token_file, 'wb') as token:
                pickle.dump(creds, token)
            console.print("[green]✓ Authentication successful![/green]")

        self.creds = creds
        self.service = build('classroom', 'v1', credentials=creds)
        self.drive_service = build('drive', 'v3', credentials=creds)

    def _setup_gemini(self):
        """Setup Gemini AI."""
        api_key = os.getenv('GEMINI_API_KEY')
        if not api_key:
            raise ValueError("Missing GEMINI_API_KEY in environment variables.")
        genai.configure(api_key=api_key)
        self.gemini_model = genai.GenerativeModel('models/gemini-2.0-flash-exp')
        console.print("[green]✓ Gemini AI configured![/green]")

    def get_courses(self):
        """Fetch available Google Classroom courses."""
        try:
            results = self.service.courses().list(pageSize=100).execute()
            return results.get('courses', [])
        except HttpError as e:
            console.print(f"[red]⚠️ Error fetching courses:[/red] {e}")
            return []

    def get_coursework(self, course_id, since_hours=None):
        """Retrieve course assignments/coursework."""
        try:
            coursework_list = []
            page_token = None
            
            while True:
                response = self.service.courses().courseWork().list(
                    courseId=course_id,
                    pageSize=100,
                    pageToken=page_token,
                    orderBy='updateTime desc'
                ).execute()

                items = response.get('courseWork', [])
                
                if since_hours:
                    cutoff = datetime.now() - timedelta(hours=since_hours)
                    items = [i for i in items if self._parse_timestamp(i.get('updateTime')) > cutoff]

                coursework_list.extend(items)
                page_token = response.get('nextPageToken')
                if not page_token:
                    break

            return coursework_list
        except HttpError as e:
            console.print(f"[red]⚠️ Error fetching coursework:[/red] {e}")
            return []

    def _parse_timestamp(self, ts: str):
        if not ts:
            return datetime.min
        ts = ts.replace('Z', '+00:00')
        try:
            return datetime.fromisoformat(ts.replace('+00:00', ''))
        except Exception:
            return datetime.min

    def detect_coding_assignments(self, coursework_list: List[Dict]) -> List[Dict]:
        """Detect assignments that appear to be coding-related."""
        coding_assignments = []
        
        for work in coursework_list:
            title = work.get('title', '').lower()
            description = work.get('description', '').lower()
            combined_text = f"{title} {description}"
            
            # Check for coding keywords
            has_coding_keyword = any(keyword in combined_text for keyword in ASSIGNMENT_KEYWORDS)
            has_language = any(lang in combined_text for lang in CODING_LANGUAGES)
            
            if has_coding_keyword or has_language:
                coding_assignments.append(work)
        
        return coding_assignments

    def check_submission_status(self, course_id: str, coursework_id: str) -> Tuple[bool, Optional[str]]:
        """Check if assignment has already been submitted."""
        try:
            submissions = self.service.courses().courseWork().studentSubmissions().list(
                courseId=course_id,
                courseWorkId=coursework_id,
                userId='me'
            ).execute()
            
            student_submissions = submissions.get('studentSubmissions', [])
            if student_submissions:
                submission = student_submissions[0]
                state = submission.get('state', '')
                submission_id = submission.get('id', '')
                
                # States: NEW, CREATED, TURNED_IN, RETURNED, RECLAIMED_BY_STUDENT
                is_submitted = state in ['TURNED_IN', 'RETURNED']
                return is_submitted, submission_id
            
            return False, None
        except HttpError as e:
            console.print(f"[red]⚠️ Error checking submission:[/red] {e}")
            return False, None

    def extract_questions(self, assignment_text: str) -> Dict:
        """Use Gemini to extract and analyze coding questions from assignment."""
        prompt = f"""
You are analyzing a coding assignment. Extract all coding questions/problems from the following assignment text.

Assignment Text:
{assignment_text}

Please analyze and provide:
1. Identify the programming language required (Python, Java, C++, or multiple)
2. Extract each coding question/problem separately
3. For each question, identify:
   - Question number
   - Problem statement
   - Input/output requirements
   - Any constraints or special conditions
   - Expected functionality

Format your response as JSON:
{{
  "language": "python/java/c++",
  "total_questions": number,
  "questions": [
    {{
      "number": 1,
      "title": "Brief title",
      "problem": "Full problem statement",
      "requirements": "Key requirements",
      "input_output": "Input/output specifications"
    }}
  ]
}}

Be thorough and extract all questions. If no clear coding questions are found, indicate that.
"""
        
        try:
            response = self.gemini_model.generate_content(prompt)
            text = response.text.strip()
            
            # Extract JSON from markdown code blocks if present
            json_match = re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, re.DOTALL)
            if json_match:
                text = json_match.group(1)
            
            import json
            return json.loads(text)
        except Exception as e:
            console.print(f"[red]Error extracting questions:[/red] {e}")
            return {"language": "unknown", "total_questions": 0, "questions": []}

    def generate_solution(self, question: Dict, language: str) -> Dict:
        """Generate solution code for a specific question."""
        prompt = f"""
You are an expert programmer. Generate a complete, working solution for the following coding problem.

Programming Language: {language}
Question Number: {question.get('number', 1)}
Problem: {question.get('problem', '')}
Requirements: {question.get('requirements', '')}
Input/Output: {question.get('input_output', '')}

Provide:
1. Complete, executable code with proper comments
2. Example test cases with expected outputs
3. Explanation of the approach

Format your response as JSON:
{{
  "code": "Complete code here",
  "explanation": "Brief explanation of the approach",
  "test_cases": [
    {{"input": "test input", "output": "expected output"}}
  ]
}}

Make sure the code is:
- Syntactically correct
- Well-commented
- Follows best practices
- Handles edge cases
- Includes sample input/output in the code if applicable
"""
        
        try:
            response = self.gemini_model.generate_content(prompt)
            text = response.text.strip()
            
            # Extract JSON from markdown code blocks
            json_match = re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, re.DOTALL)
            if json_match:
                text = json_match.group(1)
            
            import json
            solution = json.loads(text)
            
            # Clean code from markdown if present
            code = solution.get('code', '')
            code_match = re.search(r'```(?:\w+)?\s*(.*?)\s*```', code, re.DOTALL)
            if code_match:
                solution['code'] = code_match.group(1).strip()
            
            return solution
        except Exception as e:
            console.print(f"[red]Error generating solution:[/red] {e}")
            return {"code": f"// Error generating solution: {e}", "explanation": "", "test_cases": []}

    def execute_code_and_capture(self, code: str, language: str, test_cases: List[Dict]) -> Tuple[str, bytes]:
        """Execute code and capture output as screenshot."""
        output_text = ""
        
        try:
            # Create temp file with appropriate extension
            ext_map = {'python': '.py', 'java': '.java', 'c++': '.cpp', 'cpp': '.cpp', 'c': '.c'}
            ext = ext_map.get(language.lower(), '.txt')
            
            with tempfile.NamedTemporaryFile(mode='w', suffix=ext, delete=False) as f:
                f.write(code)
                temp_file = f.name
            
            # Execute based on language
            if language.lower() == 'python':
                result = subprocess.run(
                    ['python3', temp_file],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                output_text = result.stdout + result.stderr
                
            elif language.lower() == 'java':
                # Compile
                compile_result = subprocess.run(
                    ['javac', temp_file],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if compile_result.returncode == 0:
                    # Run
                    class_name = Path(temp_file).stem
                    result = subprocess.run(
                        ['java', '-cp', str(Path(temp_file).parent), class_name],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    output_text = result.stdout + result.stderr
                else:
                    output_text = f"Compilation Error:\n{compile_result.stderr}"
                    
            elif language.lower() in ['c++', 'cpp']:
                # Compile
                exe_file = temp_file.replace('.cpp', '.out')
                compile_result = subprocess.run(
                    ['g++', temp_file, '-o', exe_file],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if compile_result.returncode == 0:
                    # Run
                    result = subprocess.run(
                        [exe_file],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    output_text = result.stdout + result.stderr
                else:
                    output_text = f"Compilation Error:\n{compile_result.stderr}"
            
            # Clean up
            os.unlink(temp_file)
            if language.lower() in ['c++', 'cpp'] and os.path.exists(exe_file):
                os.unlink(exe_file)
                
        except subprocess.TimeoutExpired:
            output_text = "Execution timeout (>5 seconds)"
        except Exception as e:
            output_text = f"Execution error: {str(e)}"
        
        # Create screenshot of output using matplotlib
        screenshot_bytes = self._create_output_screenshot(output_text, language)
        
        return output_text, screenshot_bytes

    def _create_output_screenshot(self, output_text: str, language: str) -> bytes:
        """Create a visual screenshot of the output."""
        import matplotlib.pyplot as plt
        import matplotlib.patches as patches
        
        fig, ax = plt.subplots(figsize=(10, 6))
        ax.axis('off')
        
        # Create a terminal-like background
        rect = patches.Rectangle((0, 0), 1, 1, linewidth=2, 
                                 edgecolor='#333', facecolor='#1e1e1e')
        ax.add_patch(rect)
        
        # Add output text
        wrapped_text = output_text[:1000]  # Limit output length
        ax.text(0.05, 0.95, f"{language.upper()} Output:\n\n{wrapped_text}",
               verticalalignment='top', horizontalalignment='left',
               fontfamily='monospace', fontsize=10, color='#00ff00',
               bbox=dict(boxstyle='round', facecolor='#1e1e1e', alpha=0.8))
        
        # Save to bytes
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
        plt.close()
        buf.seek(0)
        
        return buf.read()

    def create_solution_pdf(self, assignment_name: str, questions_data: Dict, 
                           solutions: List[Dict], output_path: str):
        """Create a formatted PDF with all solutions."""
        doc = SimpleDocTemplate(output_path, pagesize=letter,
                              rightMargin=0.75*inch, leftMargin=0.75*inch,
                              topMargin=1*inch, bottomMargin=0.75*inch)
        
        # Styles
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=24,
            textColor=colors.HexColor('#1a237e'),
            spaceAfter=30,
            alignment=TA_CENTER
        )
        
        heading_style = ParagraphStyle(
            'CustomHeading',
            parent=styles['Heading2'],
            fontSize=16,
            textColor=colors.HexColor('#283593'),
            spaceAfter=12,
            spaceBefore=12
        )
        
        code_style = ParagraphStyle(
            'Code',
            parent=styles['Code'],
            fontSize=9,
            leftIndent=20,
            rightIndent=20,
            spaceAfter=10
        )
        
        # Build PDF content
        story = []
        
        # Title Page
        story.append(Paragraph(f"{assignment_name}", title_style))
        story.append(Paragraph("SOLUTIONS", title_style))
        story.append(Spacer(1, 0.3*inch))
        
        # Info table
        info_data = [
            ['Language:', questions_data.get('language', 'N/A').upper()],
            ['Total Questions:', str(questions_data.get('total_questions', 0))],
            ['Generated:', datetime.now().strftime('%Y-%m-%d %H:%M')]
        ]
        info_table = Table(info_data, colWidths=[2*inch, 4*inch])
        info_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 11),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ]))
        story.append(info_table)
        story.append(PageBreak())
        
        # Solutions
        for i, (question, solution) in enumerate(zip(questions_data['questions'], solutions)):
            # Question header
            story.append(Paragraph(f"Question {question['number']}: {question.get('title', 'Problem')}", 
                                 heading_style))
            story.append(Spacer(1, 0.1*inch))
            
            # Problem statement
            story.append(Paragraph(f"<b>Problem:</b> {question.get('problem', 'N/A')}", 
                                 styles['Normal']))
            story.append(Spacer(1, 0.1*inch))
            
            # Code solution
            story.append(Paragraph("<b>Solution Code:</b>", heading_style))
            code_text = solution.get('code', '// No code generated')
            # Use Preformatted for code to preserve formatting
            code_lines = code_text.split('\n')
            for line in code_lines[:50]:  # Limit lines to avoid overflow
                story.append(Preformatted(line, code_style))
            story.append(Spacer(1, 0.2*inch))
            
            # Output screenshot
            if 'screenshot' in solution and solution['screenshot']:
                story.append(Paragraph("<b>Output Screenshot:</b>", heading_style))
                try:
                    img = Image(io.BytesIO(solution['screenshot']), width=5*inch, height=3*inch)
                    story.append(img)
                except Exception as e:
                    story.append(Paragraph(f"[Screenshot error: {e}]", styles['Normal']))
                story.append(Spacer(1, 0.2*inch))
            
            # Output text
            if 'output' in solution:
                story.append(Paragraph("<b>Output:</b>", heading_style))
                output_para = Paragraph(f"<font face='Courier'>{solution['output'][:500]}</font>", 
                                      styles['Normal'])
                story.append(output_para)
                story.append(Spacer(1, 0.2*inch))
            
            # Explanation
            if solution.get('explanation'):
                story.append(Paragraph("<b>Explanation:</b>", heading_style))
                story.append(Paragraph(solution['explanation'], styles['Normal']))
            
            if i < len(solutions) - 1:
                story.append(PageBreak())
        
        # Build PDF
        doc.build(story)
        console.print(f"[green]✓ PDF created: {output_path}[/green]")

    def upload_to_drive(self, file_path: str, file_name: str) -> str:
        """Upload file to Google Drive and return file ID."""
        try:
            file_metadata = {'name': file_name}
            media = MediaFileUpload(file_path, mimetype='application/pdf')
            
            file = self.drive_service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id'
            ).execute()
            
            file_id = file.get('id')
            console.print(f"[green]✓ Uploaded to Drive: {file_id}[/green]")
            return file_id
        except HttpError as e:
            console.print(f"[red]⚠️ Error uploading to Drive:[/red] {e}")
            return None

    def submit_assignment(self, course_id: str, coursework_id: str, 
                         submission_id: str, drive_file_id: str):
        """Attach file and turn in assignment."""
        try:
            # Attach file
            attachment = {
                'driveFile': {
                    'id': drive_file_id,
                    'title': 'Solution PDF'
                }
            }
            
            self.service.courses().courseWork().studentSubmissions().modifyAttachments(
                courseId=course_id,
                courseWorkId=coursework_id,
                id=submission_id,
                body={'addAttachments': [attachment]}
            ).execute()
            
            console.print("[green]✓ File attached to submission[/green]")
            
            # Turn in
            self.service.courses().courseWork().studentSubmissions().turnIn(
                courseId=course_id,
                courseWorkId=coursework_id,
                id=submission_id,
                body={}
            ).execute()
            
            console.print("[green]✓ Assignment turned in![/green]")
            
        except HttpError as e:
            console.print(f"[red]⚠️ Error submitting assignment:[/red] {e}")


# ---------------------- Commands ----------------------

@app.command()
def list_courses(
    credentials: str = typer.Option("credentials.json", help="Path to credentials file"),
    token: str = typer.Option("token.pickle", help="Path to saved token file"),
):
    """List all available Google Classroom courses."""
    console.print("📚 [bold]Fetching your courses...[/bold]\n")
    solver = AssignmentSolver(credentials_file=credentials, token_file=token)
    courses = solver.get_courses()
    if not courses:
        console.print("No courses found.")
        raise typer.Exit()
    for c in courses:
        console.print(f"• [green]{c['name']}[/green] (ID: {c['id']})")


@app.command()
def detect(
    course_id: str = typer.Option(None, help="Specific course ID to monitor."),
    all_courses: bool = typer.Option(False, "--all-courses", help="Monitor all courses."),
    since: int = typer.Option(168, help="Check assignments from last N hours (default: 168 = 1 week)."),
    auto_submit: bool = typer.Option(False, "--auto-submit", help="Automatically submit solutions."),
    skip_submitted: bool = typer.Option(True, help="Skip already submitted assignments."),
    output_dir: str = typer.Option("./solutions", help="Directory to save solution PDFs."),
    credentials: str = typer.Option("credentials.json", help="Path to credentials file."),
    token: str = typer.Option("token.pickle", help="Path to saved token file.")
):
    """
    Detect new coding assignments, generate solutions, and optionally submit them.
    """
    console.print("🤖 [bold]Initializing Assignment Auto-Solver...[/bold]\n")
    solver = AssignmentSolver(credentials_file=credentials, token_file=token)
    
    # Create output directory
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    
    # Determine courses
    if course_id:
        courses = [{'id': course_id, 'name': f'Course {course_id}'}]
    elif all_courses:
        courses = solver.get_courses()
        if not courses:
            console.print("[red]No courses found.[/red]")
            raise typer.Exit()
    else:
        console.print("[yellow]❗ Please specify either --course-id or --all-courses.[/yellow]")
        raise typer.Exit()
    
    total_assignments = 0
    total_solved = 0
    total_submitted = 0
    
    for course in courses:
        console.rule(f"[bold blue]📚 {course['name']}[/bold blue]")
        
        # Get coursework
        coursework = solver.get_coursework(course['id'], since_hours=since)
        if not coursework:
            console.print("[dim]No assignments found.[/dim]\n")
            continue
        
        # Detect coding assignments
        coding_assignments = solver.detect_coding_assignments(coursework)
        if not coding_assignments:
            console.print("[dim]No coding assignments detected.[/dim]\n")
            continue
        
        console.print(f"🎯 Found [green]{len(coding_assignments)}[/green] coding assignment(s)\n")
        total_assignments += len(coding_assignments)
        
        for assignment in coding_assignments:
            assignment_id = assignment['id']
            assignment_title = assignment.get('title', 'Untitled Assignment')
            description = assignment.get('description', '')
            
            console.print(Panel(
                f"[bold]Title:[/bold] {assignment_title}\n"
                f"[bold]ID:[/bold] {assignment_id}\n"
                f"[bold]Updated:[/bold] {assignment.get('updateTime', 'N/A')}",
                title="[cyan]📝 Assignment Detected[/cyan]",
                border_style="cyan"
            ))
            
            # Check submission status
            is_submitted, submission_id = solver.check_submission_status(course['id'], assignment_id)
            
            if is_submitted and skip_submitted:
                console.print("[yellow]⏭️  Already submitted, skipping...[/yellow]\n")
                continue
            
            if not submission_id:
                console.print("[yellow]⚠️ Could not retrieve submission ID, skipping...[/yellow]\n")
                continue
            
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console
            ) as progress:
                
                # Extract questions
                task = progress.add_task("🔍 Analyzing assignment...", total=None)
                full_text = f"{assignment_title}\n\n{description}"
                questions_data = solver.extract_questions(full_text)
                
                if questions_data['total_questions'] == 0:
                    console.print("[yellow]No coding questions detected in assignment.[/yellow]\n")
                    continue
                
                progress.update(task, description=f"✓ Found {questions_data['total_questions']} question(s)")
                progress.stop()
                
                console.print(f"\n[green]Found {questions_data['total_questions']} coding question(s)[/green]")
                console.print(f"[cyan]Language: {questions_data['language'].upper()}[/cyan]\n")
                
                # Generate solutions
                solutions = []
                for i, question in enumerate(questions_data['questions'], 1):
                    console.print(f"💻 Solving Question {i}...")
                    
                    # Generate solution
                    solution = solver.generate_solution(question, questions_data['language'])
                    
                    # Execute and capture
                    output, screenshot = solver.execute_code_and_capture(
                        solution['code'],
                        questions_data['language'],
                        solution.get('test_cases', [])
                    )
                    
                    solution['output'] = output
                    solution['screenshot'] = screenshot
                    solutions.append(solution)
                    
                    console.print(f"[green]✓ Question {i} solved[/green]")
                
                total_solved += 1
                
                # Create PDF
                console.print("\n📄 Generating solution PDF...")
                pdf_filename = f"{assignment_title.replace(' ', '_')}_SOLUTION.pdf"
                pdf_path = os.path.join(output_dir, pdf_filename)
                
                solver.create_solution_pdf(
                    assignment_title,
                    questions_data,
                    solutions,
                    pdf_path
                )
                
                console.print(f"[green]✓ PDF saved: {pdf_path}[/green]\n")
                
                # Submit if requested
                if auto_submit:
                    console.print("📤 Uploading and submitting...")
                    
                    # Upload to Drive
                    drive_file_id = solver.upload_to_drive(pdf_path, pdf_filename)
                    
                    if drive_file_id:
                        # Submit assignment
                        solver.submit_assignment(
                            course['id'],
                            assignment_id,
                            submission_id,
                            drive_file_id
                        )
                        total_submitted += 1
                        console.print("[bold green]✅ Assignment submitted successfully![/bold green]\n")
                    else:
                        console.print("[red]Failed to upload file.[/red]\n")
                else:
                    console.print("[yellow]ℹ️  Use --auto-submit to automatically turn in assignments.[/yellow]\n")
                
                console.print("="*80 + "\n")
    
    # Summary
    console.rule("[bold]Summary[/bold]")
    console.print(f"📊 Total coding assignments detected: [bold]{total_assignments}[/bold]")
    console.print(f"✅ Solutions generated: [bold green]{total_solved}[/bold green]")
    console.print(f"📤 Submitted: [bold blue]{total_submitted}[/bold blue]")
    console.print(f"\n💾 PDFs saved to: [cyan]{output_dir}[/cyan]\n")


@app.command()
def solve_manual(
    assignment_text: str = typer.Argument(..., help="Assignment text or file path"),
    language: str = typer.Option("python", help="Programming language (python/java/c++)"),
    output: str = typer.Option("solution.pdf", help="Output PDF filename"),
    credentials: str = typer.Option("credentials.json", help="Path to credentials file."),
    token: str = typer.Option("token.pickle", help="Path to saved token file.")
):
    """
    Manually solve an assignment from text or file (no Classroom integration).
    """
    console.print("🔧 [bold]Manual Assignment Solver[/bold]\n")
    solver = AssignmentSolver(credentials_file=credentials, token_file=token)
    
    # Read from file if path provided
    if os.path.exists(assignment_text):
        with open(assignment_text, 'r') as f:
            assignment_text = f.read()
    
    # Extract questions
    console.print("🔍 Analyzing assignment...")
    questions_data = solver.extract_questions(assignment_text)
    questions_data['language'] = language  # Override with specified language
    
    if questions_data['total_questions'] == 0:
        console.print("[red]No coding questions detected.[/red]")
        raise typer.Exit()
    
    console.print(f"[green]Found {questions_data['total_questions']} question(s)[/green]\n")
    
    # Generate solutions
    solutions = []
    for i, question in enumerate(questions_data['questions'], 1):
        console.print(f"💻 Solving Question {i}...")
        solution = solver.generate_solution(question, language)
        output_text, screenshot = solver.execute_code_and_capture(
            solution['code'], language, solution.get('test_cases', [])
        )
        solution['output'] = output_text
        solution['screenshot'] = screenshot
        solutions.append(solution)
        console.print(f"[green]✓ Question {i} solved[/green]")
    
    # Create PDF
    console.print("\n📄 Generating PDF...")
    solver.create_solution_pdf("Manual Assignment", questions_data, solutions, output)
    console.print(f"[bold green]✅ PDF created: {output}[/bold green]\n")


if __name__ == "__main__":
    app()
