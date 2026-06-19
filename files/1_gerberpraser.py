import os
import zipfile
import shutil
from tkinter import Tk, filedialog

# Hide the root tkinter window
Tk().withdraw()

# Ask user to select a zip file
zip_path = filedialog.askopenfilename(
    title="Select a ZIP file",
    filetypes=[("ZIP files", "*.zip")]
)

if not zip_path:
    print("No file selected.")
    exit()

# Get script directory
script_dir = os.path.dirname(os.path.abspath(__file__))

# Create cache folder if it doesn't exist
cache_dir = os.path.join(script_dir, "cache")
os.makedirs(cache_dir, exist_ok=True)

# Allowed extensions
allowed_exts = {".gtl", ".gbl", ".gts", ".gbs", ".gto", ".gbo", ".gko", ".gml"}

# Open the zip file
with zipfile.ZipFile(zip_path, 'r') as zip_ref:
    for file_info in zip_ref.infolist():
        filename = file_info.filename

        # Skip directories
        if file_info.is_dir():
            continue

        ext = os.path.splitext(filename)[1].lower()

        # Check if file extension is allowed
        if ext in allowed_exts:
            # Extract to a temporary path
            extracted_path = zip_ref.extract(file_info, path=script_dir)

            # Destination path (just filename, no folders)
            dest_path = os.path.join(cache_dir, os.path.basename(filename))

            # Move file into cache
            shutil.move(extracted_path, dest_path)

            print(f"Copied: {filename}")

print("Done.")
