import sys
import os
import re
import csv
from pathlib import Path
from pydub import AudioSegment

from PySide6.QtCore import Qt, QThread, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QLineEdit, QPushButton, QCheckBox, QSpinBox, 
    QTableWidget, QTableWidgetItem, QHeaderView, QProgressBar,
    QTextEdit, QMessageBox, QFileDialog, QRadioButton, QButtonGroup,
    QSplitter
)

SUPPORTED_EXTENSIONS = {".wav", ".flac", ".mp3", ".ogg", ".aiff", ".aif"}

def human_size(size):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"

class ScannerWorker(QThread):
    progress_update = Signal(int, int) # current, total
    log_message = Signal(str)
    scan_complete = Signal(dict)
    scan_error = Signal(str)

    def __init__(self, project_folder, media_folder, options):
        super().__init__()
        self.project_folder = Path(project_folder)
        self.media_folder = Path(media_folder)
        self.options = options
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        try:
            self.log_message.emit("Scanning REAPER project files...")
            rpp_files = self.find_rpp_files(self.project_folder)
            self.log_message.emit(f"Found {len(rpp_files)} .rpp files.")
            
            referenced_media = set()
            total_rpp = len(rpp_files)
            for i, rpp in enumerate(rpp_files):
                if self._is_cancelled:
                    self.log_message.emit("Scan cancelled.")
                    return
                referenced_media.update(self.extract_media_from_rpp(rpp))
                self.progress_update.emit(i + 1, total_rpp)
                
            self.log_message.emit(f"Referenced media files extracted: {len(referenced_media)}")
            self.log_message.emit("Scanning media directory...")
            
            media_files = self.find_media_files(self.media_folder)
            self.log_message.emit(f"Found {len(media_files)} media files.")
            
            unused_files = []
            for media in media_files:
                if self._is_cancelled:
                    self.log_message.emit("Scan cancelled.")
                    return
                if media.resolve() not in referenced_media:
                    unused_files.append(media.resolve())
            
            self.log_message.emit(f"Found {len(unused_files)} unused files.")
            
            silent_files = set()
            if self.options.get("detect_silent", False):
                self.log_message.emit("Checking for silent recordings...")
                total_unused = len(unused_files)
                threshold = self.options.get("silence_threshold", -60)
                min_duration = self.options.get("min_duration", 1000)
                for i, file in enumerate(unused_files):
                    if self._is_cancelled:
                        self.log_message.emit("Scan cancelled.")
                        return
                    if self.is_silent(file, threshold, min_duration):
                        silent_files.add(file)
                    self.progress_update.emit(i + 1, total_unused)
                    
            results = {
                "rpp_count": len(rpp_files),
                "referenced_count": len(referenced_media),
                "media_count": len(media_files),
                "unused_files": unused_files,
                "silent_files": silent_files,
            }
            self.log_message.emit("Scan complete.")
            self.scan_complete.emit(results)
            
        except Exception as e:
            self.scan_error.emit(f"Error during scan: {str(e)}")

    def find_rpp_files(self, root_folder):
        rpp_files = []
        for root, _, files in os.walk(root_folder):
            for file in files:
                if file.lower().endswith(".rpp"):
                    rpp_files.append(Path(root) / file)
        return rpp_files

    def extract_media_from_rpp(self, rpp_path):
        media_files = set()
        try:
            text = rpp_path.read_text(errors="ignore")
        except Exception as e:
            self.log_message.emit(f"Could not read {rpp_path}: {e}")
            return media_files
            
        matches = re.findall(r'FILE\s+"(.+?)"', text)
        for match in matches:
            media_path = Path(match)
            if not media_path.is_absolute():
                media_path = (rpp_path.parent / media_path).resolve()
            media_files.add(media_path.resolve())
        return media_files

    def find_media_files(self, root_folder):
        media_files = []
        for root, _, files in os.walk(root_folder):
            for file in files:
                path = (Path(root) / file).resolve()
                if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                    media_files.append(path)
        return media_files

    def is_silent(self, audio_path, threshold, min_duration):
        try:
            audio = AudioSegment.from_file(audio_path)
            if len(audio) < min_duration:
                return False
            return audio.dBFS <= threshold
        except Exception as e:
            self.log_message.emit(f"Could not analyze {audio_path}: {e}")
            return False

class DeletionWorker(QThread):
    progress_update = Signal(int, int)
    log_message = Signal(str)
    delete_complete = Signal(int, int) # deleted, failed

    def __init__(self, files_to_delete, use_recycle_bin):
        super().__init__()
        self.files = files_to_delete
        self.use_recycle_bin = use_recycle_bin
        self._is_cancelled = False
        
    def cancel(self):
        self._is_cancelled = True
        
    def run(self):
        deleted = 0
        failed = 0
        total = len(self.files)
        
        if self.use_recycle_bin:
            try:
                from send2trash import send2trash
            except ImportError:
                self.log_message.emit("send2trash not installed. Falling back to permanent deletion.")
                self.use_recycle_bin = False
                
        for i, file in enumerate(self.files):
            if self._is_cancelled:
                self.log_message.emit("Deletion cancelled.")
                break
                
            try:
                if self.use_recycle_bin:
                    send2trash(str(file))
                else:
                    file.unlink()
                deleted += 1
            except Exception as e:
                failed += 1
                self.log_message.emit(f"Failed deleting {file}: {e}")
                
            self.progress_update.emit(i + 1, total)
            
        self.log_message.emit("Deletion complete.")
        self.delete_complete.emit(deleted, failed)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("REAPER Media Cleaner")
        self.setMinimumSize(1100, 750)
        self.setup_ui()
        self.apply_styles()
        
        self.scanner = None
        self.deleter = None
        self.unused_files = []
        self.silent_files = set()
        self.report_path = Path("unused_reaper_media.txt").resolve()
        
    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        
        # --- Folders Group ---
        folder_group = QGroupBox("Folders")
        folder_layout = QVBoxLayout()
        
        # Project Folder
        proj_layout = QHBoxLayout()
        proj_layout.addWidget(QLabel("REAPER Project Folder:"))
        self.proj_input = QLineEdit()
        self.proj_input.setPlaceholderText("Select folder containing .rpp files (Recursive)...")
        proj_layout.addWidget(self.proj_input)
        proj_btn = QPushButton("Browse...")
        proj_btn.clicked.connect(self.browse_project)
        proj_layout.addWidget(proj_btn)
        folder_layout.addLayout(proj_layout)
        
        # Media Folder
        media_layout = QHBoxLayout()
        media_layout.addWidget(QLabel("Media/Junk Folder:"))
        self.media_input = QLineEdit()
        self.media_input.setText(r"C:\Users\beher\Documents\REAPER Media")
        media_layout.addWidget(self.media_input)
        media_btn = QPushButton("Browse...")
        media_btn.clicked.connect(self.browse_media)
        media_layout.addWidget(media_btn)
        folder_layout.addLayout(media_layout)
        
        folder_group.setLayout(folder_layout)
        main_layout.addWidget(folder_group)
        
        # --- Splitter for Options and Results/Table ---
        splitter = QSplitter(Qt.Horizontal)
        
        # --- Options Group ---
        options_group = QGroupBox("Settings")
        options_layout = QVBoxLayout()
        
        self.detect_silent_cb = QCheckBox("Detect silent recordings")
        self.detect_silent_cb.setChecked(True)
        options_layout.addWidget(self.detect_silent_cb)
        
        silent_settings_layout = QHBoxLayout()
        silent_settings_layout.addWidget(QLabel("Silence threshold (dBFS):"))
        self.threshold_spin = QSpinBox()
        self.threshold_spin.setRange(-100, 0)
        self.threshold_spin.setValue(-60)
        silent_settings_layout.addWidget(self.threshold_spin)
        
        silent_settings_layout.addWidget(QLabel("Min duration (ms):"))
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(0, 10000)
        self.duration_spin.setValue(1000)
        silent_settings_layout.addWidget(self.duration_spin)
        
        options_layout.addLayout(silent_settings_layout)
        
        # Delete mode
        self.use_recycle_radio = QRadioButton("Move deleted files to Recycle Bin")
        self.use_recycle_radio.setChecked(True)
        self.permanent_radio = QRadioButton("Permanent delete mode")
        
        self.delete_mode_group = QButtonGroup()
        self.delete_mode_group.addButton(self.use_recycle_radio)
        self.delete_mode_group.addButton(self.permanent_radio)
        
        options_layout.addWidget(self.use_recycle_radio)
        options_layout.addWidget(self.permanent_radio)
        
        self.gen_report_cb = QCheckBox("Generate report file after scan")
        self.gen_report_cb.setChecked(True)
        options_layout.addWidget(self.gen_report_cb)
        
        self.dry_run_cb = QCheckBox("Dry-run mode (scan only, disables deletion)")
        self.dry_run_cb.stateChanged.connect(self.on_dry_run_changed)
        options_layout.addWidget(self.dry_run_cb)
        
        options_layout.addStretch()
        options_group.setLayout(options_layout)
        splitter.addWidget(options_group)
        
        # --- Results / Actions Group ---
        results_group = QGroupBox("Scan Results")
        results_layout = QVBoxLayout()
        
        self.lbl_rpp = QLabel("RPP files found: 0")
        self.lbl_ref = QLabel("Referenced media: 0")
        self.lbl_scanned = QLabel("Media files scanned: 0")
        self.lbl_unused = QLabel("Unused files: 0")
        self.lbl_silent = QLabel("Silent files: 0")
        self.lbl_reclaim = QLabel("Reclaimable space: 0 B")
        
        stats_layout = QVBoxLayout()
        stats_layout.addWidget(self.lbl_rpp)
        stats_layout.addWidget(self.lbl_ref)
        stats_layout.addWidget(self.lbl_scanned)
        stats_layout.addWidget(self.lbl_unused)
        stats_layout.addWidget(self.lbl_silent)
        stats_layout.addWidget(self.lbl_reclaim)
        
        results_layout.addLayout(stats_layout)
        
        # Action Buttons
        actions_layout = QHBoxLayout()
        self.btn_scan = QPushButton("Scan")
        self.btn_scan.clicked.connect(self.start_scan)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.cancel_scan)
        self.btn_cancel.setEnabled(False)
        self.btn_delete = QPushButton("Delete Unused Files")
        self.btn_delete.clicked.connect(self.delete_files)
        self.btn_delete.setEnabled(False)
        self.btn_export = QPushButton("Export Report")
        self.btn_export.clicked.connect(self.export_report)
        self.btn_export.setEnabled(False)
        self.btn_open_report = QPushButton("Open Report")
        self.btn_open_report.clicked.connect(self.open_report)
        self.btn_open_report.setEnabled(False)
        
        actions_layout.addWidget(self.btn_scan)
        actions_layout.addWidget(self.btn_cancel)
        actions_layout.addWidget(self.btn_delete)
        actions_layout.addWidget(self.btn_export)
        actions_layout.addWidget(self.btn_open_report)
        
        results_layout.addLayout(actions_layout)
        results_group.setLayout(results_layout)
        splitter.addWidget(results_group)
        
        splitter.setSizes([450, 650])
        main_layout.addWidget(splitter)
        
        # --- Table Section ---
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["File Path", "Size", "Silent"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setSortingEnabled(True)
        main_layout.addWidget(self.table)
        
        # --- Log & Progress ---
        log_group = QGroupBox("Activity")
        log_layout = QVBoxLayout()
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumHeight(120)
        log_layout.addWidget(self.log_output)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        log_layout.addWidget(self.progress_bar)
        log_group.setLayout(log_layout)
        main_layout.addWidget(log_group)
        
    def apply_styles(self):
        self.setStyleSheet('''
            QMainWindow {
                background-color: #1e1e1e;
                color: #ffffff;
            }
            QGroupBox {
                border: 1px solid #333333;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 15px;
                color: #ffffff;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 3px;
            }
            QLabel, QCheckBox, QRadioButton {
                color: #cccccc;
            }
            QLineEdit, QSpinBox {
                background-color: #2d2d2d;
                border: 1px solid #444444;
                color: #ffffff;
                padding: 4px;
                border-radius: 3px;
            }
            QPushButton {
                background-color: #007acc;
                border: none;
                color: white;
                padding: 6px 12px;
                border-radius: 3px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #0098ff;
            }
            QPushButton:pressed {
                background-color: #005f9e;
            }
            QPushButton:disabled {
                background-color: #444444;
                color: #888888;
            }
            QTableWidget {
                background-color: #1e1e1e;
                color: #ffffff;
                gridline-color: #333333;
                border: 1px solid #333333;
            }
            QHeaderView::section {
                background-color: #2d2d2d;
                color: #ffffff;
                padding: 4px;
                border: 1px solid #333333;
            }
            QTextEdit {
                background-color: #121212;
                color: #00ff00;
                font-family: Consolas, monospace;
            }
            QProgressBar {
                border: 1px solid #444444;
                border-radius: 3px;
                text-align: center;
                color: white;
            }
            QProgressBar::chunk {
                background-color: #007acc;
                width: 10px;
            }
        ''')

    def browse_project(self):
        dir_path = QFileDialog.getExistingDirectory(self, "Select REAPER Project Folder")
        if dir_path:
            self.proj_input.setText(dir_path)

    def browse_media(self):
        dir_path = QFileDialog.getExistingDirectory(self, "Select Media Folder")
        if dir_path:
            self.media_input.setText(dir_path)
            
    def on_dry_run_changed(self):
        if self.dry_run_cb.isChecked():
            self.btn_delete.setEnabled(False)
        else:
            if self.unused_files:
                self.btn_delete.setEnabled(True)
                
    def log(self, message):
        self.log_output.append(message)
        # Auto-scroll
        scrollbar = self.log_output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        
    def start_scan(self):
        proj_dir = self.proj_input.text().strip()
        media_dir = self.media_input.text().strip()
        
        if not proj_dir or not os.path.isdir(proj_dir):
            QMessageBox.warning(self, "Error", "Please select a valid REAPER project folder.")
            return
            
        if not media_dir or not os.path.isdir(media_dir):
            QMessageBox.warning(self, "Error", "Please select a valid Media folder.")
            return
            
        options = {
            "detect_silent": self.detect_silent_cb.isChecked(),
            "silence_threshold": self.threshold_spin.value(),
            "min_duration": self.duration_spin.value(),
        }
        
        self.btn_scan.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.btn_delete.setEnabled(False)
        self.btn_export.setEnabled(False)
        
        self.log_output.clear()
        self.progress_bar.setValue(0)
        self.table.setRowCount(0)
        
        self.scanner = ScannerWorker(proj_dir, media_dir, options)
        self.scanner.progress_update.connect(self.update_progress)
        self.scanner.log_message.connect(self.log)
        self.scanner.scan_complete.connect(self.on_scan_complete)
        self.scanner.scan_error.connect(self.on_scan_error)
        self.scanner.start()
        
    def cancel_scan(self):
        if self.scanner and self.scanner.isRunning():
            self.scanner.cancel()
        if self.deleter and self.deleter.isRunning():
            self.deleter.cancel()
            
    def update_progress(self, current, total):
        if total > 0:
            val = int((current / total) * 100)
            self.progress_bar.setValue(val)
            
    def on_scan_complete(self, results):
        self.btn_scan.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        
        self.unused_files = results["unused_files"]
        self.silent_files = results["silent_files"]
        
        total_size = sum(f.stat().st_size for f in self.unused_files if f.exists())
        
        self.lbl_rpp.setText(f"RPP files found: {results['rpp_count']}")
        self.lbl_ref.setText(f"Referenced media: {results['referenced_count']}")
        self.lbl_scanned.setText(f"Media files scanned: {results['media_count']}")
        self.lbl_unused.setText(f"Unused files: {len(self.unused_files)}")
        self.lbl_silent.setText(f"Silent files: {len(self.silent_files)}")
        self.lbl_reclaim.setText(f"Reclaimable space: {human_size(total_size)}")
        
        self.populate_table()
        
        if self.unused_files:
            if not self.dry_run_cb.isChecked():
                self.btn_delete.setEnabled(True)
            self.btn_export.setEnabled(True)
            
        if self.gen_report_cb.isChecked() and self.unused_files:
            self.generate_report_file(self.report_path)
            
        QMessageBox.information(self, "Scan Complete", "Scan completed successfully.")
        
    def on_scan_error(self, error):
        self.btn_scan.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        QMessageBox.critical(self, "Error", error)
        
    def populate_table(self):
        self.table.setRowCount(0)
        self.table.setSortingEnabled(False)
        for row, file in enumerate(self.unused_files):
            self.table.insertRow(row)
            path_item = QTableWidgetItem(str(file))
            
            try:
                size = file.stat().st_size
                size_str = human_size(size)
                # Store numeric size for sorting (using Qt.UserRole)
                size_item = QTableWidgetItem(size_str)
                size_item.setData(Qt.UserRole, size)
            except:
                size_item = QTableWidgetItem("Unknown")
                size_item.setData(Qt.UserRole, 0)
                
            is_silent = file in self.silent_files
            silent_item = QTableWidgetItem("Yes" if is_silent else "No")
            
            self.table.setItem(row, 0, path_item)
            self.table.setItem(row, 1, size_item)
            self.table.setItem(row, 2, silent_item)
            
        self.table.setSortingEnabled(True)
        
    def delete_files(self):
        if not self.unused_files:
            return
            
        if self.dry_run_cb.isChecked():
            QMessageBox.information(self, "Dry Run", "Dry run mode is enabled. Deletion is disabled.")
            return
            
        mode = "to Recycle Bin" if self.use_recycle_radio.isChecked() else "Permanently"
        reply = QMessageBox.question(self, 'Confirm Deletion', 
                                     f'Are you sure you want to delete {len(self.unused_files)} unused files {mode}?',
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                                     
        if reply == QMessageBox.Yes:
            self.btn_delete.setEnabled(False)
            self.btn_scan.setEnabled(False)
            self.btn_cancel.setEnabled(True)
            self.btn_export.setEnabled(False)
            self.progress_bar.setValue(0)
            self.log_output.clear()
            
            self.deleter = DeletionWorker(self.unused_files, self.use_recycle_radio.isChecked())
            self.deleter.progress_update.connect(self.update_progress)
            self.deleter.log_message.connect(self.log)
            self.deleter.delete_complete.connect(self.on_delete_complete)
            self.deleter.start()
            
    def on_delete_complete(self, deleted, failed):
        self.btn_scan.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.btn_delete.setEnabled(False)
        
        QMessageBox.information(self, "Deletion Complete", 
                                f"Deleted: {deleted}\nFailed: {failed}")
        self.table.setRowCount(0)
        self.unused_files.clear()
        self.silent_files.clear()
        
        self.lbl_unused.setText("Unused files: 0")
        self.lbl_silent.setText("Silent files: 0")
        self.lbl_reclaim.setText("Reclaimable space: 0 B")

    def generate_report_file(self, path):
        try:
            with path.open("w", encoding="utf-8") as f:
                f.write("UNUSED FILES\n")
                f.write("===================================\n\n")
                for file in self.unused_files:
                    f.write(str(file) + "\n")

                f.write("\n\nSILENT FILES\n")
                f.write("===================================\n\n")
                for file in self.silent_files:
                    f.write(str(file) + "\n")
            
            self.log(f"Report saved to: {path}")
            self.btn_open_report.setEnabled(True)
            self.report_path = path
        except Exception as e:
            self.log(f"Failed to generate report: {e}")
            
    def export_report(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export Report", "reaper_cleaner_report.csv", "CSV Files (*.csv);;Text Files (*.txt)")
        if path:
            p = Path(path)
            if p.suffix == '.csv':
                try:
                    with p.open("w", newline='', encoding="utf-8") as f:
                        writer = csv.writer(f)
                        writer.writerow(["File Path", "Size (Bytes)", "Is Silent"])
                        for file in self.unused_files:
                            try:
                                size = file.stat().st_size
                            except:
                                size = 0
                            writer.writerow([str(file), size, "Yes" if file in self.silent_files else "No"])
                    self.log(f"CSV exported to: {path}")
                except Exception as e:
                    self.log(f"Export failed: {e}")
            else:
                self.generate_report_file(p)

    def open_report(self):
        if self.report_path and self.report_path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.report_path)))

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
