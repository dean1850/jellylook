# Regenerating the demo screenshots

The screenshots in `docs/` are staged demo data (fictional titles, generated
artwork) so the public README carries no copyrighted posters.

```bash
pip install playwright
playwright install chromium
python docs/mock/build_mock.py   # writes mock/mock.html
python docs/mock/shoot.py        # writes docs/screenshot-*.png
```
