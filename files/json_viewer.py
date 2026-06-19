import json
import matplotlib.pyplot as plt
import tkinter as tk
from tkinter import filedialog

def draw_shapes(shapes):
    plt.figure()

    for shape in shapes:
        if len(shape) < 2:
            continue

        x = [p[0] for p in shape] + [shape[0][0]]
        y = [p[1] for p in shape] + [shape[0][1]]

        plt.plot(x, y)
        plt.scatter([p[0] for p in shape], [p[1] for p in shape])

    plt.gca().set_aspect('equal')
    plt.grid(True)
    plt.show()

def load_json_file():
    # hide root window
    root = tk.Tk()
    root.withdraw()

    file_path = filedialog.askopenfilename(
        title="Select Shape JSON File",
        filetypes=[("JSON files", "*.json")]
    )

    if not file_path:
        print("No file selected.")
        return

    with open(file_path, "r") as f:
        data = json.load(f)

    # if your format is just a list:
    # [
    #   [[x,y],[x,y]...],
    #   ...
    # ]
    if isinstance(data, list):
        shapes = data
    else:
        # if wrapped like {"scene1": [...]}
        key = next(iter(data))
        shapes = data[key]

    draw_shapes(shapes)

load_json_file()
