"""Analyze extracted appeal letter patterns."""
import json

with open(r'C:\Users\ejones08\source\repos\aec-patient-stay-api\examples\extracted_letters\_all_letters.json', 'r') as f:
    letters = json.load(f)

print(f'Total letters: {len(letters)}')
print()

# Analyze patterns
stats = {
    'hook_admitted': 0,
    'hook_required': 0,
    'm1_member': 0,
    'm1_patient': 0,
    'm2_member': 0,
    'm2_patient': 0,
    'close_member': 0,
    'close_patient': 0
}

for letter in letters:
    hook = letter.get('hook') or ''
    m1 = letter.get('first_midnight') or ''
    m2 = letter.get('second_midnight') or ''
    close = letter.get('closing') or ''
    
    if 'admitted' in hook.lower():
        stats['hook_admitted'] += 1
    if 'required' in hook.lower():
        stats['hook_required'] += 1
        
    if 'member' in m1.lower():
        stats['m1_member'] += 1
    if 'patient' in m1.lower():
        stats['m1_patient'] += 1
        
    if 'member' in m2.lower():
        stats['m2_member'] += 1
    if 'patient' in m2.lower():
        stats['m2_patient'] += 1
        
    if 'member' in close.lower():
        stats['close_member'] += 1
    if 'patient' in close.lower():
        stats['close_patient'] += 1

print('HOOK PATTERN:')
print(f'  Your member was admitted...: {stats["hook_admitted"]}')
print(f'  Your member required...: {stats["hook_required"]}')
print()
print('FIRST MIDNIGHT PATTERN:')
print(f'  Uses member: {stats["m1_member"]}')
print(f'  Uses patient: {stats["m1_patient"]}')
print()
print('SECOND MIDNIGHT PATTERN:')
print(f'  Uses member: {stats["m2_member"]}')
print(f'  Uses patient: {stats["m2_patient"]}')
print()
print('CLOSING PATTERN:')
print(f'  Uses member: {stats["close_member"]}')
print(f'  Uses patient: {stats["close_patient"]}')

# Also check first midnight starts
print()
print('FIRST MIDNIGHT STARTS WITH:')
starts_during = 0
starts_management = 0
for letter in letters:
    m1 = letter.get('first_midnight') or ''
    if m1.lower().startswith('during'):
        starts_during += 1
    if 'management' in m1[:50].lower():
        starts_management += 1
print(f'  "During the first midnight...": {starts_during}')
print(f'  Contains "management of": {starts_management}')

print()
print('SECOND MIDNIGHT STARTS WITH:')
starts_during = 0
for letter in letters:
    m2 = letter.get('second_midnight') or ''
    if m2.lower().startswith('during'):
        starts_during += 1
print(f'  "During the second midnight...": {starts_during}')

# Sample a few letters for review
print()
print('=' * 60)
print('SAMPLE LETTERS:')
print('=' * 60)
for i, letter in enumerate(letters[:3]):
    print(f'\n--- {letter["source_pdf"]} ---')
    print(f'HOOK: {letter.get("hook", "N/A")[:200]}...')
    print(f'M1: {letter.get("first_midnight", "N/A")[:150]}...')
    print(f'M2: {letter.get("second_midnight", "N/A")[:150]}...')
