import tkinter as tk
from tkinter import filedialog, simpledialog, messagebox
from tkinter import ttk
import os
import subprocess
from threading import Thread
import re
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
cancel_buttons = {}
upload_threads = {}
progress_frame = None


def select_folder():
    global selected_folders, total_files
    selected_folder = filedialog.askdirectory(title="Selecciona una carpeta")
    if selected_folder:
        selected_folders.append(selected_folder)
        file_list.insert(tk.END, selected_folder)
        result_label.insert(tk.END, f"Seleccionada la carpeta: {selected_folder}")
        result_label.insert(tk.END, "Contando archivos...")
        result_label.yview(tk.END)
        total_files += sum(len(files) for _, _, files in os.walk(selected_folder))
        result_label.insert(
            tk.END,
            f"Se han detectado {total_files} archivos en total por subir.",
        )
        result_label.yview(tk.END)

    update_upload_button_state()


def list_s3_folders(bucket_name):
    folders = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket_name, Delimiter="/"):
        for prefix in page.get("CommonPrefixes", []):
            folders.append(prefix.get("Prefix"))
    return folders


def update_s3_folder_combobox():
    folders = list_s3_folders(AWS_BUCKET)
    s3_folder_combobox["values"] = folders
    if folders:
        s3_folder_combobox.current(0)


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


def create_new_s3_folder():
    new_folder_name = simpledialog.askstring(
        "Nueva carpeta", "Ingrese el nombre de la nueva carpeta:"
    )
    if new_folder_name:
        if not new_folder_name.endswith("/"):
            new_folder_name += "/"
        try:
            s3_client.put_object(Bucket=AWS_BUCKET, Key=new_folder_name)
            result_label.insert(
                tk.END, f"Carpeta creada: s3://{AWS_BUCKET}/{new_folder_name}"
            )
            update_s3_folder_combobox()
        except Exception as e:
            result_label.insert(tk.END, f"Error al crear carpeta: {str(e)}")
        result_label.yview(tk.END)


def upload_folder():
    global progress_frame

    s3_folder = s3_folder_combobox.get()
    result_label.insert(tk.END, f"Directorio de destino s3://{AWS_BUCKET}/{s3_folder}")

    select_folder_button.config(state="disabled")
    create_folder_button.config(state="disabled")
    upload_button.config(state="disabled")
    cancel_button.config(state="normal")

    if progress_frame:
        progress_frame.destroy()
    progress_frame = tk.Frame(root)
    progress_frame.grid(
        row=7, column=0, columnspan=2, padx=20, pady=(10, 20), sticky="ew"
    )

    def upload_single_folder(selected_folder):
        global cancel_buttons, upload_threads

        cancel_thread = False

        def cancel_upload():
            nonlocal cancel_thread
            cancel_thread = True
            cancel_buttons[selected_folder].config(state="disabled")

        cancel_button = tk.Button(
            progress_frame, text="Cancelar", command=cancel_upload
        )
        cancel_button.pack(fill=tk.X, padx=10, pady=2)
        cancel_buttons[selected_folder] = cancel_button

        for rootf, dirs, files in os.walk(selected_folder):
            folder_name = os.path.basename(rootf)
            for file_name in files:
                base, ext = os.path.splitext(file_name)
                if not base.endswith(f"_{folder_name}"):
                    new_file_name = f"{base}_{folder_name}{ext}"
                    old_path = os.path.join(rootf, file_name)
                    new_path = os.path.join(rootf, new_file_name)
                    os.rename(old_path, new_path)

        base_folder_name = os.path.basename(selected_folder)
        if s3_folder == "":
            s3_path = f"s3://{AWS_BUCKET}/{base_folder_name}/"
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

        folder_label = tk.Label(
            progress_frame, text=f"Subiendo Carpeta: {base_folder_name}"
        )
        folder_label.pack(fill=tk.X, padx=10, pady=2)

        progress_var = tk.DoubleVar()
        progress_bar = ttk.Progressbar(
            progress_frame, variable=progress_var, length=600, mode="determinate"
        )
        progress_bar.pack(fill=tk.X, padx=10, pady=2)
        progress_label = tk.Label(progress_frame, text="0%")
        progress_label.pack(fill=tk.X, padx=10, pady=2)

        progress_bars[selected_folder] = progress_bar
        progress_labels[selected_folder] = progress_label

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        data_transferred_regex = re.compile(
            r"Completed (\d+(\.\d+)?) (KiB|MiB|GiB)/~?(\d+(\.\d+)?) (KiB|MiB|GiB)"
        )

        while True:
            if cancel_thread:
                process.terminate()
                result_label.insert(
                    tk.END, f"Subida de {selected_folder} cancelada por el usuario."
                )
                messagebox.showinfo(
                    "Cancelado", f"La subida de {selected_folder} ha sido cancelada."
                )
                break

            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if line:
                match = data_transferred_regex.search(line)
                if match:
                    completed_data = float(match.group(1))
                    completed_unit = match.group(3)
                    total_data = float(match.group(4))
                    total_unit = match.group(6)

                    if completed_unit == "MiB":
                        completed_data *= 1024
                    elif completed_unit == "GiB":
                        completed_data *= 1024 * 1024
                    if total_unit == "MiB":
                        total_data *= 1024
                    elif total_unit == "GiB":
                        total_data *= 1024 * 1024

                    progress = (completed_data / total_data) * 100
                    progress_var.set(progress)
                    progress_label.config(text=f"{int(progress)}% {line.strip()}")
                    root.update_idletasks()

        stderr = process.stderr.read()
        if stderr and not cancel_thread:
            result_label.insert(tk.END, f"Error: {stderr}")

        if process.returncode == 0 and not cancel_thread:
            result_label.insert(
                tk.END,
                f"La carpeta {selected_folder} se ha subido exitosamente",
            )
            result_label.yview(tk.END)
        elif cancel_thread:
            result_label.insert(
                tk.END, f"Subida de {selected_folder} cancelada y revertida."
            )
            result_label.yview(tk.END)

        cancel_button.config(state="disabled")

    for selected_folder in selected_folders:
        upload_threads[selected_folder] = Thread(
            target=upload_single_folder, args=(selected_folder,)
        )
        upload_threads[selected_folder].start()

    def restore_ui():
        for thread in upload_threads.values():
            thread.join()
        select_folder_button.config(state="normal")
        create_folder_button.config(state="normal")
        upload_button.config(state="normal")
        cancel_button.config(state="disabled")

    Thread(target=restore_ui).start()


def cancel_upload_function():
    global cancel_buttons
    for folder, button in cancel_buttons.items():
        button.invoke()
    cancel_button.config(state="disabled")


def on_closing():
    result_label.insert(tk.END, "Cerrando programa...")
    result_label.yview(tk.END)
    cancel_upload_function()
    root.after(2000, root.destroy)


def update_upload_button_state():
    if selected_folders:
        upload_button.config(state="normal")
    else:
        upload_button.config(state="disabled")


root = tk.Tk()
root.title("Subir Carpetas a S3")

selected_folder_label = tk.Label(
    root, text="Carpetas seleccionadas:", font=("Arial", 13)
)
selected_folder_label.grid(row=0, column=0, padx=20, pady=(20, 0), sticky="w")

file_list = tk.Listbox(root, width=64, height=5)
file_list.grid(row=1, column=0, padx=(20, 5), pady=5, sticky="w")

select_folder_button = tk.Button(
    root, text="Seleccionar", command=select_folder, width=10
)
select_folder_button.grid(row=1, column=1, padx=(5, 20), pady=5, sticky="e")

s3_folder_info_label = tk.Label(
    root, text="Selecciona destino en S3:", font=("Arial", 13)
)
s3_folder_info_label.grid(row=2, padx=20, pady=(5, 0), column=0, sticky="w")

s3_folder_combobox = ttk.Combobox(root, width=60)
s3_folder_combobox.grid(row=3, column=0, padx=(20, 5), pady=5, sticky="w")
s3_folder_combobox.bind("<<ComboboxSelected>>", on_s3_folder_selected)

create_folder_button = tk.Button(
    root, text="Crear carpeta", command=create_new_s3_folder, width=10
)
create_folder_button.grid(row=3, column=1, padx=(5, 20), pady=5, sticky="e")

upload_button = tk.Button(
    root, text="Subir Carpetas", command=upload_folder, state="disabled", width=10
)
upload_button.grid(row=5, column=0, padx=(20, 10), pady=(50, 5), sticky="e")

cancel_button = tk.Button(
    root, text="Cancelar Todo", command=cancel_upload_function, width=10
)
cancel_button.grid(row=5, column=1, padx=(10, 20), pady=(50, 5), sticky="e")
cancel_button.config(state="disabled")

root.grid_columnconfigure(0, weight=1)
root.grid_columnconfigure(1, weight=1)

result_frame = tk.Frame(root)
result_frame.grid(row=6, column=0, columnspan=3, padx=20, pady=(10, 20))

v_scrollbar = tk.Scrollbar(result_frame, orient=tk.VERTICAL)
v_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

h_scrollbar = tk.Scrollbar(result_frame, orient=tk.HORIZONTAL)
h_scrollbar.pack(side=tk.BOTTOM, fill=tk.X)

result_label = tk.Listbox(
    result_frame,
    width=80,
    height=10,
    yscrollcommand=v_scrollbar.set,
    xscrollcommand=h_scrollbar.set,
)
result_label.pack(side=tk.LEFT, fill=tk.BOTH)

v_scrollbar.config(command=result_label.yview)
h_scrollbar.config(command=result_label.xview)

root.protocol("WM_DELETE_WINDOW", on_closing)

update_s3_folder_combobox()

root.mainloop()
