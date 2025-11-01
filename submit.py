#!/usr/bin/env python3
"""
Google Classroom Automation CLI
- Lists classes (courses) and assignments
- Fetches assignment PDF, extracts text
- Uses OpenAI to generate Java solutions per question
- Compiles and runs Java; captures outputs
- Builds a PDF solution doc with title page
- Uploads to Drive, attaches to Classroom submission, and turns in

Author: You
"""

import argparse
import os
import sys
import re
import io
import json
import time
import shutil
import logging
import tempfile
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple

# Google APIs
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# PDF text extraction
from pdfminer.high_level import extract_text

# OCR fallback (optional)
try:
    import pytesseract
    from pdf2image import convert_from_path
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False

# PDF generation
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Preformatted

# OpenAI for code generation
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except Exception:
    OPENAI_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# Google API scopes
SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file"
]

DEFAULT_STUDENT_NAME = "Rashi"

@dataclass
class QuestionSolution:
    index: int
    question_text: str
    class_name: str
    java_code: str
    run_args: List[str] = field(default_factory=list)
    output: str = ""
    compile_ok: bool = False
    run_ok: bool = False
    compile_err: str = ""
    run_err: str = ""


def get_credentials() -> Credentials:
    creds = None
    token_path = "token.json"
    client_secret_path = os.environ.get("GOOGLE_CLIENT_SECRET", "client_secret.json")
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                from google.auth.transport.requests import Request
                creds.refresh(Request())
            except Exception:
                creds = None
        if not creds:
            if not os.path.exists(client_secret_path):
                raise FileNotFoundError("Missing OAuth client secrets at client_secret.json. "
                                        "Download from Google Cloud Console and save as client_secret.json")
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as token:
            token.write(creds.to_json())
    return creds


def classroom_service(creds: Credentials):
    return build("classroom", "v1", credentials=creds)


def drive_service(creds: Credentials):
    return build("drive", "v3", credentials=creds)


def list_courses_cmd(args):
    creds = get_credentials()
    service = classroom_service(creds)
    courses = []
    page_token = None
    while True:
        resp = service.courses().list(pageSize=100, pageToken=page_token).execute()
        courses.extend(resp.get("courses", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    if not courses:
        print("No classes found.")
        return

    print("Classes (Courses):")
    print("-" * 80)
    for c in courses:
        cid = c.get("id")
        name = c.get("name")
        section = c.get("section", "")
        state = c.get("courseState", "")
        print(f"ID: {cid} | Name: {name} | Section: {section} | State: {state}")


def list_assignments_cmd(args):
    creds = get_credentials()
    service = classroom_service(creds)
    course_id = args.course_id
    try:
        course = service.courses().get(id=course_id).execute()
        print(f"Class: {course.get('name')} (ID: {course_id})")
    except HttpError as e:
        logging.error(f"Error fetching course: {e}")
        sys.exit(1)

    cw_service = service.courses().courseWork()
    courseworks = []
    page_token = None
    while True:
        resp = cw_service.list(courseId=course_id, pageSize=100, pageToken=page_token).execute()
        courseworks.extend(resp.get("courseWork", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    if not courseworks:
        print("No assignments found for this class.")
        return

    print("Assignments:")
    print("-" * 80)
    for cw in courseworks:
        cid = cw.get("id")
        title = cw.get("title")
        state = cw.get("state", "")
        due = cw.get("dueDate", {})
        due_time = cw.get("dueTime", {})
        due_str = ""
        if due:
            due_str = f"{due.get('year','')}-{str(due.get('month','')).zfill(2)}-{str(due.get('day','')).zfill(2)}"
            if due_time:
                due_str += f" {str(due_time.get('hours','0')).zfill(2)}:{str(due_time.get('minutes','0')).zfill(2)}"
        print(f"ID: {cid} | Title: {title} | State: {state} | Due: {due_str}")
        # Show materials briefly
        mats = cw.get("materials", [])
        if mats:
            mat_summ = []
            for m in mats:
                if "driveFile" in m:
                    mat_summ.append(f"DriveFile: {m['driveFile']['driveFile'].get('title','(no title)')}")
                elif "link" in m:
                    mat_summ.append(f"Link: {m['link'].get('title','')}")
                elif "youtubeVideo" in m:
                    mat_summ.append("YouTube")
            if mat_summ:
                print("  Materials:", "; ".join(mat_summ))


def download_assignment_pdfs(service_classroom, service_drive, course_id: str, coursework_id: str, dest_dir: str) -> List[str]:
    """Download PDF materials for the coursework. Returns list of local file paths."""
    cw = service_classroom.courses().courseWork().get(courseId=course_id, id=coursework_id).execute()
    materials = cw.get("materials", [])
    pdf_paths = []
    os.makedirs(dest_dir, exist_ok=True)
    for m in materials:
        if "driveFile" in m:
            df = m["driveFile"]["driveFile"]
            file_id = df["id"]
            # Inspect mimeType
            meta = service_drive.files().get(fileId=file_id, fields="id,name,mimeType").execute()
            name = meta["name"]
            mime = meta.get("mimeType")
            if mime == "application/pdf" or name.lower().endswith(".pdf"):
                out_path = os.path.join(dest_dir, name)
                request = service_drive.files().get_media(fileId=file_id)
                fh = io.FileIO(out_path, "wb")
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    status, done = downloader.next_chunk()
                fh.close()
                logging.info(f"Downloaded: {out_path}")
                pdf_paths.append(out_path)
    return pdf_paths


def extract_text_from_pdf(pdf_path: str) -> str:
    try:
        text = extract_text(pdf_path)
        if text and len(text.strip()) >= 50:
            return text
    except Exception as e:
        logging.warning(f"pdfminer failed, trying OCR if available. Error: {e}")

    if not OCR_AVAILABLE:
        logging.warning("OCR not available; install pytesseract and pdf2image for scanned PDFs.")
        return ""

    # OCR fallback
    try:
        images = convert_from_path(pdf_path, dpi=300)
        ocr_texts = []
        for img in images:
            ocr_texts.append(pytesseract.image_to_string(img))
        return "\n".join(ocr_texts)
    except Exception as e:
        logging.error(f"OCR failed: {e}")
        return ""


def sanitize_class_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not re.match(r"[A-Za-z_]", name):
        name = f"Q_{name}"
    return name


def check_java_installed() -> Tuple[bool, str]:
    try:
        out = subprocess.run(["javac", "-version"], capture_output=True, text=True)
        if out.returncode == 0 or out.stderr.startswith("javac"):
            # Java prints version to stderr often
            ver_line = out.stderr.strip() or out.stdout.strip()
            return True, ver_line
        return False, out.stderr or out.stdout
    except FileNotFoundError:
        return False, "javac not found on PATH"


def compile_and_run_java(class_name: str, java_code: str, work_dir: str, run_args: Optional[List[str]] = None) -> Tuple[bool, bool, str, str]:
    """Compile and run a Java class. Returns (compile_ok, run_ok, stdout, stderr)."""
    run_args = run_args or []
    java_file = os.path.join(work_dir, f"{class_name}.java")
    with open(java_file, "w", encoding="utf-8") as f:
        f.write(java_code)

    # Compile
    comp = subprocess.run(["javac", "-encoding", "UTF-8", java_file], capture_output=True, text=True, cwd=work_dir)
    if comp.returncode != 0:
        return False, False, "", comp.stderr

    # Run
    run = subprocess.run(["java", class_name] + run_args, capture_output=True, text=True, cwd=work_dir, timeout=20)
    run_ok = (run.returncode == 0)
    return True, run_ok, run.stdout, run.stderr


def build_solution_pdf(output_path: str, course_name: str, assignment_name: str, student_name: str, solutions: List[QuestionSolution]):
    # Prepare document
    doc = SimpleDocTemplate(output_path, pagesize=LETTER, rightMargin=54, leftMargin=54, topMargin=54, bottomMargin=54)
    styles = getSampleStyleSheet()
    style_title = styles["Title"]
    style_h1 = styles["Heading1"]
    style_h2 = styles["Heading2"]
    style_body = styles["BodyText"]
    style_mono = ParagraphStyle(
        name="Monospace",
        parent=styles["Code"],
        fontName="Courier",
        fontSize=9,
        leading=12,
    )

    story = []

    # Title page
    story.append(Spacer(1, 2 * inch))
    story.append(Paragraph(assignment_name, style_title))
    story.append(Spacer(1, 0.3 * inch))
    story.append(Paragraph(f"Course: {course_name}", style_h2))
    story.append(Paragraph(f"Student: {student_name}", style_h2))
    story.append(Spacer(1, 4 * inch))
    story.append(Paragraph("Generated by Google Classroom CLI", style_body))
    story.append(PageBreak())

    # Questions
    for sol in solutions:
        story.append(Paragraph(f"Q{sol.index}. Original Question Text", style_h1))
        story.append(Spacer(1, 0.1 * inch))
        story.append(Paragraph(escape_html(sol.question_text).replace("\n", "<br/>"), style_body))
        story.append(Spacer(1, 0.2 * inch))

        story.append(Paragraph("✅ Java Code Solution (fully working)", style_h2))
        story.append(Spacer(1, 0.1 * inch))
        story.append(Preformatted(sol.java_code, style_mono))
        story.append(Spacer(1, 0.2 * inch))

        story.append(Paragraph("📸 Output Screenshot / Output Block", style_h2))
        story.append(Spacer(1, 0.1 * inch))
        output_block = sol.output if (sol.compile_ok and sol.run_ok) else (
            "Compilation or execution failed.\n"
            f"Compile OK: {sol.compile_ok}\n"
            f"Run OK: {sol.run_ok}\n"
            f"Compile Errors:\n{sol.compile_err}\n"
            f"Runtime Errors:\n{sol.run_err}\n"
        )
        story.append(Preformatted(output_block, style_mono))
        story.append(PageBreak())

    doc.build(story)
    logging.info(f"Solution PDF created: {output_path}")


def escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[^\w\-. ]", "_", name)
    name = name.strip().replace(" ", "_")
    return name[:120]


def upload_to_drive(drive, file_path: str, mime_type: str = "application/pdf") -> Dict[str, Any]:
    file_metadata = {
        "name": os.path.basename(file_path),
        "mimeType": mime_type,
    }
    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
    f = drive.files().create(body=file_metadata, media_body=media, fields="id,webViewLink").execute()
    logging.info(f"Uploaded to Drive: {f['id']}")
    return f


def get_my_submission(classroom, course_id: str, coursework_id: str) -> Optional[Dict[str, Any]]:
    # Many API versions accept userId='me' parameter
    try:
        resp = classroom.courses().courseWork().studentSubmissions().list(
            courseId=course_id, courseWorkId=coursework_id, pageSize=10, userId="me"
        ).execute()
    except TypeError:
        # Fallback if userId not supported: fetch and find first with "userId" equal to me (not trivial without profile)
        resp = classroom.courses().courseWork().studentSubmissions().list(
            courseId=course_id, courseWorkId=coursework_id, pageSize=100
        ).execute()
    subs = resp.get("studentSubmissions", [])
    if not subs:
        return None
    # Return the first submission for this user
    # Some courses allow multiple submissions; take the first in "state" NEW|CREATED|RECLAIMED_BY_STUDENT
    for s in subs:
        if s.get("state") in ("CREATED", "NEW", "RECLAIMED_BY_STUDENT", "RETURNED"):
            return s
    return subs[0]


def attach_and_turn_in(classroom, course_id: str, coursework_id: str, submission_id: str, drive_file_id: str):
    # Attach drive file
    try:
        req_body = {
            "addAttachments": [
                {"driveFile": {"id": drive_file_id}}
            ]
        }
        classroom.courses().courseWork().studentSubmissions().modifyAttachments(
            courseId=course_id, courseWorkId=coursework_id, id=submission_id, body=req_body
        ).execute()
        logging.info("Attached PDF to submission.")
    except HttpError as e:
        logging.error(f"Failed to attach file: {e}")

    # Turn in (if not already turned in)
    try:
        classroom.courses().courseWork().studentSubmissions().turnIn(
            courseId=course_id, courseWorkId=coursework_id, id=submission_id, body={}
        ).execute()
        logging.info("Submission turned in.")
    except HttpError as e:
        if "already turned in" in str(e).lower():
            logging.info("Submission already turned in.")
        else:
            logging.error(f"Turn in failed: {e}")


def generate_solutions_with_openai(assignment_text: str, course_name: str, assignment_name: str, student_name: str) -> List[QuestionSolution]:
    if not OPENAI_AVAILABLE:
        raise RuntimeError("OpenAI library not installed. pip install openai")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable is not set.")
    client = OpenAI(api_key=api_key)

    system = (
        "You are a senior Java instructor. Extract all programming questions from the provided assignment text. "
        "For each question:\n"
        "- Provide the original question text (verbatim).\n"
        "- Provide a fully working Java program with a public class and a main method.\n"
        "- Do NOT use packages.\n"
        "- Ensure the program does not require interactive input; if the prompt requires input, pick reasonable sample inputs embedded in code so it runs headlessly and demonstrates the solution.\n"
        "- Use class names: Q1_Solution, Q2_Solution, etc.\n"
        "- The code must compile with Java 17.\n"
        "Return a strict JSON object of the form:\n"
        "{\n"
        '  "questions": [\n'
        '    {\n'
        '      "index": 1,\n'
        '      "question_text": "<original text>",\n'
        '      "class_name": "Q1_Solution",\n'
        '      "java_code": "public class Q1_Solution { ... }",\n'
        '      "run_args": []\n'
        "    }, ...\n"
        "  ]\n"
        "}\n"
    )

    user = f"Course: {course_name}\nAssignment: {assignment_name}\nAssignment PDF text follows:\n---\n{assignment_text}\n---\n"

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.2,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ]
    )

    content = resp.choices[0].message.content
    # Try to extract JSON if surrounded by code fences
    json_text = content
    fence_match = re.search(r"```json\s*(.*?)```", content, re.S)
    if fence_match:
        json_text = fence_match.group(1).strip()
    else:
        # Try any fence
        fence_match = re.search(r"```(.*?)```", content, re.S)
        if fence_match:
            json_text = fence_match.group(1).strip()

    data = json.loads(json_text)
    out: List[QuestionSolution] = []
    for q in data.get("questions", []):
        idx = int(q.get("index", len(out) + 1))
        class_name = sanitize_class_name(q.get("class_name", f"Q{idx}_Solution"))
        code = q.get("java_code", "")
        # Ensure class name matches file class
        class_decl_match = re.search(r"public\s+class\s+([A-Za-z_]\w*)", code)
        if class_decl_match and class_decl_match.group(1) != class_name:
            # Rename class in code to expected class_name
            code = re.sub(r"(public\s+class\s+)([A-Za-z_]\w*)", r"\1" + class_name, code, count=1)

        out.append(QuestionSolution(
            index=idx,
            question_text=q.get("question_text", "").strip(),
            class_name=class_name,
            java_code=code.strip(),
            run_args=q.get("run_args", []) or []
        ))
    return out


def solve_and_submit_cmd(args):
    student_name = args.student_name or DEFAULT_STUDENT_NAME
    creds = get_credentials()
    classroom = classroom_service(creds)
    drive = drive_service(creds)

    course_id = args.course_id
    coursework_id = args.coursework_id

    # Fetch course and assignment meta
    course = classroom.courses().get(id=course_id).execute()
    cw = classroom.courses().courseWork().get(courseId=course_id, id=coursework_id).execute()
    course_name = course.get("name", f"Course_{course_id}")
    assignment_name = cw.get("title", f"Assignment_{coursework_id}")

    # Download or use provided PDF
    tmp_dir = tempfile.mkdtemp(prefix="gc_cli_")
    try:
        if args.pdf:
            pdf_paths = [args.pdf]
        else:
            pdf_paths = download_assignment_pdfs(classroom, drive, course_id, coursework_id, tmp_dir)

        if not pdf_paths:
            logging.error("No PDF materials found for this assignment. Provide --pdf to supply a local file.")
            sys.exit(1)

        pdf_path = pdf_paths[0]
        logging.info(f"Processing PDF: {pdf_path}")

        # Extract text
        text = extract_text_from_pdf(pdf_path)
        if not text or len(text.strip()) < 10:
            logging.error("Failed to extract text from the PDF.")
            sys.exit(1)

        # Check Java
        ok, ver = check_java_installed()
        if not ok:
            logging.error(f"Java compiler not available: {ver}")
            sys.exit(1)
        logging.info(f"Java detected: {ver}")

        # Generate solutions via OpenAI
        solutions = generate_solutions_with_openai(text, course_name, assignment_name, student_name)

        # Compile & run each
        build_dir = os.path.join(tmp_dir, "java_build")
        os.makedirs(build_dir, exist_ok=True)
        for sol in sorted(solutions, key=lambda s: s.index):
            comp_ok, run_ok, out_str, err_str = compile_and_run_java(sol.class_name, sol.java_code, build_dir, sol.run_args)
            sol.compile_ok = comp_ok
            sol.run_ok = run_ok
            sol.output = out_str
            sol.compile_err = "" if comp_ok else err_str
            sol.run_err = "" if run_ok else err_str
            logging.info(f"Q{sol.index} compile: {comp_ok}, run: {run_ok}")

        # Build PDF
        pdf_name = f"Rashi_{sanitize_filename(assignment_name)}.pdf"
        output_pdf = os.path.abspath(pdf_name)
        build_solution_pdf(output_pdf, course_name, assignment_name, student_name, solutions)

        # Upload to Drive
        uploaded = upload_to_drive(drive, output_pdf, mime_type="application/pdf")

        # Attach & turn in
        submission = get_my_submission(classroom, course_id, coursework_id)
        if not submission:
            logging.error("Could not find your student submission for this assignment.")
            sys.exit(1)

        attach_and_turn_in(classroom, course_id, coursework_id, submission["id"], uploaded["id"])
        print(f"Done. Submitted: {output_pdf} (Drive file ID: {uploaded['id']})")

    finally:
        if not args.keep_temp:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def process_local_pdf_cmd(args):
    student_name = args.student_name or DEFAULT_STUDENT_NAME
    course_name = args.course_name or "Unknown Course"
    assignment_name = args.assignment_name or "Assignment"
    pdf_path = args.pdf

    if not os.path.exists(pdf_path):
        logging.error(f"PDF not found: {pdf_path}")
        sys.exit(1)

    text = extract_text_from_pdf(pdf_path)
    if not text or len(text.strip()) < 10:
        logging.error("Failed to extract text from the PDF.")
        sys.exit(1)

    ok, ver = check_java_installed()
    if not ok:
        logging.error(f"Java compiler not available: {ver}")
        sys.exit(1)
    logging.info(f"Java detected: {ver}")

    solutions = generate_solutions_with_openai(text, course_name, assignment_name, student_name)

    tmp_dir = tempfile.mkdtemp(prefix="gc_cli_local_")
    try:
        build_dir = os.path.join(tmp_dir, "java_build")
        os.makedirs(build_dir, exist_ok=True)
        for sol in sorted(solutions, key=lambda s: s.index):
            comp_ok, run_ok, out_str, err_str = compile_and_run_java(sol.class_name, sol.java_code, build_dir, sol.run_args)
            sol.compile_ok = comp_ok
            sol.run_ok = run_ok
            sol.output = out_str
            sol.compile_err = "" if comp_ok else err_str
            sol.run_err = "" if run_ok else err_str

        pdf_name = f"Rashi_{sanitize_filename(assignment_name)}.pdf"
        output_pdf = os.path.abspath(pdf_name)
        build_solution_pdf(output_pdf, course_name, assignment_name, student_name, solutions)
        print(f"Built solution PDF: {output_pdf}")
    finally:
        if not args.keep_temp:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="Google Classroom Automation CLI")
    sub = parser.add_subparsers(required=True)

    # list-courses
    p1 = sub.add_parser("list-courses", help="List all classes and their IDs")
    p1.set_defaults(func=list_courses_cmd)

    # list-assignments
    p2 = sub.add_parser("list-assignments", help="List assignments and IDs for a class")
    p2.add_argument("--course-id", required=True, help="Course (class) ID")
    p2.set_defaults(func=list_assignments_cmd)

    # solve-and-submit
    p3 = sub.add_parser("solve-and-submit", help="Fetch assignment PDF, solve, generate PDF, and submit")
    p3.add_argument("--course-id", required=True, help="Course ID")
    p3.add_argument("--coursework-id", required=True, help="Assignment (courseWork) ID")
    p3.add_argument("--student-name", default=DEFAULT_STUDENT_NAME, help="Student name for title page")
    p3.add_argument("--pdf", help="Path to local assignment PDF (override download)")
    p3.add_argument("--keep-temp", action="store_true", help="Keep temp working directory for debugging")
    p3.set_defaults(func=solve_and_submit_cmd)

    # process-local-pdf (no submission)
    p4 = sub.add_parser("process-pdf", help="Process a local PDF, generate solutions PDF (no submission)")
    p4.add_argument("--pdf", required=True, help="Path to local assignment PDF")
    p4.add_argument("--course-name", help="Course name (for title page)")
    p4.add_argument("--assignment-name", help="Assignment name (for title page and filename)")
    p4.add_argument("--student-name", default=DEFAULT_STUDENT_NAME, help="Student name for title page")
    p4.add_argument("--keep-temp", action="store_true", help="Keep temp working directory for debugging")
    p4.set_defaults(func=process_local_pdf_cmd)

    args = parser.parse_args()
    try:
        args.func(args)
    except HttpError as e:
        logging.error(f"Google API error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(1)


if __name__ == "__main__":
    main()
