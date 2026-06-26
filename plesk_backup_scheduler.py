"""Download the latest backup from Plesk on a fixed schedule."""
from __future__ import annotations

import json
import random
import re
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import requests
import urllib3
from PyQt6.QtCore import QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


APP_NAME = "Plesk Backup Scheduler"

PERIODS: dict[str, int] = {
    "Every 8 hours": 8 * 3600,
    "Every 24 hours": 24 * 3600,
    "Every 3 days": 3 * 24 * 3600,
    "Every week": 7 * 24 * 3600,
    "Every 2 weeks": 14 * 24 * 3600,
    "Every month": 30 * 24 * 3600,
}


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


CONFIG_PATH = app_dir() / "config.json"


@dataclass
class Config:
    plesk_url: str = ""
    plesk_user: str = ""
    plesk_password: str = ""
    domain_id: int = 1
    domain_name: str = ""
    backup_dir: str = ""
    period: str = "Every 24 hours"

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                defaults = asdict(cls())
                return cls(**{**defaults, **data})
            except Exception:
                pass
        return cls()

    def save(self) -> None:
        CONFIG_PATH.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


class PleskError(RuntimeError):
    pass


class PleskClient:
    """Log in to Plesk, list backups, and download backup archives."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        domain_id: int,
        domain_name: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.domain_id = domain_id
        self.domain_name = domain_name.strip()
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({"User-Agent": "PleskBackupScheduler/1.0"})

    def login(self) -> None:
        login_page_url = f"{self.base_url}/login_up.php3"
        response = self.session.get(login_page_url, timeout=30)
        csrf_match = re.search(
            r'forgery_protection_token[^>]*content="([^"]+)"', response.text
        )
        csrf = csrf_match.group(1) if csrf_match else ""

        data = {
            "login_name": self.username,
            "passwd": self.password,
            "locale_id": "en-US",
        }
        if csrf:
            data["forgery_protection_token"] = csrf

        result = self.session.post(
            login_page_url,
            data=data,
            allow_redirects=False,
            timeout=30,
        )
        if result.status_code >= 400:
            raise PleskError(f"Login failed with HTTP {result.status_code}.")

        if "PLESKSESSID" not in self.session.cookies:
            err_match = re.search(
                r'"status":"error"[^}]*"content":"([^"]+)"', result.text
            ) or re.search(
                r'"content":"([^"]+)"[^}]*"status":"error"', result.text
            )
            msg = err_match.group(1) if err_match else "invalid username/password or locked account"
            try:
                msg = msg.encode().decode("unicode_escape")
            except Exception:
                pass
            raise PleskError(f"Plesk login failed: {msg}")

    def _fallback_dump_id(self, ts: str) -> str | None:
        if not self.domain_name:
            return None
        return (
            f"clients\\{self.username}\\domains\\{self.domain_name}\\"
            f"backup_info_{ts}.xml"
        )

    def list_backups(self, debug_dir: Path | None = None) -> list[dict]:
        url = f"{self.base_url}/smb/backup/list/domainId/{self.domain_id}"
        response = self.session.get(url, timeout=60)
        if response.status_code >= 400:
            raise PleskError(f"Could not retrieve backup list: HTTP {response.status_code}")
        html = response.text

        results: dict[str, str] = {}
        for raw in re.findall(r'dumpId=([^&"\'\s>]+)', html):
            decoded = urllib.parse.unquote(urllib.parse.unquote(raw))
            match = re.search(r'backup_info_(\d{10})\.xml', decoded)
            if match:
                ts = match.group(1)
                if ts not in results:
                    results[ts] = decoded

        if not results:
            for ts in set(re.findall(r'backup_info_(\d{10})\.xml', html)):
                fallback = self._fallback_dump_id(ts)
                if fallback:
                    results[ts] = fallback

        if not results:
            for ajax_url in (
                f"{self.base_url}/smb/backup/list-data/domainId/{self.domain_id}",
                f"{self.base_url}/smb/backup/data/domainId/{self.domain_id}",
                f"{self.base_url}/api/v2/cli/pleskbackup/call",
            ):
                try:
                    ajax_response = self.session.get(ajax_url, timeout=30)
                    if ajax_response.status_code == 200:
                        for ts in set(re.findall(r'backup_info_(\d{10})\.xml', ajax_response.text)):
                            fallback = self._fallback_dump_id(ts)
                            if fallback:
                                results[ts] = fallback
                        if results:
                            break
                except Exception:
                    pass

        if not results and debug_dir is not None:
            try:
                debug_dir.mkdir(parents=True, exist_ok=True)
                debug_file = debug_dir / "plesk_debug_list.html"
                debug_file.write_text(html, encoding="utf-8", errors="replace")
                raise PleskError(
                    "Could not find dumpId values in the backup list page. "
                    f"Saved debug HTML to: {debug_file}"
                )
            except PleskError:
                raise
            except Exception:
                pass

        items = [{"timestamp": ts, "dump_id": dump_id} for ts, dump_id in results.items()]
        items.sort(key=lambda x: x["timestamp"], reverse=True)
        return items

    def download(self, dump_id: str, dest_path: Path, progress_cb=None) -> Path:
        once_encoded = urllib.parse.quote(dump_id, safe="")
        list_url = f"{self.base_url}/smb/backup/list/domainId/{self.domain_id}"
        form_url = (
            f"{self.base_url}/smb/backup/download-local/domainId/"
            f"{self.domain_id}?type=local&dumpId={once_encoded}"
        )

        response_form = self.session.get(
            form_url,
            headers={"Referer": list_url},
            timeout=30,
        )
        if response_form.status_code >= 400:
            raise PleskError(f"Could not open download form: HTTP {response_form.status_code}")

        csrf_match = re.search(
            r'name="forgery_protection_token"[^>]*value="([^"]+)"',
            response_form.text,
        )
        if not csrf_match:
            raise PleskError("CSRF token not found in download form.")
        csrf = csrf_match.group(1)

        xhr_headers = {
            "Referer": form_url,
            "Origin": self.base_url,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Forgery-Protection-Token": csrf,
            "X-Prototype-Version": "1.7.3",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "text/javascript, text/html, application/xml, text/xml, */*",
        }
        xhr_body = (
            "secureBackup%5BusePasswordProtection%5D=false&hidden=&"
            f"forgery_protection_token={csrf}"
        )

        response_xhr = self.session.post(
            form_url,
            data=xhr_body,
            headers=xhr_headers,
            timeout=30,
            allow_redirects=False,
        )
        if response_xhr.status_code >= 400:
            raise PleskError(
                f"Could not disable password-protected download mode: HTTP {response_xhr.status_code}"
            )
        if '"status":"success"' not in response_xhr.text:
            raise PleskError(
                "Unexpected response when preparing backup download: "
                f"{response_xhr.text[:200]}"
            )

        random_id = random.randint(100000, 999999)
        download_url = (
            f"{self.base_url}/smb/backup/download-dump/domainId/"
            f"{self.domain_id}?dumpId={once_encoded}&_randomId={random_id}"
        )
        nav_headers = {
            "Referer": f"{list_url}?_randomId={random.randint(100000, 999999)}",
            "Origin": self.base_url,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1",
        }
        nav_body = f"type=local&dumpId={once_encoded}&forgery_protection_token={csrf}"

        temp_path = dest_path.with_suffix(dest_path.suffix + ".part")
        resume_from = temp_path.stat().st_size if temp_path.exists() else 0
        if resume_from > 0:
            nav_headers["Range"] = f"bytes={resume_from}-"

        with self.session.post(
            download_url,
            data=nav_body,
            headers=nav_headers,
            stream=True,
            timeout=(30, 7200),
            allow_redirects=False,
        ) as response:
            if response.status_code >= 400 and response.status_code != 416:
                raise PleskError(f"Download failed: HTTP {response.status_code}")

            content_type = response.headers.get("content-type", "")
            if content_type.startswith("text/html") or content_type.startswith("application/json"):
                raise PleskError(
                    f"Plesk returned '{content_type}' instead of a ZIP stream. "
                    "Your session may have expired, or dump_id is invalid."
                )

            is_partial = response.status_code == 206
            content_length = int(response.headers.get("content-length") or 0)
            if is_partial:
                total = resume_from + content_length if content_length else 0
                mode = "ab"
                downloaded = resume_from
                check_zip_signature = False
            else:
                total = content_length
                mode = "wb"
                downloaded = 0
                check_zip_signature = True

            first_chunk = True
            with open(temp_path, mode) as file_obj:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    if first_chunk and check_zip_signature:
                        first_chunk = False
                        if chunk[:2] != b"PK":
                            raise PleskError(
                                "Downloaded payload is not a ZIP file (missing PK signature)."
                            )
                    file_obj.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        progress_cb(downloaded, total)

            temp_path.replace(dest_path)
        return dest_path


class DownloadWorker(QThread):
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)
    log = pyqtSignal(str)
    progress = pyqtSignal("qint64", "qint64", float, float)

    ESTIMATED_TOTAL = 5_740_131_779

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg

    def run(self) -> None:
        try:
            output_dir = Path(self.cfg.backup_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

            self.log.emit(f"Connecting to Plesk: {self.cfg.plesk_url}")
            client = PleskClient(
                base_url=self.cfg.plesk_url,
                username=self.cfg.plesk_user,
                password=self.cfg.plesk_password,
                domain_id=self.cfg.domain_id,
                domain_name=self.cfg.domain_name,
            )
            client.login()
            self.log.emit("Login successful.")

            self.log.emit("Loading backup list...")
            backups = client.list_backups(debug_dir=app_dir())
            if not backups:
                raise PleskError(
                    "No backups were found in Plesk. Create at least one backup first."
                )

            latest = backups[0]
            ts = latest["timestamp"]
            self.log.emit(
                f"Latest backup: {self._fmt_ts(ts)} (total backups found: {len(backups)})"
            )

            file_name = f"plesk_backup_{ts}.zip"
            dest_path = output_dir / file_name

            if dest_path.exists():
                self.log.emit(f"Backup already exists locally, skipping: {file_name}")
                self.finished_ok.emit(str(dest_path))
                return

            self.log.emit(f"Downloading -> {file_name}")
            start_time = time.time()
            last_emit = [0.0]
            last_log_pct = [-1]

            def progress_callback(done: int, total: int) -> None:
                if total <= 0:
                    total = max(self.ESTIMATED_TOTAL, done)

                elapsed = time.time() - start_time
                speed = done / elapsed if elapsed > 0 else 0.0
                eta = (total - done) / speed if speed > 0 else -1.0

                now = time.time()
                if now - last_emit[0] >= 0.25 or done >= total:
                    last_emit[0] = now
                    self.progress.emit(done, total, speed, eta)

                pct = done * 100 // total
                if pct >= last_log_pct[0] + 10:
                    last_log_pct[0] = pct
                    self.log.emit(f"  {pct}% ({done:,}/{total:,} bytes)")

            client.download(latest["dump_id"], dest_path, progress_cb=progress_callback)
            size = dest_path.stat().st_size
            self.log.emit(f"Completed: {file_name} ({size:,} bytes)")
            self.finished_ok.emit(str(dest_path))
        except Exception as exc:
            self.failed.emit(str(exc))

    @staticmethod
    def _fmt_ts(ts: str) -> str:
        try:
            return f"20{ts[0:2]}-{ts[2:4]}-{ts[4:6]} {ts[6:8]}:{ts[8:10]}"
        except Exception:
            return ts


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(620, 650)

        self.cfg = Config.load()
        self.worker: DownloadWorker | None = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._run_download)
        self.running = False

        self._build_ui()
        self._apply_cfg_to_ui()
        self._set_running_state(False)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        connection_group = QGroupBox("Plesk Connection")
        form = QFormLayout(connection_group)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://your-plesk-host:8443")
        self.user_edit = QLineEdit()
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.subid_spin = QSpinBox()
        self.subid_spin.setRange(1, 999999)
        self.domain_name_edit = QLineEdit()
        self.domain_name_edit.setPlaceholderText("example.com (optional, improves fallback parsing)")

        form.addRow("Plesk URL:", self.url_edit)
        form.addRow("Username:", self.user_edit)
        form.addRow("Password:", self.pass_edit)
        form.addRow("Subscription (Domain) ID:", self.subid_spin)
        form.addRow("Primary Domain Name:", self.domain_name_edit)
        root.addWidget(connection_group)

        download_group = QGroupBox("Download Settings")
        download_layout = QVBoxLayout(download_group)

        row_period = QHBoxLayout()
        row_period.addWidget(QLabel("Schedule:"))
        self.period_combo = QComboBox()
        self.period_combo.addItems(list(PERIODS.keys()))
        row_period.addWidget(self.period_combo, 1)
        download_layout.addLayout(row_period)

        row_dir = QHBoxLayout()
        self.dir_edit = QLineEdit()
        self.dir_edit.setReadOnly(True)
        self.dir_btn = QPushButton("Choose Folder...")
        self.dir_btn.clicked.connect(self._pick_dir)
        row_dir.addWidget(QLabel("Backup destination:"))
        row_dir.addWidget(self.dir_edit, 1)
        row_dir.addWidget(self.dir_btn)
        download_layout.addLayout(row_dir)

        row_actions = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop)
        row_actions.addWidget(self.start_btn)
        row_actions.addWidget(self.stop_btn)
        download_layout.addLayout(row_actions)

        self.status_lbl = QLabel("Status: idle")
        download_layout.addWidget(self.status_lbl)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setTextVisible(True)
        download_layout.addWidget(self.progress_bar)

        self.progress_lbl = QLabel("")
        self.progress_lbl.setStyleSheet("font-family: Consolas, monospace;")
        download_layout.addWidget(self.progress_lbl)
        root.addWidget(download_group)

        root.addWidget(QLabel("Log:"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        root.addWidget(self.log_view, 1)

        info = QLabel(
            "This app only downloads backups that are already generated by Plesk. "
            "You must configure and schedule backup creation in Plesk first."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: gray;")
        root.addWidget(info)

    def _apply_cfg_to_ui(self) -> None:
        self.url_edit.setText(self.cfg.plesk_url)
        self.user_edit.setText(self.cfg.plesk_user)
        self.pass_edit.setText(self.cfg.plesk_password)
        self.subid_spin.setValue(self.cfg.domain_id or 1)
        self.domain_name_edit.setText(self.cfg.domain_name)
        self.dir_edit.setText(self.cfg.backup_dir)
        idx = self.period_combo.findText(self.cfg.period)
        if idx >= 0:
            self.period_combo.setCurrentIndex(idx)

    def _collect_cfg(self) -> Config:
        return Config(
            plesk_url=self.url_edit.text().strip(),
            plesk_user=self.user_edit.text().strip(),
            plesk_password=self.pass_edit.text(),
            domain_id=self.subid_spin.value(),
            domain_name=self.domain_name_edit.text().strip(),
            backup_dir=self.dir_edit.text().strip(),
            period=self.period_combo.currentText(),
        )

    def _pick_dir(self) -> None:
        start = self.cfg.backup_dir or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose backup folder", start)
        if chosen:
            self.dir_edit.setText(chosen)

    def _log(self, msg: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{timestamp}] {msg}")

    def _set_running_state(self, running: bool) -> None:
        self.running = running
        for widget in (
            self.url_edit,
            self.user_edit,
            self.pass_edit,
            self.subid_spin,
            self.domain_name_edit,
            self.period_combo,
            self.dir_btn,
        ):
            widget.setEnabled(not running)

        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    def _start(self) -> None:
        cfg = self._collect_cfg()
        missing = []
        if not cfg.plesk_url:
            missing.append("Plesk URL")
        if not cfg.plesk_user:
            missing.append("Username")
        if not cfg.plesk_password:
            missing.append("Password")
        if not cfg.domain_id:
            missing.append("Subscription (Domain) ID")
        if not cfg.backup_dir:
            missing.append("Backup destination folder")

        if missing:
            QMessageBox.warning(self, APP_NAME, "Missing fields: " + ", ".join(missing))
            return

        self.cfg = cfg
        self.cfg.save()
        self._set_running_state(True)
        self._log("Scheduler started. Running the first download now.")
        self._run_download()

    def _stop(self) -> None:
        self.timer.stop()
        if self.worker and self.worker.isRunning():
            self._log("Stop requested. Current download will finish first.")
        self._set_running_state(False)
        self.status_lbl.setText("Status: stopped")
        self._log("Scheduler stopped.")

    def _run_download(self) -> None:
        if self.worker and self.worker.isRunning():
            self._log("Previous download is still running; skipping this cycle.")
            self._schedule_next()
            return

        self.status_lbl.setText("Status: download in progress...")
        self.progress_bar.setValue(0)
        self.progress_lbl.setText("")

        self.worker = DownloadWorker(self.cfg)
        self.worker.log.connect(self._log)
        self.worker.finished_ok.connect(self._on_done)
        self.worker.failed.connect(self._on_fail)
        self.worker.progress.connect(self._on_progress)
        self.worker.start()

    @staticmethod
    def _fmt_bytes(n: float) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.2f} {unit}"
            n /= 1024
        return f"{n:.2f} PB"

    @staticmethod
    def _fmt_eta(sec: float) -> str:
        if sec < 0 or sec > 86400 * 7:
            return "--:--:--"
        sec_int = int(sec)
        hours, sec_int = divmod(sec_int, 3600)
        minutes, seconds = divmod(sec_int, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _on_progress(self, done: int, total: int, speed: float, eta: float) -> None:
        if total > 0:
            pct1000 = int(min(1000, done * 1000 / total))
            self.progress_bar.setValue(pct1000)
            self.progress_bar.setFormat(f"{pct1000 / 10:.1f}%")

        self.progress_lbl.setText(
            f"{self._fmt_bytes(done)} / {self._fmt_bytes(total)}   "
            f"speed: {self._fmt_bytes(speed)}/s   ETA: {self._fmt_eta(eta)}"
        )

    def _on_done(self, path: str) -> None:
        self.progress_bar.setValue(1000)
        self.progress_bar.setFormat("100%")
        self._log(f"SUCCESS -> {path}")
        if self.running:
            self._schedule_next()

    def _on_fail(self, err: str) -> None:
        self._log(f"ERROR: {err}")
        if self.running:
            self._schedule_next()

    def _schedule_next(self) -> None:
        secs = PERIODS.get(self.cfg.period, 24 * 3600)
        self.timer.start(secs * 1000)
        next_run_timestamp = datetime.now().timestamp() + secs
        next_run_str = datetime.fromtimestamp(next_run_timestamp).strftime("%Y-%m-%d %H:%M:%S")
        self.status_lbl.setText(f"Status: active | Next run: {next_run_str}")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.running:
            result = QMessageBox.question(
                self,
                APP_NAME,
                "The scheduler is active. Do you want to close the application?",
            )
            if result != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
