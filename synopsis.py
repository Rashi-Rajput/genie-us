#!/usr/bin/env python3
"""
Google Classroom Project Announcement Detector
===============================================

This CLI connects to the Google Classroom API to retrieve announcements,
detects project-related keywords, and generates tailored project ideas
based on the specific requirements in each announcement.

Features:
----------
• OAuth2 authentication with Google Classroom.
• Detects project keywords: synopsis, project, PBL, assignment, etc.
• Reads announcement specifications and requirements.
• Generates custom project ideas matching the announcement details.
• Provides relevant resources and example project links.

Setup:
-------
1. Enable Google Classroom & Drive APIs in Google Cloud Console.
2. Download OAuth credentials.json.
3. Install dependencies:
   pip install google-auth google-auth-oauthlib google-api-python-client google-generativeai python-dotenv typer rich
4. Create a .env file with:
   GEMINI_API_KEY=your_api_key_here
5. Run:
   python classroom_monitor.py detect --all-courses --since 24
"""

import os
import pickle
import re
from datetime import datetime, timedelta
from typing import List, Dict, Tuple
import typer
from rich import print
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import google.generativeai as genai
from dotenv import load_dotenv

# ---------------------- Setup ----------------------
app = typer.Typer(help="Detect project announcements and generate tailored project ideas with Gemini AI.")
console = Console()
load_dotenv()

SCOPES = [
    'https://www.googleapis.com/auth/classroom.courses.readonly',
    'https://www.googleapis.com/auth/classroom.announcements.readonly'
]

# Project-related keywords to detect
PROJECT_KEYWORDS = [
    'synopsis', 'project', 'pbl', 'problem-based learning', 'problem based learning',
    'assignment', 'capstone', 'thesis', 'research', 'presentation', 'proposal',
    'implementation', 'development', 'design', 'prototype', 'deliverable',
    'milestone', 'case study', 'report', 'documentation', 'mini project',
    'major project', 'final year project', 'term project', 'semester project'
]


# ---------------------- Core Class ----------------------
class ClassroomMonitor:
    def __init__(self, credentials_file='credentials.json', token_file='token.pickle'):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = None
        self.gemini_model = None
        self._authenticate()
        self._setup_gemini()

    def _authenticate(self):
        """Authenticate with Google Classroom API."""
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
                console.print("[cyan]Authenticating with Google Classroom...[/cyan]")
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)

            with open(self.token_file, 'wb') as token:
                pickle.dump(creds, token)
            console.print("[green]✓ Authentication successful![/green]")

        self.service = build('classroom', 'v1', credentials=creds)

    def _setup_gemini(self):
        """Setup Gemini AI."""
        api_key = os.getenv('GEMINI_API_KEY')
        if not api_key:
            raise ValueError("Missing GEMINI_API_KEY in environment variables (.env file).")
        genai.configure(api_key=api_key)
        self.gemini_model = genai.GenerativeModel('models/gemini-2.5-flash')
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
        """Retrieve course announcements."""
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
            return datetime.fromisoformat(ts.replace('+00:00', ''))
        except Exception:
            return datetime.min

    def detect_project_announcements(self, announcements: List[Dict]) -> List[Tuple[Dict, List[str]]]:
        """
        Detect announcements containing project-related keywords.
        Returns list of tuples: (announcement, matched_keywords)
        """
        project_announcements = []
        
        for ann in announcements:
            text = ann.get('text', '').lower()
            matched_keywords = []
            
            for keyword in PROJECT_KEYWORDS:
                if re.search(r'\b' + re.escape(keyword) + r'\b', text, re.IGNORECASE):
                    matched_keywords.append(keyword)
            
            if matched_keywords:
                project_announcements.append((ann, list(set(matched_keywords))))
        
        return project_announcements

    def generate_tailored_project_ideas(self, course_name: str, announcement_text: str, keywords: List[str]) -> str:
        """
        Generate project ideas tailored to the specific announcement requirements.
        """
        prompt = f"""
You are an expert educational advisor analyzing a project announcement and generating tailored project ideas.

Course: {course_name}
Project Announcement (Full Text):
{announcement_text}

Detected Keywords: {', '.join(keywords)}

Your task:
1. CAREFULLY READ and ANALYZE the announcement to understand:
   - Specific requirements and constraints
   - Topics or domains mentioned
   - Technologies or tools specified
   - Learning objectives
   - Deliverables expected
   - Any deadlines or milestones
   - Team size or collaboration requirements
   - Evaluation criteria

2. Generate 5-7 TAILORED PROJECT IDEAS that:
   - DIRECTLY match the announcement specifications
   - Address the stated requirements
   - Are feasible within the given constraints
   - Align with the course subject and level
   - Incorporate mentioned technologies/tools

3. For EACH project idea provide:
   - **Project Title**: Clear, descriptive name
   - **Description**: 2-3 sentences explaining the project
   - **How it meets requirements**: Explicitly state which announcement requirements it fulfills
   - **Key Technologies/Tools**: Specific tech stack
   - **Implementation Steps**: 4-6 high-level steps
   - **Expected Outcomes**: What students will learn/achieve
   - **Complexity Level**: Beginner/Intermediate/Advanced

4. RESOURCES & REFERENCES:
   - Provide 5-8 specific resources for EACH major technology/tool mentioned
   - Include actual GitHub repository search terms (e.g., "search GitHub for: 'student management system python django'")
   - List relevant tutorial websites with search queries
   - Suggest YouTube channels or specific video searches
   - Recommend documentation links (provide exact URLs where possible)
   - Mention similar projects on platforms like:
     * GitHub (provide search terms)
     * Kaggle (for data science projects)
     * CodePen/JSFiddle (for web projects)
     * Instructables (for hardware projects)

5. EXAMPLE PROJECT LINKS & SEARCH STRATEGIES:
   - Provide specific GitHub search queries that will find similar completed projects
   - Format: "GitHub Search: [exact search term]"
   - Example: "GitHub Search: 'e-commerce website react node mongodb'"
   - Include alternative search terms for different platforms

Format your response clearly with:
- Main headers (##)
- Subheaders (###)
- Bullet points for lists
- Code blocks for search terms or commands
- Bold for emphasis

Be SPECIFIC, PRACTICAL, and ensure all ideas are directly relevant to the announcement content.
"""
        
        try:
            response = self.gemini_model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            return f"⚠️ Error generating project ideas: {str(e)}"


# ---------------------- Typer Commands ----------------------

@app.command()
def list_courses(
    credentials: str = typer.Option("credentials.json", help="Path to credentials file"),
    token: str = typer.Option("token.pickle", help="Path to saved token file"),
):
    """List all available Google Classroom courses."""
    console.print("📚 [bold]Fetching your courses...[/bold]\n")
    monitor = ClassroomMonitor(credentials_file=credentials, token_file=token)
    courses = monitor.get_courses()
    if not courses:
        console.print("No courses found.")
        raise typer.Exit()
    for c in courses:
        console.print(f"• [green]{c['name']}[/green] (ID: {c['id']})")


@app.command()
def detect(
    course_id: str = typer.Option(None, help="Specific course ID to scan."),
    all_courses: bool = typer.Option(False, "--all-courses", help="Scan all available courses."),
    max: int = typer.Option(20, help="Max announcements to scan per course."),
    since: int = typer.Option(None, help="Only scan announcements from last N hours."),
    keywords_only: bool = typer.Option(False, help="Only show detected keywords without generating ideas."),
    credentials: str = typer.Option("credentials.json", help="Path to credentials file."),
    token: str = typer.Option("token.pickle", help="Path to saved token file.")
):
    """
    Detect project announcements and generate tailored project ideas based on announcement specifications.
    """
    console.print("🚀 [bold]Initializing Google Classroom Project Detector...[/bold]")
    monitor = ClassroomMonitor(credentials_file=credentials, token_file=token)
    print()

    # Determine which courses to process
    if course_id:
        courses = [{'id': course_id, 'name': f'Course {course_id}'}]
    elif all_courses:
        courses = monitor.get_courses()
        if not courses:
            console.print("[red]No courses found.[/red]")
            raise typer.Exit()
    else:
        console.print("[yellow]❗ Please specify either --course-id or --all-courses.[/yellow]")
        raise typer.Exit()

    total_announcements = 0
    total_projects_detected = 0
    
    for course in courses:
        console.rule(f"[bold blue]🔍 Scanning: {course['name']}[/bold blue]")
        anns = monitor.get_announcements(course['id'], max_results=max, since_hours=since)
        
        if not anns:
            console.print("[dim]No announcements found in this course.[/dim]\n")
            continue

        total_announcements += len(anns)
        console.print(f"📢 Scanning [green]{len(anns)}[/green] announcement(s) for project keywords...\n")
        
        # Detect project announcements
        project_anns = monitor.detect_project_announcements(anns)
        
        if not project_anns:
            console.print("[dim]✗ No project-related announcements detected in this course.[/dim]\n")
            continue
        
        total_projects_detected += len(project_anns)
        console.print(f"🎯 [bold green]Found {len(project_anns)} project-related announcement(s)![/bold green]\n")
        
        # Process each project announcement
        for idx, (ann, keywords) in enumerate(project_anns, start=1):
            timestamp = monitor._parse_timestamp(ann.get('updateTime')).strftime("%Y-%m-%d %H:%M")
            text = ann.get('text', 'No content').strip()
            
            # Display the announcement
            console.print(Panel(
                f"[bold yellow]📅 Posted:[/bold yellow] {timestamp}\n\n"
                f"[bold yellow]🔑 Detected Keywords:[/bold yellow] {', '.join(keywords)}\n\n"
                f"[bold yellow]📝 Full Announcement:[/bold yellow]\n{text}",
                title=f"[bold cyan]Project Announcement #{idx} - {course['name']}[/bold cyan]",
                border_style="cyan",
                padding=(1, 2)
            ))
            
            if keywords_only:
                console.print()
                continue
            
            # Generate tailored project ideas
            console.print("\n💡 [bold magenta]Analyzing announcement and generating tailored project ideas...[/bold magenta]\n")
            
            project_ideas = monitor.generate_tailored_project_ideas(
                course['name'], 
                text, 
                keywords
            )
            
            # Display project ideas
            console.print(Panel(
                Markdown(project_ideas),
                title=f"[bold green]🚀 Tailored Project Ideas & Resources[/bold green]",
                border_style="green",
                padding=(1, 2)
            ))
            console.print("\n" + "="*80 + "\n")

    # Summary
    console.rule("[bold]Summary[/bold]")
    console.print(f"✅ Scanned [bold]{total_announcements}[/bold] announcements across [bold]{len(courses)}[/bold] course(s)")
    console.print(f"🎯 Detected [bold green]{total_projects_detected}[/bold green] project-related announcements")
    
    if total_projects_detected == 0:
        console.print("\n[yellow]💡 Tip: Try increasing the scan window with --since option or check more courses with --all-courses[/yellow]")
    
    console.print()


@app.command()
def analyze(
    announcement_text: str = typer.Argument(..., help="Project announcement text to analyze"),
    course_name: str = typer.Option("General", help="Course name for context"),
    credentials: str = typer.Option("credentials.json", help="Path to credentials file."),
    token: str = typer.Option("token.pickle", help="Path to saved token file.")
):
    """
    Analyze a specific project announcement text and generate tailored project ideas.
    Useful for testing or analyzing announcements from outside Google Classroom.
    """
    console.print(f"🔍 [bold]Analyzing project announcement for: {course_name}[/bold]\n")
    monitor = ClassroomMonitor(credentials_file=credentials, token_file=token)
    
    # Detect keywords
    matched_keywords = []
    text_lower = announcement_text.lower()
    for keyword in PROJECT_KEYWORDS:
        if re.search(r'\b' + re.escape(keyword) + r'\b', text_lower, re.IGNORECASE):
            matched_keywords.append(keyword)
    
    if not matched_keywords:
        console.print("[yellow]⚠️ No project-related keywords detected in the provided text.[/yellow]")
        console.print(f"[dim]Looking for keywords like: {', '.join(PROJECT_KEYWORDS[:10])}...[/dim]\n")
        return
    
    console.print(Panel(
        f"[bold yellow]🔑 Detected Keywords:[/bold yellow] {', '.join(matched_keywords)}\n\n"
        f"[bold yellow]📝 Announcement Text:[/bold yellow]\n{announcement_text}",
        title=f"[bold cyan]Project Announcement Analysis[/bold cyan]",
        border_style="cyan",
        padding=(1, 2)
    ))
    
    console.print("\n💡 [bold magenta]Generating tailored project ideas...[/bold magenta]\n")
    
    project_ideas = monitor.generate_tailored_project_ideas(
        course_name, 
        announcement_text, 
        matched_keywords
    )
    
    console.print(Panel(
        Markdown(project_ideas),
        title=f"[bold green]🚀 Tailored Project Ideas & Resources[/bold green]",
        border_style="green",
        padding=(1, 2)
    ))
    console.print()


# ---------------------- Main ----------------------
if __name__ == "__main__":
    app()
