import os
import subprocess
import re
from queue import Queue
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import pyqtSignal, QObject, QThread
import boto3
from dotenv import load_dotenv
import requests


# Determinar si se está ejecutando en un entorno empaquetado (ejecutable)

if getattr(sys, "frozen", False):
    # Si es así, ajustar el PATH para incluir la ruta al ejecutable de AWS CLI
    os.environ["PATH"] += os.pathsep + os.path.join(sys._MEIPASS, ".local/bin")

else:
    # De lo contrario, añadir la ruta de instalación habitual (para desarrollo)
    os.environ["PATH"] += os.pathsep + "/home/adentu/.local/bin"

load_dotenv()

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_BUCKET = os.getenv("AWS_BUCKET")

s3_client = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
)

selected_folders = []
total_files = 0

progress_bars = {}
progress_labels = {}
upload_threads = {}

MAX_CONCURRENT_UPLOADS = 5


def check_internet_connection():
    try:
        response = requests.get("https://www.google.com", timeout=5)
        return response.status_code == 200
    except requests.ConnectionError:
        return False


class ProgressWindow(QtWidgets.QWidget):
    cancel_all = pyqtSignal()
    reset_ui = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.initUI()
        self.close_event_handled = False

    def initUI(self):
        self.setWindowTitle("Progreso de Carga")
        self.setMinimumWidth(600)
        self.setMaximumWidth(600)

        barra_altura = 25
        etiqueta_altura = 20
        espacio_entre_barras = 15

        altura_total = (barra_altura + etiqueta_altura + espacio_entre_barras) * 4 * 2

        self.setMinimumHeight(altura_total)
        self.setMaximumHeight(altura_total)

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(15)

        scroll_area = QtWidgets.QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOn)
        scroll_area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        scroll_content = QtWidgets.QWidget()
        self.progress_layout = QtWidgets.QVBoxLayout(scroll_content)
        self.progress_layout.setContentsMargins(5, 5, 5, 5)
        self.progress_layout.setSpacing(15)

        scroll_area.setWidget(scroll_content)
        layout.addWidget(scroll_area)

        self.setLayout(layout)
        self.show()

    def add_progress_ui(self, folder_name, initial_message="Cargando archivos..."):
        group_box = QtWidgets.QGroupBox()
        group_box_layout = QtWidgets.QVBoxLayout()
        group_box_layout.setContentsMargins(5, 5, 5, 5)
        group_box_layout.setSpacing(5)

        folder_label = QtWidgets.QLabel(f"Subiendo Carpeta: {folder_name}", self)
        group_box_layout.addWidget(folder_label)

        barra_layout = QtWidgets.QHBoxLayout()
        barra_layout.setContentsMargins(5, 5, 5, 5)
        barra_layout.setSpacing(5)

        progress_bar = QtWidgets.QProgressBar(self)
        progress_bar.setMaximum(100)
        progress_bar.setValue(100)

        palette = progress_bar.palette()
        palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor("yellow"))
        progress_bar.setPalette(palette)

        barra_layout.addWidget(progress_bar)

        group_box_layout.addLayout(barra_layout)

        progress_label = QtWidgets.QLabel(initial_message, self)
        group_box_layout.addWidget(progress_label)

        group_box.setLayout(group_box_layout)

        self.progress_layout.addWidget(group_box)

        progress_bars[folder_name] = progress_bar
        progress_labels[folder_name] = progress_label

    def update_progress(self, folder_name, value, message):
        if folder_name in progress_bars:
            progress_bars[folder_name].setValue(int(value))
            progress_labels[folder_name].setText(message)
            if value == 100:
                self.set_progress_color(folder_name, "green")
            elif value == 0:
                self.set_progress_color(folder_name, "default")

    def set_progress_color(self, folder_name, color):
        if folder_name in progress_bars:
            progress_bar = progress_bars[folder_name]
            palette = progress_bar.palette()
            if color == "green":
                palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor("green"))
            elif color == "red":
                palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor("red"))
            elif color == "yellow":
                palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor("yellow"))
            elif color == "orange":
                palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor("orange"))
            else:
                palette.setColor(
                    QtGui.QPalette.Highlight,
                    self.style().standardPalette().color(QtGui.QPalette.Highlight),
                )
            progress_bar.setPalette(palette)

    def closeEvent(self, event):
        if not self.close_event_handled:
            self.close_event_handled = True
            reply = QtWidgets.QMessageBox.question(
                self,
                "Confirmación de cierre",
                "Si cierras la ventana, todas las subidas pendientes se cancelarán. ¿Estás seguro de que deseas cerrar?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply == QtWidgets.QMessageBox.Yes:
                self.cancel_all.emit()
                self.reset_ui.emit()
                event.accept()
            else:
                self.close_event_handled = False
                event.ignore()
        else:
            event.ignore()


class UploadWorker(QObject):
    progress_updated = pyqtSignal(str, float, str)
    upload_complete = pyqtSignal(str, bool)
    cancel_signal = pyqtSignal()

    def __init__(self, folder, s3_folder, max_retries=3):
        super().__init__()
        self.folder = folder
        self.s3_folder = s3_folder
        self.is_canceled = False
        self.max_retries = max_retries
        self.cancel_signal.connect(self.cancel_upload)

    def run(self):
        retries = 0
        success = False

        while retries < self.max_retries and not success and not self.is_canceled:
            if not check_internet_connection():
                self.handle_connection_loss()
                retries += 1
                QtCore.QThread.sleep(5)
                continue

            success = self.upload_to_s3()
            if not success:
                retries += 1
                if retries < self.max_retries:
                    self.progress_updated.emit(
                        self.folder, 0, "Error: Fallo en la subida. Reintentando..."
                    )
                    self.handle_connection_loss()
                    QtCore.QThread.sleep(5)
                else:
                    self.progress_updated.emit(
                        self.folder,
                        0,
                        "Error: Fallo en la subida. Máximo número de reintentos alcanzado.",
                    )

        self.upload_complete.emit(self.folder, success)

    def upload_to_s3(self):
        base_folder_name = os.path.basename(self.folder)
        s3_path = f"s3://{AWS_BUCKET}/{self.s3_folder}{base_folder_name}/"
        command = [
            "aws",
            "s3",
            "cp",
            self.folder,
            s3_path,
            "--recursive",
        ]

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True,
        )

        data_transferred_regex = re.compile(
            r"Completed (\d+(\.\d+)?) (KiB|MiB|GiB)/~?(\d+(\.\d+)?) (KiB|MiB|GiB) \((\d+(\.\d+)?) (KiB|MiB|GiB)/s\)"
        )

        while True:
            if self.is_canceled:
                process.terminate()
                return False

            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if line.startswith("Completed"):
                match = data_transferred_regex.search(line)
                if match:
                    completed_data = float(match.group(1))
                    completed_unit = match.group(3)
                    total_data = float(match.group(4))
                    total_unit = match.group(6)
                    speed = float(match.group(7))
                    speed_unit = match.group(8)

                    completed_dataKB, total_dataKB, speedKBps = self.convert_units(
                        completed_data,
                        completed_unit,
                        total_data,
                        total_unit,
                        speed,
                        speed_unit,
                    )

                    if total_dataKB > 0:
                        progress = (completed_dataKB / total_dataKB) * 100

                        remaining_dataKB = total_dataKB - completed_dataKB
                        if speedKBps > 0:
                            time2finish = remaining_dataKB / speedKBps
                        else:
                            time2finish = float("inf")

                        time2finish_str = self.format_time2finish(time2finish)

                        self.progress_updated.emit(
                            base_folder_name,
                            progress,
                            f"{completed_data}{completed_unit} de {total_data}{total_unit} Subidos. Tiempo restante: {time2finish_str}.",
                        )

        stderr = process.stderr.read()
        return process.returncode == 0 and not self.is_canceled

    def convert_units(
        self, completed_data, completed_unit, total_data, total_unit, speed, speed_unit
    ):
        if completed_unit == "MiB":
            completed_dataKB = completed_data * 1024
        elif completed_unit == "GiB":
            completed_dataKB = completed_data * 1024 * 1024
        else:
            completed_dataKB = completed_data

        if total_unit == "MiB":
            total_dataKB = total_data * 1024
        elif total_unit == "GiB":
            total_dataKB = total_data * 1024 * 1024
        else:
            total_dataKB = total_data

        if speed_unit == "MiB":
            speedKBps = speed * 1024
        elif speed_unit == "GiB":
            speedKBps = speed * 1024 * 1024
        else:
            speedKBps = speed

        return completed_dataKB, total_dataKB, speedKBps

    def format_time2finish(self, time2finish):
        if time2finish == float("inf"):
            return "Tiempo desconocido"
        else:
            hours = int(time2finish // 3600)
            minutes = int((time2finish % 3600) // 60)
            seconds = int(time2finish % 60)

            if hours > 0:
                if minutes >= 30:
                    hours += 1
                return f"~{hours}h"
            elif minutes > 0:
                if seconds >= 30:
                    minutes += 1
                return f"~{minutes}min"
            else:
                return f"~{seconds}s"

    def handle_connection_loss(self):
        base_folder_name = os.path.basename(self.folder)
        message = "Se perdió conexión. Esperando a reconectarse para reintentar..."
        self.progress_updated.emit(base_folder_name, 0, message)
        if base_folder_name in progress_bars:
            if base_folder_name in progress_labels:
                progress_labels[base_folder_name].setText(message)
            palette = progress_bars[base_folder_name].palette()
            palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor("orange"))
            progress_bars[base_folder_name].setPalette(palette)

    def cancel_upload(self):
        self.is_canceled = True


class S3FileExplorer(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.current_path = ""
        self.history = [""]
        self.history_index = 0
        self.initUI()

    def initUI(self):
        self.setWindowTitle("Explorador de S3")
        self.setGeometry(100, 100, 800, 600)

        main_layout = QtWidgets.QVBoxLayout(self)

        toolbar = QtWidgets.QHBoxLayout()
        self.back_button = QtWidgets.QPushButton("< Atrás")
        self.back_button.clicked.connect(self.go_back)
        self.back_button.setEnabled(False)

        self.forward_button = QtWidgets.QPushButton("Adelante >")
        self.forward_button.clicked.connect(self.go_forward)
        self.forward_button.setEnabled(False)

        self.home_button = QtWidgets.QPushButton("Home")
        self.home_button.clicked.connect(self.go_home)

        self.refresh_button = QtWidgets.QPushButton("Refrescar")
        self.refresh_button.clicked.connect(self.refresh)

        toolbar.addWidget(self.back_button)
        toolbar.addWidget(self.forward_button)
        toolbar.addWidget(self.home_button)
        toolbar.addWidget(self.refresh_button)

        self.path_edit = QtWidgets.QLineEdit(self)
        self.path_edit.setText(self.current_path)
        self.path_edit.returnPressed.connect(self.navigate_to_path)
        toolbar.addWidget(self.path_edit)

        main_layout.addLayout(toolbar)

        self.tree_view = QtWidgets.QTreeWidget(self)
        self.tree_view.setHeaderLabel("Nombre")
        self.tree_view.itemDoubleClicked.connect(self.on_item_double_clicked)
        main_layout.addWidget(self.tree_view)

        self.setLayout(main_layout)
        self.show()

        self.go_home()

    def navigate_to_path(self):
        path = self.path_edit.text().strip()
        if path != self.current_path:
            self.load_path(path)
            self.history.append(path)
            self.history_index += 1
            self.update_navigation_buttons()

    def load_path(self, path=""):
        self.tree_view.clear()
        self.current_path = path
        self.path_edit.setText(path)

        if path and not path.endswith("/"):
            path += "/"

        try:
            folder_icon = self.style().standardIcon(QtWidgets.QStyle.SP_DirIcon)
            file_icon = self.style().standardIcon(QtWidgets.QStyle.SP_FileIcon)

            paginator = s3_client.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=AWS_BUCKET, Prefix=path, Delimiter="/"
            ):
                for folder in page.get("CommonPrefixes", []):
                    folder_name = folder["Prefix"][len(path) :].strip("/")
                    folder_item = QtWidgets.QTreeWidgetItem(
                        self.tree_view, [folder_name]
                    )
                    folder_item.setIcon(0, folder_icon)
                    folder_item.setData(0, QtCore.Qt.UserRole, folder["Prefix"])

                for obj in page.get("Contents", []):
                    file_name = obj["Key"][len(path) :]
                    if file_name and "/" not in file_name:
                        file_item = QtWidgets.QTreeWidgetItem(
                            self.tree_view, [file_name]
                        )
                        file_item.setIcon(0, file_icon)
                        file_item.setData(0, QtCore.Qt.UserRole, obj["Key"])
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Error", str(e))

    def on_item_double_clicked(self, item, column):
        item_data = item.data(0, QtCore.Qt.UserRole)
        if item_data.endswith("/"):
            self.load_path(item_data)
            self.history.append(item_data)
            self.history_index += 1
            self.update_navigation_buttons()

    def go_back(self):
        if self.history_index > 0:
            self.history_index -= 1
            self.load_path(self.history[self.history_index])
            self.update_navigation_buttons()

    def go_forward(self):
        if self.history_index < len(self.history) - 1:
            self.history_index += 1
            self.load_path(self.history[self.history_index])
            self.update_navigation_buttons()

    def go_home(self):
        self.load_path("")
        self.history = [""]
        self.history_index = 0
        self.update_navigation_buttons()

    def refresh(self):
        self.load_path(self.current_path)

    def update_navigation_buttons(self):
        self.back_button.setEnabled(self.history_index > 0)
        self.forward_button.setEnabled(self.history_index < len(self.history) - 1)


class S3UploaderApp(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.initUI()
        self.upload_queue = Queue()
        self.active_uploads = 0
        self.progress_window = None
        self.close_event_handled = False
        self.last_selected_folder = "/mnt/e/Stuff/Adentu/Imagenes/MEM/SS01"

    def initUI(self):
        self.setWindowTitle("Subir Carpetas a S3")
        self.setMinimumWidth(700)
        main_layout = QtWidgets.QVBoxLayout()

        self.selected_folder_label = QtWidgets.QLabel("Carpetas seleccionadas:", self)
        self.selected_folder_label.setFont(QtGui.QFont("Arial", 13))
        main_layout.addWidget(self.selected_folder_label)

        folder_layout = QtWidgets.QHBoxLayout()

        self.file_list = QtWidgets.QListWidget(self)
        folder_layout.addWidget(self.file_list)

        btn_fldr_layout = QtWidgets.QVBoxLayout()
        self.select_folder_button = QtWidgets.QPushButton("Seleccionar", self)
        self.select_folder_button.clicked.connect(self.select_folder)
        btn_fldr_layout.addWidget(self.select_folder_button)

        self.delete_folder_button = QtWidgets.QPushButton("Eliminar", self)
        self.delete_folder_button.clicked.connect(self.delete_selected_folders)
        btn_fldr_layout.addWidget(self.delete_folder_button)

        folder_layout.addLayout(btn_fldr_layout)

        main_layout.addLayout(folder_layout)

        self.s3_folder_info_label = QtWidgets.QLabel("Selecciona destino en S3:", self)
        self.s3_folder_info_label.setFont(QtGui.QFont("Arial", 13))
        main_layout.addWidget(self.s3_folder_info_label)

        s3_folder_layout = QtWidgets.QHBoxLayout()

        self.s3_folder_combobox = QtWidgets.QComboBox(self)
        self.update_s3_folder_combobox()
        self.s3_folder_combobox.currentIndexChanged.connect(self.on_s3_folder_selected)
        s3_folder_layout.addWidget(self.s3_folder_combobox)

        self.create_folder_button = QtWidgets.QPushButton("Crear carpeta", self)
        self.create_folder_button.clicked.connect(self.create_new_s3_folder)
        s3_folder_layout.addWidget(self.create_folder_button)

        main_layout.addLayout(s3_folder_layout)

        btn_layout = QtWidgets.QHBoxLayout()

        self.upload_button = QtWidgets.QPushButton("Subir Carpetas", self)
        self.upload_button.setEnabled(False)
        self.upload_button.clicked.connect(self.upload_folder)
        btn_layout.addWidget(self.upload_button)

        self.s3_dirView = QtWidgets.QPushButton("Ver directorio S3", self)
        self.s3_dirView.clicked.connect(self.show_s3_directory)
        btn_layout.addWidget(self.s3_dirView)

        main_layout.addLayout(btn_layout)

        self.result_list = QtWidgets.QListWidget(self)
        main_layout.addWidget(self.result_list)

        self.setLayout(main_layout)
        self.show()

    def show_s3_directory(self):
        self.s3_dir_view_window = S3FileExplorer()
        self.s3_dir_view_window.show()

    def closeEvent(self, event):
        if not self.close_event_handled:
            self.close_event_handled = True
            reply = QtWidgets.QMessageBox.question(
                self,
                "Confirmación de cierre",
                "Si cierras la ventana, todas las subidas pendientes se cancelarán. ¿Estás seguro de que deseas cerrar?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply == QtWidgets.QMessageBox.Yes:
                self.cancel_all_uploads()
                if self.progress_window:
                    self.progress_window.close_event_handled = True
                    self.progress_window.close()
                event.accept()
            else:
                self.close_event_handled = False
                event.ignore()
        else:
            event.ignore()

    def select_folder(self):
        global selected_folders, total_files
        initial_dir = self.last_selected_folder
        selected_folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Selecciona una carpeta", initial_dir
        )
        if selected_folder:
            self.last_selected_folder = selected_folder
            selected_folders.append(selected_folder)
            self.file_list.addItem(selected_folder)
            self.result_list.addItem(f"Seleccionada la carpeta: {selected_folder}")
            self.result_list.addItem("Contando archivos...")
            self.result_list.scrollToBottom()
            total_files += sum(len(files) for _, _, files in os.walk(selected_folder))
            self.result_list.addItem(
                f"Se han detectado {total_files} archivos en total por subir de la carpeta {os.path.basename(selected_folder)}."
            )
            self.result_list.scrollToBottom()

            for rootf, dirs, files in os.walk(selected_folder):
                folder_name = os.path.basename(rootf)
                for file_name in files:
                    base, ext = os.path.splitext(file_name)
                    if not base.endswith(f"_{folder_name}"):
                        new_file_name = f"{base}_{folder_name}{ext}"
                        old_path = os.path.join(rootf, file_name)
                        new_path = os.path.join(rootf, new_file_name)
                        os.rename(old_path, new_path)

        self.update_upload_button_state()

    def delete_selected_folders(self):
        selected_items = self.file_list.selectedItems()
        if not selected_items:
            return

        for item in selected_items:
            folder_path = item.text()
            selected_folders.remove(folder_path)
            self.file_list.takeItem(self.file_list.row(item))

        self.update_upload_button_state()

    def list_s3_folders(self, bucket_name):
        folders = []
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket_name, Delimiter="/"):
            for prefix in page.get("CommonPrefixes", []):
                folders.append(prefix.get("Prefix"))
        return folders

    def update_s3_folder_combobox(self):
        folders = self.list_s3_folders(AWS_BUCKET)
        self.s3_folder_combobox.clear()
        self.s3_folder_combobox.addItems(folders)
        if folders:
            self.s3_folder_combobox.setCurrentIndex(0)

    def on_s3_folder_selected(self):
        s3_folder = self.s3_folder_combobox.currentText()
        if s3_folder:
            self.result_list.addItem(
                f"Carpeta S3 seleccionada: s3://{AWS_BUCKET}/{s3_folder}"
            )
        else:
            self.result_list.addItem("No se seleccionó ninguna carpeta de S3.")
        self.result_list.scrollToBottom()

    def create_new_s3_folder(self):
        new_folder_name, ok = QtWidgets.QInputDialog.getText(
            self, "Nueva carpeta", "Ingrese el nombre de la nueva carpeta:"
        )
        if ok and new_folder_name:
            if not new_folder_name.endswith("/"):
                new_folder_name += "/"
            try:
                s3_client.put_object(Bucket=AWS_BUCKET, Key=new_folder_name)
                self.result_list.addItem(
                    f"Carpeta creada: s3://{AWS_BUCKET}/{new_folder_name}"
                )
                self.update_s3_folder_combobox()
            except Exception as e:
                self.result_list.addItem(f"Error al crear carpeta: {str(e)}")
            self.result_list.scrollToBottom()

    def upload_folder(self):
        global progress_bars, progress_labels, upload_threads

        s3_folder = self.s3_folder_combobox.currentText()
        self.result_list.addItem(f"Directorio de destino s3://{AWS_BUCKET}/{s3_folder}")

        self.file_list.clear()

        if not self.progress_window or not self.progress_window.isVisible():
            self.progress_window = ProgressWindow()
            self.progress_window.cancel_all.connect(self.cancel_all_uploads)
            self.progress_window.reset_ui.connect(self.reset_ui_state)
            self.progress_window.show()

            progress_bars.clear()
            progress_labels.clear()

        for folder in selected_folders:
            base_folder_name = os.path.basename(folder)
            if base_folder_name not in progress_bars:
                self.progress_window.add_progress_ui(
                    base_folder_name, "Carpeta en cola"
                )
                self.upload_queue.put((folder, s3_folder))

        self.start_next_uploads()

    def start_next_uploads(self):
        while (
            self.active_uploads < MAX_CONCURRENT_UPLOADS
            and not self.upload_queue.empty()
        ):
            folder, s3_folder = self.upload_queue.get()
            base_folder_name = os.path.basename(folder)

            self.progress_window.update_progress(base_folder_name, 0, "Iniciando...")
            self.progress_window.set_progress_color(base_folder_name, "default")

            worker = UploadWorker(folder, s3_folder)
            worker_thread = QThread()
            worker.moveToThread(worker_thread)

            worker.progress_updated.connect(self.progress_window.update_progress)
            worker.upload_complete.connect(self.on_upload_complete)

            worker_thread.started.connect(worker.run)
            worker_thread.start()
            upload_threads[folder] = (worker, worker_thread)

            self.active_uploads += 1

    def on_upload_complete(self, folder, success):
        base_folder_name = os.path.basename(folder)
        if success:
            self.result_list.addItem(f"La carpeta {folder} se ha subido exitosamente.")
            if base_folder_name in progress_bars:
                self.progress_window.update_progress(
                    base_folder_name, 100, "Carpeta subida con éxito."
                )
                self.progress_window.set_progress_color(base_folder_name, "green")
        else:
            self.result_list.addItem(f"Error al subir la carpeta {folder}.")
            if base_folder_name in progress_bars:
                self.progress_window.set_progress_color(base_folder_name, "red")
            if base_folder_name in progress_labels:
                progress_labels[base_folder_name].setText(
                    "Error al subir la carpeta o problema de conexión."
                )

        self.result_list.scrollToBottom()

        self.active_uploads -= 1
        self.start_next_uploads()

    def cancel_all_uploads(self):
        global upload_threads

        for folder, (worker, worker_thread) in upload_threads.items():
            worker.cancel_signal.emit()
            worker_thread.quit()
            worker_thread.wait()

        upload_threads.clear()
        self.result_list.addItem("Todas las cargas pendientes han sido canceladas.")
        self.upload_queue.queue.clear()

        if self.progress_window:
            self.progress_window.close()
            self.progress_window = None

    def update_upload_button_state(self):
        if selected_folders and self.s3_folder_combobox.currentIndex() != -1:
            self.upload_button.setEnabled(True)
        else:
            self.upload_button.setEnabled(False)

    def reset_ui_state(self):
        global selected_folders, total_files, progress_bars, progress_labels, upload_threads

        selected_folders = []
        total_files = 0
        progress_bars = {}
        progress_labels = {}
        upload_threads = {}

        self.file_list.clear()
        self.select_folder_button.setEnabled(True)
        self.create_folder_button.setEnabled(True)
        self.upload_button.setEnabled(False)


if __name__ == "__main__":
    app = QtWidgets.QApplication([])
    uploader = S3UploaderApp()
    app.exec_()
