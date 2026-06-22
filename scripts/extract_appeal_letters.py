"""
Extract appeal letters from PDFs and save them for comparison testing.
"""
import pdfplumber
import re
import os
import json
from pathlib import Path

def extract_appeal_letter(pdf_path: str) -> dict:
    """Extract appeal letter sections from a PDF."""
    try:
        pdf = pdfplumber.open(pdf_path)
        text = ''.join([p.extract_text() or '' for p in pdf.pages])
        pdf.close()
    except Exception as e:
        return {"error": str(e)}
    
    # Normalize text (PDF extraction often removes spaces)
    # We'll store both raw and normalized versions
    
    result = {
        "source_pdf": os.path.basename(pdf_path),
        "hook": None,
        "history_paragraph": None,
        "first_midnight": None,
        "second_midnight": None,
        "closing": None,
        "raw_letter": None
    }
    
    # Find the full appeal letter (from "Dear Medical Director" to "Sincerely")
    letter_match = re.search(
        r'(DearMedicalDirector|Dear Medical Director)(.*?)(Sincerely|SINCERELY)',
        text, 
        re.DOTALL | re.IGNORECASE
    )
    
    if letter_match:
        raw_letter = letter_match.group(0)
        result["raw_letter"] = raw_letter
        
        # Extract hook (Your member was admitted/required...)
        hook_match = re.search(
            r'(Yourmember(?:was)?(?:admitted|required)[^\.]+(?:requiring|treatment)[^\.]+\.)',
            raw_letter,
            re.DOTALL
        )
        if hook_match:
            result["hook"] = hook_match.group(1)
        
        # Extract history paragraph (patient is a XX year old...)
        history_match = re.search(
            r'([A-Z][a-z]+(?:\s)?[A-Z]?(?:\s)?[A-Z][a-z]+isa?\d+\s?year\s?old[^\.]+\.)',
            raw_letter,
            re.DOTALL
        )
        if history_match:
            result["history_paragraph"] = history_match.group(1)
        
        # Extract first midnight
        m1_match = re.search(
            r'(Duringthefirstmidnight|During the first midnight)[^\.]+\.[^\.]*\.[^\.]*\.',
            raw_letter,
            re.DOTALL
        )
        if m1_match:
            result["first_midnight"] = m1_match.group(0)
        
        # Extract second midnight
        m2_match = re.search(
            r'(Duringthesecondmidnight|During the second midnight)[^\.]+\.[^\.]*\.[^\.]*\.',
            raw_letter,
            re.DOTALL
        )
        if m2_match:
            result["second_midnight"] = m2_match.group(0)
        
        # Extract closing (In summary...)
        close_match = re.search(
            r'(Insummary|In summary)[^\.]+medicallynecessary\.',
            raw_letter,
            re.DOTALL | re.IGNORECASE
        )
        if close_match:
            result["closing"] = close_match.group(0)
    
    return result


def add_spaces_to_text(text: str) -> str:
    """Add spaces back to PDF-extracted text that lost them."""
    if not text:
        return text
    # Add space before capital letters that follow lowercase
    result = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    # Add space after periods followed by capital
    result = re.sub(r'\.([A-Z])', r'. \1', result)
    # Add space after commas
    result = re.sub(r',([A-Za-z])', r', \1', result)
    return result


def extract_all_from_folder(source_folder: str, output_folder: str):
    """Extract appeal letters from all PDFs in a folder."""
    source_path = Path(source_folder)
    output_path = Path(output_folder)
    output_path.mkdir(parents=True, exist_ok=True)
    
    results = []
    
    for pdf_file in source_path.glob("*.pdf"):
        print(f"Processing: {pdf_file.name}")
        
        extracted = extract_appeal_letter(str(pdf_file))
        
        if "error" in extracted:
            print(f"  ERROR: {extracted['error']}")
            continue
        
        if not extracted["raw_letter"]:
            print(f"  No appeal letter found")
            continue
        
        # Save individual letter with spaces added back
        letter_data = {
            "source_pdf": extracted["source_pdf"],
            "hook": add_spaces_to_text(extracted["hook"]),
            "history_paragraph": add_spaces_to_text(extracted["history_paragraph"]),
            "first_midnight": add_spaces_to_text(extracted["first_midnight"]),
            "second_midnight": add_spaces_to_text(extracted["second_midnight"]),
            "closing": add_spaces_to_text(extracted["closing"]),
        }
        
        # Save to JSON
        output_file = output_path / f"{pdf_file.stem}_letter.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(letter_data, f, indent=2)
        
        results.append(letter_data)
        print(f"  Saved: {output_file.name}")
    
    # Save summary of all letters
    summary_file = output_path / "_all_letters.json"
    with open(summary_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2)
    
    print(f"\nExtracted {len(results)} appeal letters")
    print(f"Summary saved to: {summary_file}")
    
    return results


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        # Default paths
        source_folder = r"C:\Users\ejones08\OneDrive - SSM Health\Patient Stay Agent\Examples"
        output_folder = r"C:\Users\ejones08\source\repos\aec-patient-stay-api\examples\extracted_letters"
    else:
        source_folder = sys.argv[1]
        output_folder = sys.argv[2] if len(sys.argv) > 2 else "./extracted_letters"
    
    print(f"Source: {source_folder}")
    print(f"Output: {output_folder}")
    print()
    
    extract_all_from_folder(source_folder, output_folder)
