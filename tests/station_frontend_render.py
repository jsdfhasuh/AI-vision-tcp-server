"""Compatibility entry for explicit offline synthetic catalog/monitor DOM tests."""
from catalog_browser_smoke import main

if __name__ == '__main__':
    main(default_offline=True)
