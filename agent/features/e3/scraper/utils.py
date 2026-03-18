import os, re
import json
from hashlib import md5
from . import config

BASE_DIR = config.BASE_DIR

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

def get_file_hash(path):
    if os.path.exists(path):
        with open(path, "rb") as f:
            return md5(f.read()).hexdigest()
    return None

def ensure_course_folder(course_id, course_name):
    folder_name = f"{course_id}_{course_name}"
    folder_path = os.path.join(BASE_DIR, folder_name)
    os.makedirs(folder_path, exist_ok=True)
    return folder_path

def safe_name(name: str) -> str:
    """
    Sanitize folder/file names for Windows:
    - Replace spaces and underscores with single '_'
    - Remove/replace invalid characters like : ? * etc.
    - Strip leading/trailing underscores
    - Limit length (MAX_PATH safety)
    """
    # Replace invalid chars with _
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    # Replace full-width colon (：) and brackets 【】 
    name = name.replace("：", "_").replace("【", "").replace("】", "")
    # Normalize spaces/underscores
    name = re.sub(r'[\s_]+', "_", name)
    # Remove leading/trailing underscores
    name = name.strip("_")
    # Optional: shorten to avoid MAX_PATH errors
    return name[:150]

def format_display_name(folder_name: str) -> str:
    """
    Format course folder name for display in UI:
    - Remove course ID prefix (e.g., "16944_")
    - Clean up extra underscores and symbols
    - Make it more readable
    - Limit length for display
    """
    # Remove course ID prefix (numbers followed by underscore at the start)
    name = re.sub(r'^\d+_', '', folder_name)
    
    # Replace multiple underscores with single space
    name = re.sub(r'_+', ' ', name)
    
    # Remove common patterns that add clutter
    # Remove semester prefixes like "114上" if they appear
    name = re.sub(r'^\d+[上下]', '', name).strip()
    
    # Clean up extra spaces
    name = re.sub(r'\s+', ' ', name).strip()
    
    # If still too long, truncate intelligently
    if len(name) > 60:
        # Try to truncate at a word boundary
        truncated = name[:57]
        last_space = truncated.rfind(' ')
        if last_space > 40:  # Only truncate at word if it's not too short
            name = truncated[:last_space] + '...'
        else:
            name = truncated + '...'
    
    return name if name else folder_name  # Fallback to original if empty
