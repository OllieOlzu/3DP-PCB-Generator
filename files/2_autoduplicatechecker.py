import os
from collections import defaultdict

# Get the directory where the script is located
script_dir = os.path.dirname(os.path.abspath(__file__))

# Path to the cache folder
cache_dir = os.path.join(script_dir, "cache")

# Path to error file
error_file_path = os.path.join(script_dir, "error.txt")

# Dictionary to store extensions and their files
ext_dict = defaultdict(list)

# Check if cache folder exists
if not os.path.exists(cache_dir):
    print("Cache folder not found.")
    exit()

# Walk through cache directory
for root, dirs, files in os.walk(cache_dir):
    for file in files:
        _, ext = os.path.splitext(file)
        ext = ext.lower()

        if ext:  # Ignore files with no extension
            ext_dict[ext].append(os.path.join(root, file))

# Find duplicates
duplicates = {ext: files for ext, files in ext_dict.items() if len(files) > 1}

if duplicates:
    with open(error_file_path, "w", encoding="utf-8") as f:
        f.write("Duplicate file extensions found:\n\n")
        for ext, file_list in duplicates.items():
            f.write(f"Extension '{ext}' appears {len(file_list)} times:\n")
            for file_path in file_list:
                f.write(f"  {file_path}\n")
            f.write("\n")

    print(f"Duplicates found. Report written to {error_file_path}")
else:
    print("No duplicate file extensions found.")
