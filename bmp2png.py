import sys
from PIL import Image

src, dst = sys.argv[1], sys.argv[2]
img = Image.open(src)
img.save(dst)
print(f"{src} ({img.width}x{img.height}) -> {dst}")
