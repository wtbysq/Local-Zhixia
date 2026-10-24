"""生成 本地智匣 的应用图标（纯几何，不依赖中文字体渲染）。

图形语义：深青底 + 一个带盖的匣子。盖子略宽出一条边，形成「匣」的轮廓，
不用文字，16px 下也不会糊成一团。
"""
import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "desktop", "本地智匣.ico")

BG = (15, 107, 92, 255)        # 主题深青
LID = (238, 244, 242, 255)     # 盖子：近白
BODY = (188, 220, 211, 255)    # 匣身：浅青
SLOT = (15, 60, 52, 255)       # 缝隙：背景压暗


def render(size=256):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 256.0

    def box(xy, r, fill):
        d.rounded_rectangle([v * s for v in xy], radius=r * s, fill=fill)

    box((0, 0, 256, 256), 56, BG)          # 底板
    box((50, 78, 206, 106), 12, LID)       # 盖：比匣身宽出一条边
    box((66, 122, 190, 202), 16, BODY)     # 匣身
    box((112, 150, 144, 159), 4, SLOT)     # 锁扣
    return img


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    base = render(256)
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    base.save(OUT, format="ICO", sizes=sizes)
    print("已生成 %s（%d 字节，含 %d 种尺寸）" % (OUT, os.path.getsize(OUT), len(sizes)))
    # 顺带留一张 PNG 方便在文档/预览里看效果
    png = os.path.join(ROOT, "desktop", "icon-preview.png")
    base.save(png)
    print("预览图 %s" % png)


if __name__ == "__main__":
    main()
