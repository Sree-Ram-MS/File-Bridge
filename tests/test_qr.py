"""QR tests: python3 tests/test_qr.py  (no dependencies)"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import filebridge as fb

# A typical dashboard URL fits version 2 (25x25 modules).
mat = fb._qr_build_matrix("http://192.168.1.20:8000")
assert len(mat) == 25 and all(len(row) == 25 for row in mat)
# Finder patterns: solid top row / left column at all three corners.
assert mat[0][:7] == [1] * 7 and mat[24][:7] == [1] * 7 and mat[0][18:] == [1] * 7
assert [mat[r][0] for r in range(7)] == [1] * 7
# Timing pattern on row 6 / column 6.
assert [mat[6][c] for c in range(8, 17)] == [1, 0, 1, 0, 1, 0, 1, 0, 1]
# White separators around all three finder patterns.
assert mat[7][:8] == [0] * 8 and mat[7][17:] == [0] * 8 and mat[17][:8] == [0] * 8
assert [mat[r][7] for r in range(8)] == [0] * 8
assert [mat[r][17] for r in range(8)] == [0] * 8
assert [mat[r][7] for r in range(17, 25)] == [0] * 8
# Dark module for version 2 is at row 17, column 8.
assert mat[17][8] == 1
# Format info must decode to a valid mask: vertical copy carries bits
# 0-7 down column 8, horizontal copy carries bits 14-7 across row 8.
fmt = 0
for i, r in enumerate((0, 1, 2, 3, 4, 5, 7, 8)):
    fmt |= mat[r][8] << i
for i, c in enumerate((0, 1, 2, 3, 4, 5, 7, 8)):
    fmt |= mat[8][c] << (14 - i)
assert fmt in {fb._qr_format_bits(m) for m in range(8)}
# No module is left unset after masking / format placement.
assert all(v in (0, 1) for row in mat for v in row)

# A longer LAN URL still fits version 3 (29x29).
mat3 = fb._qr_build_matrix("http://255.255.255.255:65535")
assert len(mat3) == 29

# Dashboard URLs are at most 28 characters; anything longer is rejected
# rather than silently producing a wrong-size code.
try:
    fb._qr_build_matrix("http://192.168.1.20:8000/" + "x" * 30)
    raise SystemExit("expected ValueError for over-long text")
except ValueError:
    pass

svg = fb.qr_svg("http://192.168.1.20:8000")
assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
assert 'viewBox="0 0 33 33"' in svg  # 25 modules + 4-module quiet zone each side
assert "<path" in svg and svg.endswith("</svg>")
print("all QR tests passed")
