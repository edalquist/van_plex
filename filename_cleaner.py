# filename_cleaner.py

def clean_filename(original_name: str) -> str:
    """
    Cleans a filename by taking everything up to the first square bracket '['.
    
    This is designed to remove metadata tags commonly found in media filenames.
    
    Args:
        original_name: The base filename (without extension).
        
    Returns:
        The cleaned filename, with any trailing whitespace removed.
    """
    first_bracket_index = original_name.find('[')
    
    if first_bracket_index != -1:
        # If a bracket is found, take everything before it
        return original_name[:first_bracket_index].rstrip()
    else:
        # If no bracket is found, return the original name as is
        return original_name.rstrip()
