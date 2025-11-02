Genie: AI-Powered Google Classroom Assistant

Genie is a command-line tool that connects with Google Classroom and Google Drive using Gemini AI. It helps students by generating study aids automatically, analyzing announcements for projects and lab tests, and compiling source code into a submission-ready document.

Features:
• Study Aid Generation (detect-materials): Scans for new lecture materials, extracts content, and creates audio summaries, flashcards, and quizzes. Uploads them to Google Drive.
• Announcement Monitoring (detect-announcements): Detects important announcements and generates project ideas or lab test materials based on content.
• AI Summarization (summarize-announcements): Creates a short summary of all new announcements with deadlines and tasks.
• On-Demand Analysis (analyze-announcement): Allows you to analyze any text for project or lab-specific guidance.
• Code-to-Docx Generation (generate-doc): Converts source code files into a single .docx file with a title page.
• Course Listing (list-courses): Lists all active Google Classroom courses and their IDs.

Setup and Installation

Step 1: Google Cloud Setup

Open Google Cloud Console and create a new project (example: Classroom Genie).

Enable these APIs: Google Classroom API and Google Drive API.

Open OAuth consent screen, choose External, and fill necessary details.

Add the following scopes:

.../auth/classroom.courses.readonly

.../auth/classroom.courseworkmaterials.readonly

.../auth/classroom.announcements.readonly

.../auth/drive.readonly

.../auth/drive.file

Add your email as a test user.

Create an OAuth client ID for Desktop App under Credentials.

Download the JSON credentials file and save it as credentials.json in the project folder.

Step 2: Get a Gemini API Key

Go to Google AI Studio.

Create an API key and copy it for later use.

Step 3: Local Project Setup

Clone the repository or save the merged_buddy.py script inside a new folder.

Create and activate a virtual environment:
python3 -m venv venv
source venv/bin/activate (Windows: .\venv\Scripts\activate)

Install required dependencies using either:
a) Create a requirements.txt file and run:
pip install -r requirements.txt
b) Or install directly:
pip install google-auth google-auth-oauthlib google-api-python-client google-generativeai python-dotenv typer rich pdfplumber gTTS python-docx rich.markdown

Create a .env file in the same folder and add:
GEMINI_API_KEY=YOUR_API_KEY_HERE
