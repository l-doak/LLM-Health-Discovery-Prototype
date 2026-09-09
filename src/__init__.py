# Makes src/ a proper Python package so modules can do `from src import db`
# etc. and so `pytest` can discover and import them consistently regardless
# of which directory it's run from.
