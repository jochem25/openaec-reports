# Baseline render failures

Elke sectie hieronder is een render die is mislukt tijdens `render_baseline.py`. Dit bestand wordt bij elke run aangevuld (niet overschreven) zodat historie behouden blijft.

## openaec_foundation / standaard — 2026-07-10T14:00:52.165742+00:00

```
Traceback (most recent call last):
  File "C:\Github\openaec-reports\scripts\render_baseline.py", line 143, in main
    gen.generate(dict(fixture), stationery_dir, tmp_pdf)
  File "C:\Github\openaec-reports\src\openaec_reports\core\renderer_v2.py", line 2559, in generate
    self._render_content(content, data)
  File "C:\Github\openaec-reports\src\openaec_reports\core\renderer_v2.py", line 2608, in _render_content
    renderer.render_section(section)
  File "C:\Github\openaec-reports\src\openaec_reports\core\renderer_v2.py", line 2220, in render_section
    self.heading_1(number, title)
  File "C:\Github\openaec-reports\src\openaec_reports\core\renderer_v2.py", line 1317, in heading_1
    self._text(n["x"], self.y, number, n["font"], n["size"], n["color"])
               ~^^^^^
KeyError: 'x'
```

## openaec_foundation / standaard — 2026-09-29T09:48:57.835679+00:00

```
Traceback (most recent call last):
  File "D:\dev\openaec\openaec-reports\scripts\render_baseline.py", line 143, in main
    gen.generate(dict(fixture), stationery_dir, tmp_pdf)
  File "D:\dev\openaec\openaec-reports\src\openaec_reports\core\renderer_v2.py", line 3946, in generate
    self._render_content(content, data)
  File "D:\dev\openaec\openaec-reports\src\openaec_reports\core\renderer_v2.py", line 4011, in _render_content
    renderer.render_section(section)
  File "D:\dev\openaec\openaec-reports\src\openaec_reports\core\renderer_v2.py", line 3589, in render_section
    self.heading_1(number, title, str(section.get("part") or ""), reference)
  File "D:\dev\openaec\openaec-reports\src\openaec_reports\core\renderer_v2.py", line 2035, in heading_1
    title_x = self._heading_title_x(s, number)
              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\dev\openaec\openaec-reports\src\openaec_reports\core\renderer_v2.py", line 2018, in _heading_title_x
    font = self.fonts.get_fitz_font(n["font"])
                                    ~^^^^^^^^
KeyError: 'font'
```

