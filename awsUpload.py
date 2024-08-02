import tkinter as tk
from tkinter import filedialog, simpledialog, messagebox
from tkinter import ttk
import os
import subprocess
from threading import Thread
import re
import boto3  # Importar boto3 para interactuar con AWS S3
from dotenv import load_dotenv

# Cargar variables de entorno desde el archivo .env
load_dotenv()

# Obtener las variables de entorno
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_BUCKET = os.getenv("AWS_BUCKET")


# Crear cliente de boto3 para S3
s3_client = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
)

# Variable para manejar la cancelación
cancel_upload = False
selected_folder = None  # Variable para guardar la carpeta seleccionada
total_files = 0  # Variable para contar el total de archivos
total_data_transferred = 0  # Variable para almacenar el total de datos transferidos
total_data_size = 0  # Variable para almacenar el tamaño total de datos a transferir


# Función para seleccionar carpeta
def select_folder():
    global selected_folder, total_files
    file_list.delete(0, tk.END)  # Limpiar la lista de archivos

    selected_folder = filedialog.askdirectory(title="Selecciona una carpeta")
    if selected_folder:
        file_list.insert(tk.END, selected_folder)  # Mostrar la carpeta seleccionada
        result_label.insert(tk.END, f"Seleccionada la carpeta: {selected_folder}")
        result_label.insert(tk.END, "Contando archivos...")
        result_label.yview(tk.END)
        # Contar la cantidad total de archivos en la carpeta seleccionada
        total_files = sum(len(files) for _, _, files in os.walk(selected_folder))
        result_label.insert(
            tk.END,
            f"Se han detectado {total_files} archivos por subir.",
        )
        result_label.yview(tk.END)

    update_upload_button_state()  # Actualizar el estado del botón de subida


# Función para listar carpetas en el bucket
def list_s3_folders(bucket_name):
    folders = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket_name, Delimiter="/"):
        for prefix in page.get("CommonPrefixes", []):
            folders.append(prefix.get("Prefix"))
    return folders


# Función para actualizar la lista desplegable de carpetas S3
def update_s3_folder_combobox():
    folders = list_s3_folders(AWS_BUCKET)
    s3_folder_combobox["values"] = folders
    if folders:
        s3_folder_combobox.current(0)


# Función para manejar la selección de la carpeta en S3
def on_s3_folder_selected(event):
    s3_folder = s3_folder_combobox.get()
    if s3_folder:
        result_label.insert(
            tk.END, f"Carpeta S3 seleccionada: s3://{AWS_BUCKET}/{s3_folder}"
        )
        result_label.yview(tk.END)
    else:
        result_label.insert(tk.END, "No se seleccionó ninguna carpeta de S3.")
        result_label.yview(tk.END)


# Función para crear una nueva carpeta en S3
def create_new_s3_folder():
    new_folder_name = simpledialog.askstring(
        "Nueva carpeta", "Ingrese el nombre de la nueva carpeta:"
    )
    if new_folder_name:
        if not new_folder_name.endswith("/"):
            new_folder_name += "/"
        try:
            # Crear una nueva carpeta en S3 (en realidad, se crea un objeto vacío con el nombre de la carpeta)
            s3_client.put_object(Bucket=AWS_BUCKET, Key=new_folder_name)
            result_label.insert(
                tk.END, f"Carpeta creada: s3://{AWS_BUCKET}/{new_folder_name}"
            )
            update_s3_folder_combobox()  # Actualizar la lista de carpetas
        except Exception as e:
            result_label.insert(tk.END, f"Error al crear carpeta: {str(e)}")
        result_label.yview(tk.END)


def upload_folder():
    global total_data_transferred
    global total_data_size
    global cancel_upload

    s3_folder = s3_folder_combobox.get()
    result_label.insert(tk.END, f"Directorio de destino s3://{AWS_BUCKET}/{s3_folder}")

    result_label.insert(tk.END, f"Comenzando a subir carpeta {selected_folder}...")
    result_label.yview(tk.END)

    total_data_transferred = 0
    total_data_size = 0
    cancel_upload = False

    # Deshabilitar todos los botones excepto el de cancelar
    select_folder_button.config(state="disabled")
    create_folder_button.config(state="disabled")
    upload_button.config(state="disabled")
    cancel_button.config(state="normal")

    # Mostrar la barra de progreso y la etiqueta de porcentaje al iniciar la subida
    progress_bar.grid()
    progress_percentage_label.grid()

    if selected_folder is None:
        result_label.insert(tk.END, "Por favor, selecciona una carpeta.")
        result_label.yview(tk.END)
        return

    # Recorre los directorios y archivos en la carpeta seleccionada
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

    def run_upload():
        global total_data_transferred
        global total_data_size
        try:
            base_folder_name = os.path.basename(selected_folder)
            if s3_folder == "":
                s3_path = f"s3://{AWS_BUCKET}{base_folder_name}/"
            else:
                s3_path = f"s3://{AWS_BUCKET}/{s3_folder}{base_folder_name}/"
            command = [
                "aws",
                "s3",
                "cp",
                selected_folder,
                s3_path,
                "--recursive",
            ]

            # Aquí es donde se hace el cambio para evitar la consola
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            # Regex para detectar los datos transferidos y el total de datos
            data_transferred_regex = re.compile(
                r"Completed (\d+(\.\d+)?) (KiB|MiB|GiB)/~?(\d+(\.\d+)?) (KiB|MiB|GiB)"
            )

            while True:
                if cancel_upload:
                    process.terminate()  # Terminar el proceso
                    result_label.insert(tk.END, "Subida cancelada por el usuario.")
                    messagebox.showinfo("Cancelado", "La subida ha sido cancelada.")
                    break

                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                if line:
                    # result_label.insert(tk.END, line)
                    # result_label.yview(tk.END)

                    # Solo actualizar la barra de progreso si el log tiene el formato esperado
                    match = data_transferred_regex.search(line)
                    if match:
                        completed_data = float(match.group(1))
                        completed_unit = match.group(3)
                        total_data = float(match.group(4))
                        total_unit = match.group(6)

                        # Convertir MiB y GiB a KiB para mantener consistencia en la comparación
                        if completed_unit == "MiB":
                            completed_data *= 1024
                        elif completed_unit == "GiB":
                            completed_data *= 1024 * 1024
                        if total_unit == "MiB":
                            total_data *= 1024
                        elif total_unit == "GiB":
                            total_data *= 1024 * 1024

                        total_data_transferred = completed_data
                        total_data_size = total_data
                        progress = (total_data_transferred / total_data_size) * 100
                        progress_bar["value"] = progress
                        progress_percentage_label.config(
                            text=f"{int(progress)}% {line.strip()}"
                        )
                        root.update_idletasks()

            stderr = process.stderr.read()
            if stderr and not cancel_upload:
                result_label.insert(tk.END, f"Error: {stderr}")

            if process.returncode == 0 and not cancel_upload:
                result_label.insert(
                    tk.END, f"La carpeta {selected_folder} se ha subido exitosamente\n"
                )
                result_label.yview(tk.END)
                messagebox.showinfo("Finalizado", "Carpeta subida exitosamente.")
                file_list.delete(0, tk.END)
                s3_folder_combobox.set("")
            elif cancel_upload:
                result_label.insert(tk.END, "Subida cancelada y revertida.")
                result_label.yview(tk.END)

        except Exception as e:
            if not cancel_upload:
                result_label.insert(tk.END, f"Error: {str(e)}")
                result_label.yview(tk.END)

        finally:
            select_folder_button.config(state="normal")
            create_folder_button.config(state="normal")
            upload_button.config(state="normal")
            cancel_button.config(state="disabled")
            # Ocultar la barra de progreso y la etiqueta de porcentaje al finalizar
            progress_bar["value"] = 0
            progress_bar.grid_remove()
            progress_percentage_label.config(text="0%")
            progress_percentage_label.grid_remove()

    Thread(target=run_upload).start()


# Función para cancelar la subida
def cancel_upload_function():
    global cancel_upload
    cancel_upload = True


def on_closing():
    result_label.insert(tk.END, "Cerrando programa...")
    result_label.yview(tk.END)
    cancel_upload_function()
    # Cerrar la ventana después de un pequeño retraso para asegurar que el mensaje se muestra
    root.after(2000, root.destroy)


# Función para actualizar el estado del botón de subida
def update_upload_button_state():
    if selected_folder:
        upload_button.config(state="normal")
    else:
        upload_button.config(state="disabled")


# Configuración de la ventana principal
root = tk.Tk()
root.title("Subir Carpeta a S3")

# Etiqueta para la carpeta seleccionada
selected_folder_label = tk.Label(root, text="Carpeta seleccionada:", font=("Arial", 13))
selected_folder_label.grid(row=0, column=0, padx=20, pady=(20, 0), sticky="w")

# Lista de archivos seleccionados
file_list = tk.Listbox(root, width=64, height=1)
file_list.grid(row=1, column=0, padx=(20, 5), pady=5, sticky="w")

# Botón para seleccionar carpeta
select_folder_button = tk.Button(
    root, text="Seleccionar", command=select_folder, width=10
)
select_folder_button.grid(row=1, column=1, padx=(5, 20), pady=5, sticky="e")

# Etiqueta para la carpeta de destino en S3
s3_folder_info_label = tk.Label(
    root, text="Selecciona destino en S3:", font=("Arial", 13)
)
s3_folder_info_label.grid(row=2, padx=20, pady=(5, 0), column=0, sticky="w")

# Lista desplegable para seleccionar carpeta de destino en S3
s3_folder_combobox = ttk.Combobox(root, width=60)
s3_folder_combobox.grid(row=3, column=0, padx=(20, 5), pady=5, sticky="w")
s3_folder_combobox.bind("<<ComboboxSelected>>", on_s3_folder_selected)

# Botón para crear una nueva carpeta en S3
create_folder_button = tk.Button(
    root, text="Crear carpeta", command=create_new_s3_folder, width=10
)
create_folder_button.grid(row=3, column=1, padx=(5, 20), pady=5, sticky="e")

# Botones para subir y cancelar la subida
upload_button = tk.Button(
    root, text="Subir Carpeta", command=upload_folder, state="disabled", width=10
)
upload_button.grid(row=5, column=0, padx=(20, 10), pady=(50, 5), sticky="e")

cancel_button = tk.Button(
    root, text="Cancelar", command=cancel_upload_function, width=10
)
cancel_button.grid(row=5, column=1, padx=(10, 20), pady=(50, 5), sticky="e")
cancel_button.config(state="disabled")

# Configurar columnas para que se expandan uniformemente
root.grid_columnconfigure(0, weight=1)
root.grid_columnconfigure(1, weight=1)

# Label para mostrar el resultado con scrollbars
result_frame = tk.Frame(root)
result_frame.grid(row=6, column=0, columnspan=3, padx=20, pady=(10, 20))

# Scrollbar vertical
v_scrollbar = tk.Scrollbar(result_frame, orient=tk.VERTICAL)
v_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

# Scrollbar horizontal
h_scrollbar = tk.Scrollbar(result_frame, orient=tk.HORIZONTAL)
h_scrollbar.pack(side=tk.BOTTOM, fill=tk.X)

# Listbox para mostrar resultados
result_label = tk.Listbox(
    result_frame,
    width=80,
    height=10,
    yscrollcommand=v_scrollbar.set,
    xscrollcommand=h_scrollbar.set,
)
result_label.pack(side=tk.LEFT, fill=tk.BOTH)

# Configurar las scrollbars para que se muevan junto con la Listbox
v_scrollbar.config(command=result_label.yview)
h_scrollbar.config(command=result_label.xview)

# Barra de progreso
progress_bar = ttk.Progressbar(root, length=600, mode="determinate")
progress_bar.grid(row=7, column=0, columnspan=2, padx=20, pady=(10, 0))
# Reiniciar la barra de progreso al inicio
progress_bar["value"] = 0
# Ocultar la barra de progreso al inicio
progress_bar.grid_remove()

# Etiqueta para mostrar el porcentaje de progreso
progress_percentage_label = tk.Label(root, text="0%")
progress_percentage_label.grid(row=8, column=0, columnspan=2)
# Poner porcentaje igual a 0 al inicio
progress_percentage_label.config(text="0%")

# Ocultar la etiqueta de porcentaje al inicio
progress_percentage_label.grid_remove()

# Vincular la función on_closing al evento de cierre de la ventana
root.protocol("WM_DELETE_WINDOW", on_closing)

# Cargar carpetas S3 automáticamente al inicio
update_s3_folder_combobox()

# Iniciar la aplicación
root.mainloop()
