import os
import subprocess
import json
import logging
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError
import requests
import re
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from urllib.parse import urlparse

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Constants
SCOPES = ['https://www.googleapis.com/auth/drive.file']
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "client_secret.json")
TOKEN_PATH = "token.json"


def is_pdf_url(url):
    parsed = urlparse(url)
    return parsed.path.lower().endswith(".pdf")


def extract_url(text):
    url_pattern = r'(https?://[^\s]+)'
    match = re.search(url_pattern, text)
    return match.group(0) if match else None


def download_file(url, output_path):
    if "dropbox.com" in url:
        url = url.replace("?d1=0", "?d1=1").replace("www.dropbox.com", "dl.dropboxusercontent.com")
    headers = {}
    if "slack.com" in url:
        headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(response.content)


def optimize_pdf(input_file, output_file):
    subprocess.run([
        "gs",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        "-dPDFSETTINGS=/screen",
        "-dNOPAUSE",
        "-dBATCH",
        f"-sOutputFile={output_file}",
        input_file
    ], check=True)



# Google Drive service setup
def get_drive_service():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    else:
        flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CLIENT_SECRET, SCOPES)
        creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, 'w') as token_file:
            token_file.write(creds.to_json())
    return build('drive', 'v3', credentials=creds)


# Upload file to Google Drive and return shareable link
def upload_to_drive(file_path, filename, channel_name):
    service = get_drive_service()
    folder_id = None
    query = f"name='{channel_name}' and mimeType='application/vnd.google-apps.folder' and trashed=False"
    results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    items = results.get('files', [])
    if items:
        folder_id = items[0]['id']
    else:
        file_metadata = {'name': channel_name, 'mimeType': 'application/vnd.google-apps.folder'}
        folder = service.files().create(body=file_metadata, fields='id').execute()
        folder_id = folder.get('id')

    file_metadata = {'name': filename, 'parents': [folder_id]}
    media = MediaFileUpload(file_path, mimetype='application/pdf')
    file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    service.permissions().create(fileId=file['id'], body={"role": "reader", "type": "anyone"}).execute()
    return f"https://drive.google.com/file/d/{file['id']}/view"


# Slack app setup
app = App(token=SLACK_BOT_TOKEN)


@app.event("app_mention")
def handle_mentions(event, say, client):
    logger.info("app_mention received")

    try:
        channel_id = event["channel"]
        user_id = event["user"]
        folder_name = None

        if channel_id.startswith("D"):
            user_info = client.users_info(user=user_id)
            folder_name = user_info["user"]["name"]
        else:
            channel_info = client.conversations_info(channel=channel_id)
            folder_name = channel_info["channel"]["name"]

        # First, check for attached files
        files = event.get("files", [])
        if files:
            if len(files) > 1:
                say("Easy now... only one PDF at a time there cowboy...")
                return
            file_info = files[0]
            if file_info.get("filetype") != "pdf":
                say(
                    text="that doesn't work... (say text)",
                    thread_ts=event["ts"],
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "that doesn't work... no, just not gonna work..."
                            }
                        },
                        {
                            "type": "image",
                            "image_url": "https://media0.giphy.com/media/3ofT5Mn9OWRL8PBR3G/giphy.gif?cid=6104955ekf2b8za2cpu76xsyw30rkt6t0un1zv34sjadqej6&ep=v1_gifs_translate&rid=giphy.gif&ct=g",
                            "alt_text": "that doesn't work... (alt text) "
                        }
                    ]
                )
                return
            file_url = file_info.get("url_private_download")
            original_filename = file_info.get("name")
        else:
            # If no attached file, check for a URL in the message text
            text = event.get("text", "")
            url = extract_url(text)
            print("extracted url: ", url)

            if not url or not is_pdf_url(url):
                say(
                    text="that doesn't work... (say text)",
                    thread_ts=event["ts"],
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "this is not the right kind of link... "
                            }
                        },
                        {
                            "type": "image",
                            "image_url": "https://media1.giphy.com/media/rGEIoqUoIMPylWCwpk/giphy.gif?cid=6104955ep67fn4nxcm80fdb4w0u2jp7oy0n1yxer3d00b8fy&ep=v1_gifs_translate&rid=giphy.gif&ct=g",
                            "alt_text": "wrong link"
                        }
                    ]
                )
                return

            file_url = url
            original_filename = url.split("/")[-1]

        optimized_name = original_filename.replace(".pdf", "") + "_cmp.pdf"

        download_file(file_url, "/tmp/original.pdf")
        say(
            text="working on that... ",
            thread_ts=event["ts"],
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "working on that... "
                    }
                },
                {
                    "type": "image",
                    "image_url": "https://media3.giphy.com/media/tQliIp3sn1T44/giphy-downsized.gif?cid=6104955etizadp34ibga9hdrby8n5m00ckb3cgw2lx8767uk&ep=v1_gifs_translate&rid=giphy-downsized.gif&ct=g",
                    "alt_text": "compressing plans"
                }
            ]
        )
        optimize_pdf("/tmp/original.pdf", "/tmp/original_cmp.pdf")
        drive_link = upload_to_drive("/tmp/original_cmp.pdf", optimized_name, folder_name)

        say(f"Here's your optimized file: <{drive_link}|Download it here>", thread_ts=event["ts"])

    except SlackApiError as e:
        logger.error(f"Slack API error: {e}")
        say(f"Something went wrong while processing the file: {e}")
        say(
            text="that doesn't work... (say text)",
            thread_ts=event["ts"],
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "uh oh... we have a problem "
                    }
                },
                {
                    "type": "image",
                    "image_url": "https://media0.giphy.com/media/4Hx5nJBfi8FzFWxztb/giphy.gif?cid=6104955edjxje2ph9k20j8jcftmvubvf7xkrjozeg84x19nv&ep=v1_gifs_translate&rid=giphy.gif&ct=g",
                    "alt_text": "we have a problem"
                }
            ]
        )
    except Exception as e:
        logger.error(f"Unhandled exception: {e}")
        say(f"An unexpected error occurred: {e}")
        say(
            text="that doesn't work... (say text)",
            thread_ts=event["ts"],
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "uh oh... we have a problem "
                    }
                },
                {
                    "type": "image",
                    "image_url": "https://media0.giphy.com/media/4Hx5nJBfi8FzFWxztb/giphy.gif?cid=6104955edjxje2ph9k20j8jcftmvubvf7xkrjozeg84x19nv&ep=v1_gifs_translate&rid=giphy.gif&ct=g",
                    "alt_text": "we have a problem"
                }
            ]
        )



@app.event("message")
def handle_message_events(event, logger):
    subtype = event.get("subtype")
    if not subtype:
        logger.info(f"ignored message: {event.get('text', '')}")


if __name__ == "__main__":
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
