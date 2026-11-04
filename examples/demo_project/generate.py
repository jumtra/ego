from pathlib import Path

Path('result.txt').write_text('OK\n', encoding='utf-8')
print('Generated a deterministic result')
