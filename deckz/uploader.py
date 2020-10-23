from logging import getLogger
from pathlib import Path
from pickle import dump as pickle_dump, load as pickle_load
from typing import Any, Dict, List, Optional

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from tqdm import tqdm

from deckz import app_name
from deckz.exceptions import DeckzException
from deckz.paths import Paths


class Uploader:
    def __init__(self, paths: Paths):
        self._logger = getLogger(__name__)
        self._paths = paths
        self._service = self._build_service()
        folder_id = self._check_folders()
        self._create_backup(folder_id)
        links = self._upload(folder_id)
        print("Uploaded documents:")
        for pdf, link in links.items():
            print(f"  {pdf.name}: {link}")

    def _build_service(self) -> Any:
        if self._paths.gdrive_credentials.is_file():
            with self._paths.gdrive_credentials.open("rb") as fh:
                creds = pickle_load(fh)
        else:
            creds = None

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._paths.gdrive_secrets,
                    ["https://www.googleapis.com/auth/drive.file"],
                )
                creds = flow.run_local_server(port=0)
            with self._paths.gdrive_credentials.open("wb") as fh:
                pickle_dump(creds, fh)

        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def _check_folders(self) -> str:
        self._logger.info("Checking/creating folder hierarchy")
        folders = [app_name]
        folders.extend(self._paths.working_dir.relative_to(self._paths.git_dir).parts)
        parent = "root"
        for folder in folders:
            folder_id = self._get(folder=True, parents=[parent], name=folder)
            if folder_id is None:
                folder_id = self._create_folder(parent, folder)
                self._logger.debug(f"“{folder}” folder was created")
            else:
                self._logger.debug(f"“{folder}” folder was present")
            parent = folder_id
        return parent

    def _create_backup(self, folder_id: str) -> None:
        file_ids = self._list(folder=False, parents=[folder_id], name=None)
        if file_ids:
            self._logger.info("Creating backup of current files")
            old_backup_id = self._get(folder=True, parents=[folder_id], name="backup")
            if old_backup_id is not None:
                self._service.files().update(
                    fileId=old_backup_id, body=dict(name="backup-old")
                ).execute()
            backup_id = self._create_folder(parent=folder_id, name="backup")
            for file_id in file_ids:
                self._service.files().update(
                    fileId=file_id, addParents=backup_id, removeParents=folder_id
                ).execute()
            if old_backup_id is not None:
                self._service.files().delete(fileId=old_backup_id).execute()

    def _upload(self, folder_id: str) -> Dict[Path, str]:
        self._logger.info("Uploading pdf/*-print.pdf")
        pdfs = list((self._paths.working_dir / "pdf").glob("*-print.pdf"))
        links: Dict[Path, str] = {}
        for pdf in pdfs:
            pdf_size = pdf.stat().st_size
            file_metadata = dict(name=pdf.name, parents=[folder_id])
            media = MediaFileUpload(
                str(pdf),
                chunksize=256 * 1024,
                mimetype="application/pdf",
                resumable=True,
            )
            request = self._service.files().create(
                body=file_metadata, media_body=media, fields="id,webViewLink"
            )
            response = None
            with tqdm(
                desc=pdf.name, total=pdf_size, unit="o", unit_scale=True
            ) as progress_bar:
                previous_progress = 0
                while response is None:
                    status, response = request.next_chunk()
                    if status and previous_progress != status.progress():
                        progress_bar.update(
                            int((status.progress() - previous_progress) * pdf_size)
                        )
                        previous_progress = status.progress()
            self._logger.debug(
                f"Setting permissions for {pdf.relative_to(self._paths.working_dir)}"
            )
            upload_id, upload_link = response.get("id"), response.get("webViewLink")
            self._service.permissions().create(
                fileId=upload_id, body={"type": "anyone", "role": "reader"}
            ).execute()
            links[pdf] = upload_link
        return links

    def _create_folder(self, parent: str, name: str) -> str:
        file_metadata = dict(
            name=name, mimeType="application/vnd.google-apps.folder", parents=[parent]
        )
        file = self._service.files().create(body=file_metadata, fields="id").execute()
        return file.get("id")

    def _list(
        self, folder: Optional[bool], parents: List[str], name: Optional[str],
    ) -> List[str]:
        return [item.get("id") for item in self._query(folder, parents, name)]

    def _get(
        self, folder: Optional[bool], parents: List[str], name: Optional[str]
    ) -> Optional[str]:
        results = self._query(folder, parents, name)
        if len(results) > 1:
            raise DeckzException("Found several files while trying to retrieve one.")
        return results[0].get("id") if results else None

    def _query(
        self, folder: Optional[bool], parents: List[str], name: Optional[str]
    ) -> Any:
        page_token = None
        results = []
        query_conditions = ["trashed = false"]
        if folder is not None:
            query_conditions.append(
                f"mimeType {'=' if folder else '!='} "
                "'application/vnd.google-apps.folder'"
            )
        for parent in parents:
            query_conditions.append(f"'{parent}' in parents")
        if name is not None:
            query_conditions.append(f"name = '{name}'")
        query = " and ".join(query_conditions)
        while True:
            response = (
                self._service.files()
                .list(
                    q=query,
                    spaces="drive",
                    fields="nextPageToken, files(id)",
                    pageToken=page_token,
                )
                .execute()
            )
            results.extend(response.get("files", []))
            page_token = response.get("nextPageToken", None)
            if page_token is None:
                break
        return results
