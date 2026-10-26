import sys

lines = open('app/routers/bookings.py', 'r', encoding='utf-8').readlines()
with open('app/routers/bookings.py', 'w', encoding='utf-8') as f:
    for i, line in enumerate(lines):
        if 170 <= i < 252:
            f.write('# ' + line)
        else:
            f.write(line)
