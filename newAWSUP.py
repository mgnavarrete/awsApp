import os
import subprocess
import re
from queue import Queue
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import pyqtSignal, QObject, QThread
import boto3
from dotenv import load_dotenv

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


class ProgressWindow(QtWidgets.QWidget):
    cancel_all = pyqtSignal()  # Signal to cancel all uploads
    reset_ui = pyqtSignal()  # Nueva señal para resetear la UI

    def __init__(self):
        super().__init__()
        self.initUI()
        self.close_event_handled = False  # Variable para manejar una única confirmación

    def initUI(self):
        self.setWindowTitle("Progreso de Carga")
        self.setMinimumWidth(600)
        self.setMaximumWidth(600)

        # Calcula la altura para 4 barras de progreso
        barra_altura = 25  # Altura aproximada de cada QProgressBar
        etiqueta_altura = 20  # Altura aproximada de cada QLabel
        espacio_entre_barras = 15  # Espacio aproximado entre cada barra de progreso

        # Calcula la altura total para 4 barras de progreso
        altura_total = (barra_altura + etiqueta_altura + espacio_entre_barras) * 4 * 2

        # Establece la altura máxima de la ventana
        self.setMinimumHeight(altura_total)
        self.setMaximumHeight(altura_total)

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(15)

        # Crear el área de desplazamiento
        scroll_area = QtWidgets.QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOn)
        scroll_area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        # Crear un contenedor para el contenido desplazable
        scroll_content = QtWidgets.QWidget()
        self.progress_layout = QtWidgets.QVBoxLayout(scroll_content)
        self.progress_layout.setContentsMargins(5, 5, 5, 5)
        self.progress_layout.setSpacing(15)

        # Configurar el área de desplazamiento
        scroll_area.setWidget(scroll_content)
        layout.addWidget(scroll_area)

        self.setLayout(layout)
        self.show()

    def add_progress_ui(self, folder_name, initial_message="Cargando archivos..."):
        # Crear un QGroupBox para encapsular los elementos de la carpeta
        group_box = QtWidgets.QGroupBox()
        group_box_layout = QtWidgets.QVBoxLayout()
        group_box_layout.setContentsMargins(5, 5, 5, 5)
        group_box_layout.setSpacing(5)

        # Etiqueta para mostrar el nombre de la carpeta
        folder_label = QtWidgets.QLabel(f"Subiendo Carpeta: {folder_name}", self)
        group_box_layout.addWidget(folder_label)

        # Layout horizontal para la barra de progreso
        barra_layout = QtWidgets.QHBoxLayout()
        barra_layout.setContentsMargins(5, 5, 5, 5)
        barra_layout.setSpacing(5)

        progress_bar = QtWidgets.QProgressBar(self)
        progress_bar.setMaximum(100)
        progress_bar.setValue(100)  # Set progress to 100% while in queue

        # Establecer el color de la barra en amarillo
        palette = progress_bar.palette()
        palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor("yellow"))
        progress_bar.setPalette(palette)

        barra_layout.addWidget(progress_bar)

        group_box_layout.addLayout(barra_layout)

        # Etiqueta para mostrar el mensaje de progreso
        progress_label = QtWidgets.QLabel(initial_message, self)
        group_box_layout.addWidget(progress_label)

        # Establecer el layout en el QGroupBox
        group_box.setLayout(group_box_layout)

        # Añadir el QGroupBox al layout principal de progreso
        self.progress_layout.addWidget(group_box)

        # Almacenar referencias para futuras actualizaciones
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
                self.cancel_all.emit()  # Emitir señal para cancelar todas las subidas
                self.reset_ui.emit()  # Emitir señal para resetear la UI
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

    def __init__(self, folder, s3_folder):
        super().__init__()
        self.folder = folder
        self.s3_folder = s3_folder
        self.is_canceled = False
        self.cancel_signal.connect(self.cancel_upload)

    def run(self):
        if self.is_canceled:
            return

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
                break

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

                    if total_dataKB > 0:
                        progress = (completed_dataKB / total_dataKB) * 100

                        remaining_dataKB = total_dataKB - completed_dataKB
                        if speedKBps > 0:
                            time2finish = remaining_dataKB / speedKBps
                        else:
                            time2finish = float("inf")

                        if time2finish == float("inf"):
                            time2finish_str = "Tiempo desconocido"
                        else:
                            hours = int(time2finish // 3600)
                            minutes = int((time2finish % 3600) // 60)
                            seconds = int(time2finish % 60)

                            if hours > 0:
                                if minutes >= 30:
                                    hours += 1
                                time2finish_str = f"~{hours}h"
                            elif minutes > 0:
                                if seconds >= 30:
                                    minutes += 1
                                time2finish_str = f"~{minutes}min"
                            else:
                                time2finish_str = f"~{seconds}s"

                        self.progress_updated.emit(
                            base_folder_name,
                            progress,
                            f"{completed_data}{completed_unit} de {total_data}{total_unit} Subidos. Tiempo restante: {time2finish_str}.",
                        )

        stderr = process.stderr.read()
        success = process.returncode == 0 and not self.is_canceled
        self.upload_complete.emit(self.folder, success)

    def cancel_upload(self):
        self.is_canceled = True


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

        self.select_folder_button = QtWidgets.QPushButton("Seleccionar", self)
        self.select_folder_button.clicked.connect(self.select_folder)
        folder_layout.addWidget(self.select_folder_button)

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

        main_layout.addLayout(btn_layout)

        self.result_list = QtWidgets.QListWidget(self)
        main_layout.addWidget(self.result_list)

        self.setLayout(main_layout)
        self.show()

    def closeEvent(self, event):
        if not self.close_event_handled:  # Verifica si el evento ya fue manejado
            self.close_event_handled = True  # Marca el evento como manejado
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
                    self.progress_window.close_event_handled = (
                        True  # Evita cierre múltiple
                    )
                    self.progress_window.close()
                event.accept()
            else:
                self.close_event_handled = False  # Permitir futuras confirmaciones
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
                    # Crea el nuevo nombre del archivo con el nombre de la carpeta al final
                    base, ext = os.path.splitext(file_name)

                    # Comprueba si el nombre del archivo ya contiene el nombre de la carpeta
                    if not base.endswith(f"_{folder_name}"):
                        new_file_name = f"{base}_{folder_name}{ext}"

                        # Obtiene la ruta completa de los archivos antiguos y nuevos
                        old_path = os.path.join(rootf, file_name)
                        new_path = os.path.join(rootf, new_file_name)

                        # Renombra el archivo
                        os.rename(old_path, new_path)

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

        # Crear una nueva instancia de ProgressWindow si no existe o no está visible
        if not self.progress_window or not self.progress_window.isVisible():
            self.progress_window = ProgressWindow()
            self.progress_window.cancel_all.connect(self.cancel_all_uploads)
            self.progress_window.reset_ui.connect(self.reset_ui_state)
            self.progress_window.show()  # Asegura que la ventana esté visible

            # Limpiar referencias antiguas y reiniciar progresos
            progress_bars.clear()
            progress_labels.clear()

        # Añadir todas las carpetas seleccionadas a la interfaz con el estado "Carpeta en cola"
        for folder in selected_folders:
            base_folder_name = os.path.basename(folder)
            if base_folder_name not in progress_bars:
                self.progress_window.add_progress_ui(
                    base_folder_name, "Carpeta en cola"
                )
                self.upload_queue.put((folder, s3_folder))

        # Iniciar la subida si hay slots disponibles
        self.start_next_uploads()

    def start_next_uploads(self):
        while (
            self.active_uploads < MAX_CONCURRENT_UPLOADS
            and not self.upload_queue.empty()
        ):
            folder, s3_folder = self.upload_queue.get()
            base_folder_name = os.path.basename(folder)

            # Actualizar el estado de la carpeta a "Iniciando..." y progreso a 0%
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
                progress_labels[base_folder_name].setText("Error al subir la carpeta.")

        self.result_list.scrollToBottom()

        # Restar una subida activa y comenzar una nueva si hay carpetas en la cola
        self.active_uploads -= 1
        self.start_next_uploads()

    def cancel_all_uploads(self):
        global upload_threads

        # Emite la señal de cancelación y espera a que los hilos terminen
        for folder, (worker, worker_thread) in upload_threads.items():
            worker.cancel_signal.emit()
            worker_thread.quit()
            worker_thread.wait()

        # Limpia todas las referencias a hilos y trabajadores
        upload_threads.clear()
        self.result_list.addItem("Todas las cargas pendientes han sido canceladas.")
        self.upload_queue.queue.clear()  # Limpiar la cola de subidas pendientes

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
