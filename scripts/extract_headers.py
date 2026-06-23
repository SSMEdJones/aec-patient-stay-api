"""
Extract header images from all ministry templates into headers/ folder.
Run once to set up header images for dynamic header swapping.
"""
import json
import zipfile
import shutil
from pathlib import Path

def extract_header_images():
    """Extract header images from ministry templates."""
    
    # Load ministry config
    config_path = Path(__file__).parent.parent / "ministries.json"
    with open(config_path) as f:
        ministries = json.load(f)["ministries"]
    
    templates_dir = Path(__file__).parent.parent / "appeal_templates"
    headers_dir = templates_dir / "headers"
    headers_dir.mkdir(exist_ok=True)
    
    for ministry_code, ministry in ministries.items():
        template_file = ministry.get("template_file")
        if not template_file:
            print(f"  {ministry_code}: No template_file specified")
            continue
        
        template_path = templates_dir / template_file
        if not template_path.exists():
            print(f"  {ministry_code}: Template not found: {template_file}")
            continue
        
        print(f"\n{ministry_code}: {template_file}")
        
        # docx is a ZIP file - extract media files from header
        try:
            with zipfile.ZipFile(template_path, 'r') as zf:
                # List all files
                all_files = zf.namelist()
                
                # Find header files
                header_files = [f for f in all_files if 'header' in f.lower()]
                print(f"  Header files: {header_files}")
                
                # Find media files (images)
                media_files = [f for f in all_files if f.startswith('word/media/')]
                print(f"  Media files: {media_files}")
                
                # Extract first image (usually the logo)
                if media_files:
                    # Get the first image
                    img_file = media_files[0]
                    ext = Path(img_file).suffix  # .emf, .png, .jpg, etc.
                    
                    # Extract to headers folder
                    output_path = headers_dir / f"{ministry_code}{ext}"
                    
                    with zf.open(img_file) as src:
                        with open(output_path, 'wb') as dst:
                            dst.write(src.read())
                    
                    print(f"  ✓ Extracted: {output_path.name}")
                else:
                    print(f"  No media files found")
                    
        except Exception as e:
            print(f"  Error: {e}")
    
    print(f"\n\nDone! Headers saved to: {headers_dir}")

if __name__ == "__main__":
    extract_header_images()
