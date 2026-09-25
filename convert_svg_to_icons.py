import os
import resvg_py

SVG_INPUT_FILE = "icon.svg"
OUTPUT_DIR = "resources"

# (base_name, target_pixel_dimension)
ICON_TARGETS = [
    # 1x Low-DPI targets
    ("16x16", 16),
    ("32x32", 32),
    ("64x64", 64),
    
    # 2x Retina / High-DPI targets (Qt @2x convention)
    ("16x16@2x", 32),
    ("32x32@2x", 64),
    ("64x64@2x", 128),
]

def generate_clean_icons(svg_path, output_dir):
    if not os.path.exists(svg_path):
        print(f"Error: Could not find '{svg_path}'")
        return

    os.makedirs(output_dir, exist_ok=True)

    with open(svg_path, "r", encoding="utf-8") as f:
        svg_text = f.read()

    for name, pixel_size in ICON_TARGETS:
        png_data = resvg_py.svg_to_bytes(
            svg_string=svg_text,
            width=pixel_size,
            height=pixel_size
        )

        # Standard theme
        std_path = os.path.join(output_dir, f"{name}.png")
        with open(std_path, "wb") as f:
            f.write(png_data)

        # Dark theme variants (both base-dark@2x and base@2x-dark for Qt safety)
        if "@2x" in name:
            base = name.replace("@2x", "")
            dark_names = [f"{base}-dark@2x.png", f"{base}@2x-dark.png"]
        else:
            dark_names = [f"{name}-dark.png"]

        for d_name in dark_names:
            dark_path = os.path.join(output_dir, d_name)
            with open(dark_path, "wb") as f:
                f.write(png_data)

        print(f"Rendered: {name}.png ({pixel_size}x{pixel_size} px)")

    print("\nAll Retina (@2x) and standard assets generated successfully!")

if __name__ == "__main__":
    generate_clean_icons(SVG_INPUT_FILE, OUTPUT_DIR)