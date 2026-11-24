import re

def is_valid_match(q, full_name):
    q = q.lower().strip()
    full_lower = full_name.lower().strip()
    # Word boundary match for query
    pattern = r'\b' + re.escape(q) + r'\b'
    if not re.search(pattern, full_lower):
        # Also check without spaces if compound
        if q not in full_lower:
            return False
        # If substring is part of another word (like Dayanand for Anand), reject
        if re.search(r'\w' + re.escape(q), full_lower):
            return False
    # If q does not contain 'road', reject 'X Road' or '(X Road)'
    if 'road' not in q and re.search(r'\b' + re.escape(q) + r'\s+(road|marg|path)\b', full_lower):
        return False
    if 'road' not in q and re.search(r'\(.*?' + re.escape(q) + r'.*?road.*?\)', full_lower):
        return False
    return True

print("Najafgarh matches:")
for s in ['Najafgarh Terminal', 'Chhawla Stand Najafgarh', 'Tilak Nagar Crossing (Najafgarh Road)', 'Uttam Nagar (Najafgarh Road)']:
    print(f"  {s} -> {is_valid_match('najafgarh', s)}")

print("Anand Vihar matches:")
for s in ['Anand Vihar ISBT', 'ISBT Anand Vihar', 'Anand Vihar (ISBT) / Gazi Pur Telco', 'Dayanand Vihar']:
    print(f"  {s} -> {is_valid_match('anand vihar', s)}")
