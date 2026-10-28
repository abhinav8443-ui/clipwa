"""
Automated YouTube Shorts uploader using YouTube Data API v3 with OAuth2 and resumable uploads.
"""

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .config import Config

YOUTUBE_UPLOAD_SCOPE = ["https://www.googleapis.com/auth/youtube.upload"]
YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"


class YouTubeShortsUploader:
    """Manages YouTube Shorts authentication and resumable video uploads."""

    def __init__(
        self,
        client_secrets_file: Optional[str] = None,
        token_file: Optional[str] = None,
    ):
        self.client_secrets_file = client_secrets_file or Config.YOUTUBE_CLIENT_SECRETS_FILE
        self.token_file = token_file or Config.YOUTUBE_TOKEN_FILE
        self._youtube_client = None

    def get_authenticated_service(self):
        """Authenticate via OAuth 2.0 and return YouTube resource."""
        if self._youtube_client is not None:
            return self._youtube_client

        creds = None
        if os.path.exists(self.token_file):
            try:
                creds = Credentials.from_authorized_user_file(self.token_file, YOUTUBE_UPLOAD_SCOPE)
            except Exception:
                creds = None

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(self.client_secrets_file):
                    raise FileNotFoundError(
                        f"YouTube client secrets file not found at '{self.client_secrets_file}'.\n"
                        "To upload directly to YouTube Shorts:\n"
                        "1. Create a project in Google Cloud Console (https://console.cloud.google.com)\n"
                        "2. Enable YouTube Data API v3\n"
                        "3. Create OAuth 2.0 Client ID (Desktop app)\n"
                        "4. Download JSON credentials and save as 'client_secrets.json' in project folder."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.client_secrets_file, YOUTUBE_UPLOAD_SCOPE
                )
                creds = flow.run_local_server(port=0)

            # Save credentials for future runs
            with open(self.token_file, "w", encoding="utf-8") as token:
                token.write(creds.to_json())

        self._youtube_client = build(YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION, credentials=creds)
        return self._youtube_client

    def upload_short(
        self,
        video_path: str,
        title: str,
        description: str,
        tags: Optional[List[str]] = None,
        category_id: str = "24",  # 24 = Entertainment, 28 = Science & Tech
        privacy_status: str = "public",  # public, unlisted, private
        dry_run: bool = False,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Uploads a video file directly to YouTube Shorts with resumable upload chunking.
        """
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")

        # Ensure #Shorts is present in title for YouTube algorithm indexing
        if "#Shorts" not in title and "#shorts" not in title:
            title = f"{title.strip()} #Shorts"[:100]

        all_tags = list(tags or [])
        if "shorts" not in [t.lower() for t in all_tags]:
            all_tags.append("shorts")

        body = {
            "snippet": {
                "title": title,
                "description": f"{description.strip()}\n\n#Shorts",
                "tags": all_tags,
                "categoryId": category_id,
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": False,
            },
        }

        if dry_run:
            file_size = os.path.getsize(video_path)
            return {
                "status": "dry_run_success",
                "video_id": "MOCK_VIDEO_ID_123",
                "url": "https://youtube.com/shorts/MOCK_VIDEO_ID_123",
                "title": title,
                "description": body["snippet"]["description"],
                "tags": all_tags,
                "privacy_status": privacy_status,
                "file_size_bytes": file_size,
            }

        youtube = self.get_authenticated_service()
        media = MediaFileUpload(
            video_path,
            chunksize=1024 * 1024 * 4,  # 4MB chunks
            resumable=True,
            mimetype="video/mp4",
        )

        request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status and progress_callback:
                pct = int(status.progress() * 100)
                progress_callback({
                    "stage": "uploading",
                    "percent": pct,
                    "bytes_uploaded": status.resumable_progress,
                    "total_bytes": status.total_size,
                })

        video_id = response.get("id")
        video_url = f"https://youtube.com/shorts/{video_id}"

        return {
            "status": "uploaded",
            "video_id": video_id,
            "url": video_url,
            "title": title,
            "privacy_status": privacy_status,
            "response": response,
        }
