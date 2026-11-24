import re

sample_names = [
    'Tilak Nagar Crossing (Najafgarh Road)',
    'Uttam Nagar (Najafgarh Road)',
    'Najafgarh Terminal',
    'Chhawla Stand Najafgarh',
    'Saket (Mehrauli Badarpur Road)',
    'Badarpur Border (T)'
]

for name in sample_names:
    cleaned = re.sub(r'\(.*?\)', '', name).strip().lower()
    print(f'{name} -> cleaned: "{cleaned}" | has najafgarh: {"najafgarh" in cleaned}')
