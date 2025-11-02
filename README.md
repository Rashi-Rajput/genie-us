```
      .o.
     .'.'.
    .`.'.'`.
   .`.'.',`.`.
  .`.'.',`.`.'.
  `.'.',`.`.'.'
   '..',`.`.'
    `-...-'
    .-------.
   /         \
  |           |
  |           |
   \         /
    `-------'
```

# Genie-us 🧞✨

Your AI-Powered Google Classroom Assistant.

**Genie-us** is a powerful command-line tool designed to supercharge your Google Classroom experience. This tool connects to the Classroom and Drive APIs to automate the detection of new materials, generate AI-powered study aids, analyze announcements for important keywords, and even compile your source code into a formatted `.docx` document.

It's the perfect assistant for students who want to stay organized and get a head start on their studies. **You can find the demo video [here](https://drive.google.com/file/d/1drKuH6TiLZK1pg1Z3XWZHiyRG1p8X2TQ/view)** 🚀

## Features

*   ** Course Management**
    *   `list-courses`: See all your active Google Classroom courses.

*   **AI-Powered Study Aid Generation**
    *   `detect-materials`: Automatically detects new lecture materials (PDFs, Google Docs).
    *   For each material, it can generate:
        *   🎧 **Audio Summaries**: An MP3 narration of the key points.
        *   🃏 **Flashcards**: A `.csv` file with key terms and definitions.
        *   📝 **Quizzes**: A multiple-choice quiz in Markdown.
    *   All generated aids are automatically uploaded to your Google Drive.

*   **Intelligent Announcement Monitoring**
    *   `detect-announcements`: Scans announcements for keywords related to projects or lab tests.
    *   Generates tailored **project ideas** and **practice questions** based on the announcement's content using Gemini AI.
    *   `summarize-announcements`: Provides a concise summary of all recent announcements in a course.
    *   `analyze-announcement`: Manually analyze any text to generate project ideas or practice questions.

*   **Code Documentation**
    *   `generate-doc`: Scans a local directory of source code and generates a single, formatted `.docx` file.

## Setup and Installation

Follow these steps to get Genie-us up and running.

### 1. Prerequisites

*   Python 3.7 or higher.
*   **uv**: The project uses `uv` for fast package management. If you don't have it, install it via pip:
    ```bash
    pip install uv
    ```

### 2. Google Cloud Setup

You need to enable APIs and get credentials to allow the script to access your data.

1.  **Create a Google Cloud Project:** Go to the [Google Cloud Console](https://console.cloud.google.com/) and create a new project.
2.  **Enable APIs:**
    *   In the **Library**, search for and enable the following two APIs:
        1.  **Google Classroom API**
        2.  **Google Drive API**
3.  **Configure OAuth Consent Screen:**
    *   Go to **OAuth consent screen**.
    *   Choose **External** and click **Create**.
    *   Fill in the required fields (App name, User support email, Developer contact).
    *   On the **Test users** page, add the Google account email you'll be using with the tool.
4.  **Create Credentials:**
    *   Go to **Credentials**, click **+ CREATE CREDENTIALS**, and select **OAuth client ID**.
    *   For **Application type**, select **Desktop app**.
    *   Click **DOWNLOAD JSON** to get the credentials file.
    *   **Rename the downloaded file to `credentials.json`** and place it in your project directory.

### 3. Installation

1.  **Clone the Repository:**
    ```bash
    git clone https://github.com/your-username/genie-us.git
    cd genie-us
    ```

2.  **Create `requirements.txt`:**
    Create a file named `requirements.txt` in the project root and add the following dependencies to it:
    ```
    google-auth
    google-auth-oauthlib
    google-api-python-client
    google-generativeai
    python-dotenv
    typer
    rich
    pdfplumber
    gTTS
    python-docx
    ```

3.  **Install Dependencies with `uv` and run:**
    Run the following command to create a virtual environment and install all packages. `uv` will handle everything.
    ```bash
    uv run main.py --help
    ```

### 4. Configuration

1.  **Get a Gemini API Key:** Get your free key from [Google AI Studio](https://aistudio.google.com/app/apikey).
2.  **Create a `.env` file:** Create a file named `.env` in the project directory.
3.  **Add your API Key:** Add the key to the `.env` file like this:

    ```
    GEMINI_API_KEY=your_api_key_here
    ```

### 5. First-Time Authentication

The first time you run a command that requires Google access, you'll need to authorize the application.

1.  Run the `list-courses` command to start the authentication process:
    ```bash
    python merged_buddy.py list-courses
    ```
2.  A link will be printed to the console. Open it in your browser.
3.  Choose your Google account, grant the requested permissions (you may need to bypass the "unverified app" screen by clicking `Advanced > Go to...`).
4.  A `token.pickle` file will be created. This stores your authentication tokens so you won't have to log in again.

## Usage

Here are examples for each command.

---

### `list-courses`

List all your active Google Classroom courses.

```bash
python merged_buddy.py list-courses```

---

### `detect-materials`

Scan courses for new materials and generate study aids.

```bash
# Scan all courses for materials posted in the last 24 hours
python merged_buddy.py detect-materials --all-courses

# Scan a specific course for materials from the last 3 days (72 hours)
python merged_buddy.py detect-materials --course-id "1234567890" --since 72
```

---

### `summarize-announcements`

Get a high-level summary of recent announcements.

```bash
# Summarize announcements from all courses
python merged_buddy.py summarize-announcements --all-courses
```

---

### `detect-announcements`

Scan announcements for project/lab test keywords.

```bash
# Scan all courses for relevant announcements from the last week (168 hours)
python merged_buddy.py detect-announcements --all-courses --since 168
```

---

### `analyze-announcement`

Manually analyze any text to generate project ideas or practice questions.

```bash
python merged_buddy.py analyze-announcement "The final project proposal is due next Friday. It should involve creating a web application with a database backend." --course-name "Web Development"
```

---

### `generate-doc`

Scan a source code directory and generate a `.docx` file.

```bash
# Scan the 'src' directory and create 'Code_Documentation.docx'
python merged_buddy.py generate-doc --source "./src"

# Specify all options
python merged_buddy.py generate-doc \
  --source "./my_app" \
  --output-dir "./docs" \
  --filename "MyApp_Source_Code" \
  --title "My Awesome App - Source Code" \
  --extensions ".py,.js,.html" \
  --project-name "My Awesome App" \
  --github "https://github.com/user/my-awesome-app"
```

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.
