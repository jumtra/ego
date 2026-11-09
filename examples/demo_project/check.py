from pathlib import Path

assert Path('result.txt').read_text(encoding='utf-8') == 'OK\n'
print('PASS: result.txt contains the expected text')
