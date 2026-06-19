import os

# Get script directory
script_dir = os.path.dirname(os.path.abspath(__file__))

# Cache folder path
cache_dir = os.path.join(script_dir, "cache")

# Error file path
error_file_path = os.path.join(script_dir, "error.txt")

# Check if cache folder exists
if not os.path.exists(cache_dir):
    print("Cache folder not found.")
    exit()

# Flags to track if files are found
found_gko = False
found_gml = False

# Walk through cache directory
for root, dirs, files in os.walk(cache_dir):
    for file in files:
        ext = os.path.splitext(file)[1].lower()

        if ext == ".gko":
            found_gko = True
        elif ext == ".gml":
            found_gml = True

# If neither found, write error
if not found_gko and not found_gml:
    with open(error_file_path, "w", encoding="utf-8") as f:
        f.write("No outline found\n")

    print("Error: No outline found (written to error.txt)")
else:
    print("Outline file detected (.gko or .gml present)")
